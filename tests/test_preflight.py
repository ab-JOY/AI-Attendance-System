"""
The readiness checks in scripts/preflight.py (D1, D2, B3).

These are the checks a version update ends with on a machine that retrains on
every release, so the thing that matters about them is that they **fail** when
they should. A check that cannot fail is worse than no check: it is a green
light nobody re-examines.

Every case here therefore drives the failing side first and asserts the passing
side second, and the database is a stub - no server, no rows, no seeds to keep
in step with a schema. `db_cursor` is patched out entirely; the checks take a
cursor and never open one, the same contract `repositories/` follows.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = PROJECT_ROOT / "scripts"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import preflight  # noqa: E402


class FakeCursor:
    """
    Returns canned rows chosen by matching a fragment of the SQL.

    Matching on the statement rather than on call order means a test does not
    silently pass when a check is reordered or a query is added.
    """

    def __init__(self, responses):
        self._responses = responses
        self._rows = []

    def execute(self, sql, params=None):
        collapsed = " ".join(sql.split())

        for fragment, rows in self._responses.items():
            if fragment in collapsed:
                self._rows = list(rows)
                return

        raise AssertionError(f"no canned response for: {collapsed[:90]}")

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


@pytest.fixture
def model(tmp_path, monkeypatch):
    """A trainer/ and dataset/ pair under tmp_path, redirected at settings."""
    trainer_dir = tmp_path / "trainer"
    dataset_dir = tmp_path / "dataset"
    trainer_dir.mkdir()
    dataset_dir.mkdir()

    monkeypatch.setattr(preflight.settings, "trainer_dir", trainer_dir)
    monkeypatch.setattr(preflight.settings, "dataset_dir", dataset_dir)

    return trainer_dir, dataset_dir


def write_labels(trainer_dir, student_ids):
    lines = [f"{label},{sid}" for label, sid in enumerate(student_ids)]
    (trainer_dir / "labels.txt").write_text("\n".join(lines), encoding="utf-8")
    (trainer_dir / "trainer.yml").write_text("model", encoding="utf-8")


# ---------------------------------------------------------------------------
# D1 - a student in the database who is not in the model
# ---------------------------------------------------------------------------


def test_a_student_missing_from_the_model_fails(model):
    """
    ⚠️ The check that exists because a retrain reports success while doing this.

    The student is in `students`, is on every screen, and is absent only from
    the file that does the recognising.
    """
    trainer_dir, _dataset_dir = model
    write_labels(trainer_dir, ["23-1-1-0559"])

    cursor = FakeCursor({
        "FROM students": [
            {"student_id": "23-1-1-0559", "name": "Alice"},
            {"student_id": "23-1-1-0918", "name": "Bob"},
        ]
    })

    result = preflight.check_model_covers_every_student(cursor)

    assert result.failed
    assert "23-1-1-0918" in result.detail
    assert "Bob" in result.detail


def test_a_complete_model_passes(model):
    trainer_dir, _dataset_dir = model
    write_labels(trainer_dir, ["23-1-1-0559", "23-1-1-0918"])

    cursor = FakeCursor({
        "FROM students": [
            {"student_id": "23-1-1-0559", "name": "Alice"},
            {"student_id": "23-1-1-0918", "name": "Bob"},
        ]
    })

    assert not preflight.check_model_covers_every_student(cursor).failed


def test_an_unreadable_model_fails_rather_than_passing_vacuously(model):
    """
    ⚠️ No labels.txt must be a failure, never an empty set that satisfies the
    comparison. "Nothing is missing" is true of a model that does not exist.
    """
    cursor = FakeCursor({"FROM students": [{"student_id": "x", "name": "X"}]})

    result = preflight.check_model_covers_every_student(cursor)

    assert result.failed
    assert "could not be read" in result.detail


# ---------------------------------------------------------------------------
# The mirror: a label with no student row
# ---------------------------------------------------------------------------


def test_a_trained_identity_with_no_student_row_fails(model):
    """A test identity left in dataset/ is retrained back in on every release."""
    trainer_dir, _dataset_dir = model
    write_labels(trainer_dir, ["23-1-1-0559", "test-111"])

    cursor = FakeCursor({"FROM students": [{"student_id": "23-1-1-0559"}]})

    result = preflight.check_no_stray_identities(cursor)

    assert result.failed
    assert "test-111" in result.detail


# ---------------------------------------------------------------------------
# D2 - the model is older than the data
# ---------------------------------------------------------------------------


def test_a_model_older_than_the_dataset_fails(model):
    """
    ⚠️ The failure no exit code sees.

    A retrain that never ran, or ran and failed, leaves the previous model in
    place - by design, so a bad retrain cannot destroy a working one. The
    application then starts and recognises against a stale roster while
    looking entirely healthy.
    """
    trainer_dir, dataset_dir = model
    write_labels(trainer_dir, ["23-1-1-0559"])

    folder = dataset_dir / "23-1-1-0918"
    folder.mkdir()
    image = folder / "1.jpg"
    image.write_bytes(b"face")

    model_mtime = (trainer_dir / "trainer.yml").stat().st_mtime
    import os

    os.utime(image, (model_mtime + 600, model_mtime + 600))

    result = preflight.check_model_is_not_stale()

    assert result.failed
    assert "23-1-1-0918" in result.detail


def test_a_fresh_model_passes(model):
    trainer_dir, dataset_dir = model

    folder = dataset_dir / "23-1-1-0559"
    folder.mkdir()
    (folder / "1.jpg").write_bytes(b"face")

    write_labels(trainer_dir, ["23-1-1-0559"])

    assert not preflight.check_model_is_not_stale().failed


def test_the_quarantine_folder_is_not_mistaken_for_a_student(model):
    """
    `_migrated_duplicates` is skipped by training and must be skipped here.

    Counting it would report a student who does not exist, and - because its
    images are old copies - could also make a current model look stale.
    """
    trainer_dir, dataset_dir = model

    quarantine = dataset_dir / "_migrated_duplicates" / "23-1-1-0559_Alice"
    quarantine.mkdir(parents=True)
    (quarantine / "1.jpg").write_bytes(b"face")

    write_labels(trainer_dir, ["23-1-1-0559"])

    assert preflight.dataset_folders() == set()


# ---------------------------------------------------------------------------
# B3 - a student in no class list
# ---------------------------------------------------------------------------


def test_a_student_in_no_class_fails():
    cursor = FakeCursor({
        "LEFT JOIN enrolments": [{"student_id": "23-1-1-0918", "name": "Bob"}]
    })

    result = preflight.check_every_student_is_in_a_class(cursor)

    assert result.failed
    assert "23-1-1-0918" in result.detail


def test_no_classless_students_passes():
    cursor = FakeCursor({"LEFT JOIN enrolments": []})

    assert not preflight.check_every_student_is_in_a_class(cursor).failed


# ---------------------------------------------------------------------------
# Demo content, and the exit status the deploy reads
# ---------------------------------------------------------------------------


def test_no_instructor_and_no_history_both_reported():
    cursor = FakeCursor({
        "FROM instructors": [{"n": 0}],
        "FROM attendance": [{"n": 0}],
    })

    result = preflight.check_demo_content(cursor)

    assert result.failed
    assert "instructor" in result.detail
    assert "attendance" in result.detail


def test_report_exit_status_is_zero_only_when_everything_passed(capsys):
    """The whole point of the script: a deploy can end with it."""
    passing = [preflight.Result("a", preflight.PASS, "fine")]
    failing = [
        preflight.Result("a", preflight.PASS, "fine"),
        preflight.Result("b", preflight.FAIL, "broken", "do the thing"),
    ]

    assert preflight.report(passing) == 0
    assert preflight.report(failing) == 1

    assert "do the thing" in capsys.readouterr().out


def test_a_skipped_check_does_not_fail_the_run():
    """SKIP means 'could not ask', which is not the same as 'the answer is no'."""
    results = [preflight.Result("a", preflight.SKIP, "no data")]

    assert preflight.report(results) == 0
