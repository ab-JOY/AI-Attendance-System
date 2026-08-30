"""
What `labels.txt` may contain, and where the display name comes from.

**This is the reader half of todo.md §7.5.** `labels.txt` was
`{label},{student_id}_{student_name}` and the recognition overlay took the
name straight out of it, which made the trained model the authority on how to
spell a person's name: a student corrected in the database kept the old
spelling on screen until somebody retrained. It is now `{label},{student_id}`
and the name is read from `students`.

Three properties are worth holding onto, and each corresponds to a way this
could go quietly wrong:

1. **An un-migrated `labels.txt` is refused, loudly.** The old format parses
   as *something* if you are not careful - `0,23-1-1-0559_Chrizol` would
   happily become the ID `23-1-1-0559_Chrizol` - and a model whose identities
   are subtly wrong is worse than one that will not load.
2. **A database failure does not stop a session.** Recognition works without a
   display name; refusing to start because a name lookup failed would trade a
   cosmetic problem for an outage.
3. **A trained student with no row shows as their ID.** They are refused at
   `save_attendance()` anyway (FS-3), so the honest thing on screen is the
   only identifier that is true.

Nothing here reads the real 55 MB model: the LBPH read is stubbed and the
labels file is written to `tmp_path`. `_attach_display_names` is then tested
both directly - for the three outcomes above - and *through* the loader, and
that second one is not redundant. See the note on it.
"""

import pytest

import recognize_face

# ---------------------------------------------------------------------------
# 1. The format
# ---------------------------------------------------------------------------


@pytest.fixture
def loader(monkeypatch, tmp_path):
    """
    The real `load_model_and_labels()`, with the LBPH read and the name lookup
    stubbed out.

    Deliberately the real function rather than a reimplementation of its
    parsing loop: a test that restates the rules it is checking passes while
    production breaks (lessons.md L3). `LABELS_FILE` and `TRAINER_FILE` are
    module-level strings captured at import, so both are redirected here.
    """

    class FakeRecognizer:
        def read(self, path):
            self.path = path

    trainer = tmp_path / "trainer.yml"
    trainer.write_text("not a real model", encoding="utf-8")

    monkeypatch.setattr(recognize_face.settings, "trainer_dir", tmp_path)
    monkeypatch.setattr(recognize_face, "TRAINER_FILE", str(trainer))
    monkeypatch.setattr(
        recognize_face.cv2.face, "LBPHFaceRecognizer_create", lambda: FakeRecognizer()
    )

    def _load(contents, *, names=None):
        labels = tmp_path / "labels.txt"
        labels.write_text(contents, encoding="utf-8")
        monkeypatch.setattr(recognize_face, "LABELS_FILE", str(labels))

        if names is not None:
            monkeypatch.setattr(
                recognize_face,
                "_attach_display_names",
                lambda label_map: [
                    entry.update(name=names[entry["student_id"]])
                    for entry in label_map.values()
                    if entry["student_id"] in names
                ],
            )

        _, label_map = recognize_face.load_model_and_labels()
        return label_map

    return _load


def test_a_migrated_labels_file_loads(loader):
    label_map = loader(
        "0,23-1-1-0559\n1,23-1-1-0918\n2,23-1-1-0920\n",
        names={},
    )

    assert [entry["student_id"] for entry in label_map.values()] == [
        "23-1-1-0559",
        "23-1-1-0918",
        "23-1-1-0920",
    ]


def test_loading_a_model_resolves_the_display_names(loader):
    """
    ⚠️ **This test exists because a mutation run found nothing without it.**

    Every other assertion about display names calls `_attach_display_names`
    directly, so deleting the *call* from `load_model_and_labels()` left all of
    them green - the function still worked perfectly, and nothing used it. The
    overlay would have shown student IDs and no test would have said so. This
    is lessons.md L12 in a different disguise: the checks were about the right
    function and not about the thing that has to happen.
    """
    label_map = loader(
        "0,23-1-1-0559\n",
        names={"23-1-1-0559": "Chrizol D. Evangelista"},
    )

    assert label_map[0]["name"] == "Chrizol D. Evangelista"


