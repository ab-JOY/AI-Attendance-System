"""
Three different failures stopped looking like one roster problem (US-10).

`save_attendance()` returned `None` for *not a student*, *not in this class*
and *the database raised*, and the overlay drew one orange "Not Enrolled" for
all three. Only the middle one is something the operator can fix at the kiosk
with a student standing in front of them - so a MySQL outage looked exactly
like somebody having forgotten to add a name to a class list, and would have
been reported as one.

⚠️ **The enum members are falsy on purpose.** Every caller tests the result of
`save_attendance()` for truth, so the reason can be carried without any call
site having to learn about it first. A test below pins that, because making
them truthy would silently mark absent students present.

No database: `db_cursor` is replaced with a scripted cursor. Every identifier
is fake (lessons.md L6).
"""

from __future__ import annotations

import contextlib

import mysql.connector
import pytest

import recognize_face
from recognize_face import ActiveSubject, NotRecorded, save_attendance, status_color

STUDENT = "SEC-TEST-NOBODY"
SUBJECT = ActiveSubject(subject_id=1, session_id=7, subject_code="CS401")


class ScriptedCursor:
    """Answers each `fetchone()` from a queue, in order."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.queries = []
        # What MySQL reports for a row that was inserted. 2 would mean a
        # duplicate was updated - the Absent upgrade below.
        self.rowcount = 1

    def execute(self, sql, params=None):
        self.queries.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.answers.pop(0)


@pytest.fixture
def scripted(monkeypatch):
    def install(answers):
        cursor = ScriptedCursor(answers)

        @contextlib.contextmanager
        def fake_cursor(*_args, **_kwargs):
            yield cursor

        monkeypatch.setattr(recognize_face, "db_cursor", fake_cursor)

        return cursor

    return install


def test_a_student_not_in_this_class_says_so(scripted):
    """
    The actionable one, and the reported symptom. The enrolment join finds
    nothing; the student exists.
    """
    scripted([None, {"student_id": STUDENT}])

    assert save_attendance(STUDENT, SUBJECT) is NotRecorded.NOT_IN_CLASS
    assert NotRecorded.NOT_IN_CLASS.value == "Not in this class"


def test_a_face_the_database_does_not_know_is_a_different_message(scripted):
    """
    The model is ahead of the `students` table - a student deleted without a
    retrain. Nothing about a class list will fix it, so it must not say to go
    and fix one.
    """
    scripted([None, None])

    assert save_attendance(STUDENT, SUBJECT) is NotRecorded.UNKNOWN_STUDENT


def test_the_second_query_runs_only_on_the_refusal_path(scripted):
    """
    The cost, stated. It is one extra round trip per *confirmed identity*, not
    per frame, and it never runs when the student is enrolled - the hot query
    above is untouched.
    """
    cursor = scripted([None, {"student_id": STUDENT}])

    save_attendance(STUDENT, SUBJECT)

    assert len(cursor.queries) == 2
    assert "FROM students WHERE student_id" in cursor.queries[1][0]


def test_a_database_failure_is_not_reported_as_a_roster_problem(monkeypatch):
    """
    ⚠️ This is the one that mattered most and was hidden best. Nothing was
    written, and the screen blamed the class list.
    """
    @contextlib.contextmanager
    def exploding_cursor(*_args, **_kwargs):
        raise mysql.connector.Error("the database is unreachable")
        yield  # pragma: no cover - unreachable, keeps this a generator

    monkeypatch.setattr(recognize_face, "db_cursor", exploding_cursor)

    assert save_attendance(STUDENT, SUBJECT) is NotRecorded.ERROR


def test_every_cause_is_falsy():
    """
    The call site reads `if recorded:` and marks the student present when it is
    true. A truthy member here would record attendance for a face that was
    refused, which is worse than the finding this enum exists for.
    """
    for cause in NotRecorded:
        assert not cause, f"{cause.name} is truthy"


def test_an_outage_and_a_missing_class_list_are_different_colours():
    """
    US-6: the colour is the second signal, but it still has to be the right
    one. Orange is what the other *actionable* hints use - somebody can add a
    student to a class. A failed write is not actionable at the kiosk and is
    red, like every other rejection.
    """
    assert status_color(NotRecorded.NOT_IN_CLASS.value) == (0, 165, 255)
    assert status_color(NotRecorded.UNKNOWN_STUDENT.value) == (0, 165, 255)
    assert status_color(NotRecorded.ERROR.value) == (0, 0, 255)


def test_a_recorded_status_is_still_green_or_amber():
    """The paths this change must not have disturbed."""
    assert status_color("Present") == (0, 255, 0)
    assert status_color("Late") == (0, 215, 255)


# ---------------------------------------------------------------------------
# An earlier Absent must not survive a later recognition (reported 2026-09-12)
# ---------------------------------------------------------------------------


def test_the_write_upgrades_an_absent_row_and_nothing_else(scripted):
    """
    ⚠️ **The duplicate branch used to be `id = id`, a deliberate no-op.**

    That was right while the only row that could already exist was an earlier
    Present. Since FS-4, ending a session writes an Absent row for everyone
    unrecognised, on the same UNIQUE key (student_id, subject_id,
    attendance_date) - so a student marked absent at 8am who is recognised at
    9am had the write discarded, and the register kept saying Absent for
    someone standing in front of the camera.

    Only Absent is overwritten: a Present must never be restamped with a later
    arrival time, and a Late must never be quietly promoted.
    """
    cursor = scripted([{"student_id": STUDENT, "name": "T", "scheduled_start": None}])

    save_attendance(STUDENT, SUBJECT)

    sql, _params = cursor.queries[-1]

    assert "ON DUPLICATE KEY UPDATE" in sql
    assert "id = id" not in sql, "the no-op branch is what discarded the upgrade"

    for column in ("session_id", "time_in", "status"):
        assert f"IF(status = 'Absent', VALUES({column}), {column})" in sql, (
            f"{column} must only be replaced when the existing row is Absent"
        )


def test_status_is_the_last_assignment_in_the_upgrade(scripted):
    """
    ⚠️ **Order is load-bearing.** MySQL applies the assignments left to right
    and each `IF` reads the column as it stands at that point. Promote `status`
    first and the other two would test a column that already says Present,
    keeping the absentee's NULL `time_in` on a student who did arrive.
    """
    cursor = scripted([{"student_id": STUDENT, "name": "T", "scheduled_start": None}])

    save_attendance(STUDENT, SUBJECT)

    sql, _params = cursor.queries[-1]
    upgrade = sql.split("ON DUPLICATE KEY UPDATE", 1)[1]

    # "status = IF(" is the assignment; "status = 'Absent'" inside each IF is
    # the test, so match on the assignment's shape rather than the bare name.
    assigned = upgrade.index("status = IF(")

    assert assigned > upgrade.index("time_in = IF("), (
        "status must be assigned after the columns whose IF reads it"
    )
    assert assigned > upgrade.index("session_id = IF("), (
        "status must be assigned after the columns whose IF reads it"
    )
