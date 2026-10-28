"""
Option B of todo.md §19: every student while the school is small, the class
list only once it is large.

The choice matters for more than speed. With every student loaded, a student
from another class is recognised as themselves and refused ("Not in this
class"). With only the class list loaded, they can only be matched to the
closest classmate - measured on the 17 enrolled students, 2.3% of an
outsider's frames fall under 58.0 for somebody. So the small mode must really
load everyone, and the large mode must really load only the class.

Fake hooks, as in test_recognition_session.py: no camera, model or database.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import recognize_face
from tests.test_recognition_session import Recorder
from vision.session import RecognitionSession

CLASS_A = SimpleNamespace(subject_id=1, subject_code="CS401")
CLASS_B = SimpleNamespace(subject_id=2, subject_code="CS402")
ROSTERS = {1: frozenset({"23-1-1-0559"}), 2: frozenset({"23-1-1-0918"})}


class ClassListRecorder(Recorder):
    def __init__(self, everyone):
        super().__init__()
        self.everyone = everyone
        self.loaded_sets = []

    def load_model(self, student_ids=None):
        self.load_calls += 1
        self.loaded_sets.append(student_ids)
        return object(), {0: {"student_id": "x", "name": "x"}}

    def class_hooks(self, **overrides):
        fields = {
            "loads_everyone": lambda: self.everyone,
            "students_to_load": lambda subject: (
                None if self.everyone else ROSTERS[subject.subject_id]
            ),
        }
        fields.update(overrides)
        return self.hooks(**fields)


def test_a_small_school_loads_everyone_once_for_every_class():
    recorder = ClassListRecorder(everyone=True)
    session = RecognitionSession(hooks=recorder.class_hooks())

    assert session.start(CLASS_A)
    session.stop()
    assert session.start(CLASS_B)

    assert recorder.loaded_sets == [None]


def test_a_large_school_loads_each_class_list_and_reloads_for_another_class():
    recorder = ClassListRecorder(everyone=False)
    session = RecognitionSession(hooks=recorder.class_hooks())

    assert session.start(CLASS_A)
    session.stop()
    assert session.start(CLASS_A)
    session.stop()
    assert session.start(CLASS_B)

    assert recorder.loaded_sets == [ROSTERS[1], ROSTERS[2]]


def test_a_large_school_does_not_preload_everyone():
    recorder = ClassListRecorder(everyone=False)
    session = RecognitionSession(hooks=recorder.class_hooks())

    session.preload_model()

    assert recorder.load_calls == 0


def test_a_small_school_preloads_and_start_uses_it():
    recorder = ClassListRecorder(everyone=True)
    session = RecognitionSession(hooks=recorder.class_hooks())

    session.preload_model()
    assert session.start(CLASS_A)

    assert recorder.loaded_sets == [None]


def test_an_unreadable_class_list_refuses_the_start():
    recorder = ClassListRecorder(everyone=False)

    def broken(_subject):
        raise RuntimeError("database is down")

    session = RecognitionSession(hooks=recorder.class_hooks(students_to_load=broken))

    assert session.start(CLASS_A) is False
    assert recorder.open_camera_calls == 0


# ---------------------------------------------------------------------------
# The decision itself, in recognize_face
# ---------------------------------------------------------------------------


@pytest.fixture
def labels(tmp_path, monkeypatch):
    def _write(count, limit):
        path = tmp_path / "labels.txt"
        path.write_text(
            "".join(f"{n},23-1-1-{n:04d}\n" for n in range(count)), encoding="utf-8"
        )
        monkeypatch.setattr(recognize_face, "LABELS_FILE", str(path))
        monkeypatch.setattr(recognize_face.settings, "recognition_load_all_limit", limit)

    return _write


@pytest.mark.parametrize("count, everyone", [(49, True), (50, True), (51, False)])
def test_the_limit_is_inclusive(labels, count, everyone):
    labels(count, limit=50)

    assert recognize_face.loads_everyone() is everyone


def test_under_the_limit_no_class_list_is_read(labels, monkeypatch):
    labels(17, limit=50)
    monkeypatch.setattr(
        recognize_face, "db_cursor", lambda **_: pytest.fail("read the class list")
    )

    assert recognize_face.students_to_load(CLASS_A) is None


def test_over_the_limit_the_class_list_is_what_loads(labels, monkeypatch):
    labels(3, limit=2)

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(recognize_face, "db_cursor", lambda **_: Cursor())
    monkeypatch.setattr(
        recognize_face.enrolments_repo,
        "enrolled_in",
        lambda cursor, subject_id: [
            {"student_id": "23-1-1-0001"},
            {"student_id": "23-1-1-0002"},
        ] if subject_id == 1 else [],
    )

    assert recognize_face.students_to_load(CLASS_A) == {"23-1-1-0001", "23-1-1-0002"}