def test_the_pre_migration_format_is_refused(loader):
    """
    ⚠️ The failure this prevents is silent, not loud.

    `23-1-1-0559_Chrizol D. Evangelista` is a perfectly readable string. Taken
    as an ID it matches no student row, so every recognition of that person
    would be refused as "not enrolled" - with a model that loaded cleanly and
    a log that says nothing. `validate_student_id` refuses the underscore,
    which turns a mystery into a startup error naming the line.
    """
    with pytest.raises(ValueError, match="Invalid labels.txt entry on line 1"):
        loader("0,23-1-1-0559_Chrizol D. Evangelista\n", names={})


def test_a_duplicate_student_id_is_refused(loader):
    with pytest.raises(ValueError, match="Duplicate student ID"):
        loader("0,23-1-1-0559\n1,23-1-1-0559\n", names={})


def test_a_duplicate_label_is_refused(loader):
    with pytest.raises(ValueError, match="Duplicate LBPH label"):
        loader("0,23-1-1-0559\n0,23-1-1-0918\n", names={})


def test_an_empty_labels_file_is_refused(loader):
    with pytest.raises(ValueError, match="does not contain any students"):
        loader("\n\n", names={})


# ---------------------------------------------------------------------------
# 2. Where the display name comes from
# ---------------------------------------------------------------------------


class FakeCursor:
    def __init__(self, rows, error=None):
        self._rows = rows
        self._error = error

    def execute(self, sql, params=None):
        if self._error is not None:
            raise self._error

        self.sql = sql
        self.params = params

    def fetchall(self):
        return self._rows


class FakeCursorContext:
    def __init__(self, cursor):
        self._cursor = cursor

    def __call__(self, *args, **kwargs):
        return self

    def __enter__(self):
        return self._cursor

    def __exit__(self, *exc):
        return False


def test_the_name_comes_from_the_students_table(monkeypatch):
    cursor = FakeCursor([{"student_id": "23-1-1-0559", "name": "Chrizol D. Evangelista"}])
    monkeypatch.setattr(recognize_face, "db_cursor", FakeCursorContext(cursor))

    label_map = {0: {"student_id": "23-1-1-0559", "name": "23-1-1-0559"}}
    recognize_face._attach_display_names(label_map)

    assert label_map[0]["name"] == "Chrizol D. Evangelista"


def test_a_student_with_no_row_keeps_their_id_as_the_name(monkeypatch):
    cursor = FakeCursor([])
    monkeypatch.setattr(recognize_face, "db_cursor", FakeCursorContext(cursor))

    label_map = {0: {"student_id": "23-1-1-0559", "name": "23-1-1-0559"}}
    recognize_face._attach_display_names(label_map)

    assert label_map[0]["name"] == "23-1-1-0559"


def test_a_database_failure_leaves_ids_showing_and_does_not_raise(monkeypatch):
    """
    The session must still start. A name is a label on a box; the recognition
    it labels does not depend on it.
    """
    cursor = FakeCursor([], error=RuntimeError("database is down"))
    monkeypatch.setattr(recognize_face, "db_cursor", FakeCursorContext(cursor))

    label_map = {0: {"student_id": "23-1-1-0559", "name": "23-1-1-0559"}}
    recognize_face._attach_display_names(label_map)

    assert label_map[0]["name"] == "23-1-1-0559"


def test_the_lookup_is_one_query_for_every_identity(monkeypatch):
    """
    Not a performance concern at three students - a correctness one. A query
    per label would run inside session start, and a partial failure would
    leave some names resolved and others not, which is harder to reason about
    than all or nothing.
    """
    cursor = FakeCursor(
        [
            {"student_id": "23-1-1-0559", "name": "One"},
            {"student_id": "23-1-1-0918", "name": "Two"},
        ]
    )
    monkeypatch.setattr(recognize_face, "db_cursor", FakeCursorContext(cursor))

    label_map = {
        0: {"student_id": "23-1-1-0559", "name": "23-1-1-0559"},
        1: {"student_id": "23-1-1-0918", "name": "23-1-1-0918"},
    }
    recognize_face._attach_display_names(label_map)

    assert cursor.params == ("23-1-1-0559", "23-1-1-0918")
    assert cursor.sql.count("%s") == 2
    assert [entry["name"] for entry in label_map.values()] == ["One", "Two"]
