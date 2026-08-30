"""
The Phase 4 data model, against a real server.

**What this file is for.** handover-phase-4.md §5 is a table of eight findings
recorded as fixed - FS-3, FS-4, FS-5, FS-7, FS-8, RE-3, RE-4 - every one of
them verified by end-to-end scripts written into a session scratchpad and then
lost. The claims were true when made. Nothing was stopping them becoming false
again, and this project's whole history is defects that looked like working
code: a threshold band that made a recognised student unmarkable for ever
(FS-14), an evaluator that scored zero images while printing a clean summary,
a migration that reported success while corrupting referential state (L11).

So each test below is written against the *defect*, not against the current
code. The docstrings say what the behaviour was before, because a test that
only describes what the code does today cannot tell you whether it is right.

**No camera anywhere.** `save_attendance()` is the real write path and takes an
`ActiveSubject`, so the SQL under test runs exactly as it does in a session
without any of the acquisition stack.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from infra.db import db_cursor

from .conftest import (
    make_attendance,
    make_enrolment,
    make_instructor,
    make_student,
    make_subject,
    sign_in_as,
)

pytestmark = pytest.mark.integration

STUDENT = "INT-TEST-0001"
OTHER_STUDENT = "INT-TEST-0002"


def active_subject(subject_id, session_id=None, subject_code="INT-TEST-101"):
    from recognize_face import ActiveSubject

    return ActiveSubject(
        subject_id=subject_id,
        session_id=session_id,
        subject_code=subject_code,
    )


def attendance_rows(student_id=None):
    with db_cursor(dictionary=True) as cursor:
        if student_id is None:
            cursor.execute("SELECT * FROM attendance ORDER BY id")
        else:
            cursor.execute(
                "SELECT * FROM attendance WHERE student_id = %s ORDER BY id",
                (student_id,),
            )
        return cursor.fetchall()


# ===========================================================================
# FS-3 - the register is scoped to enrolments
#
# Before Phase 4 there was no student-to-subject relation anywhere in the
# schema, so /end-attendance marked *every student in the database* absent for
# *every subject*. Demonstrated on 2026-08-08: with subject CS401 selected, an
# unrelated test student was reported Absent for it.
# ===========================================================================


def test_an_unenrolled_student_cannot_be_recorded():
    """
    Being in the database is not being in the class.

    ⚠️ The refusal used to be a bare `None`, shared with "no such student" and
    "the database raised" - one orange "Not Enrolled" on the overlay for three
    situations, only one of which anybody at the kiosk can act on (US-10). It
    is now a falsy `NotRecorded`, so the assertion names the cause. `not
    recorded` still holds, which is what FS-3 is about.
    """
    from recognize_face import NotRecorded, save_attendance

    with db_cursor(commit=True) as cursor:
        make_student(cursor, STUDENT)
        subject_id = make_subject(cursor)
        # Deliberately no enrolment row.

    recorded = save_attendance(STUDENT, active_subject(subject_id))

    assert not recorded
    assert recorded is NotRecorded.NOT_IN_CLASS
    assert attendance_rows(STUDENT) == [], (
        "An unenrolled student was written into the register. FS-3 has "
        "reopened: the enrolments join in save_attendance() is gone or wrong."
    )


def test_a_student_in_no_class_counts_zero_not_one():
    """
    US-10's list screens, against a real database, because the trap is SQL.

    `all_students()` reports a `classes` count so that "enrolled for
    recognition but in no class" is visible before a student stands in front of
    the camera and is refused. It is a LEFT JOIN, and a LEFT JOIN with no match
    still supplies one row of NULLs - so `COUNT(*)` would report **1** for a
    student in no class at all, which is precisely the wrong answer to the only
    question this column exists to answer. `COUNT(e.subject_id)` skips the
    NULL.

    Nothing in the route or template layer can catch that: both numbers render
    perfectly well.
    """
    from repositories import students as students_repo

    with db_cursor(commit=True) as cursor:
        make_student(cursor, STUDENT)
        make_student(cursor, OTHER_STUDENT)
        subject_id = make_subject(cursor)
        make_enrolment(cursor, OTHER_STUDENT, subject_id)
        # STUDENT is deliberately in no class.

    with db_cursor(dictionary=True) as cursor:
        counts = {
            row["student_id"]: row["classes"]
            for row in students_repo.all_students(cursor)
        }

    assert counts[STUDENT] == 0, (
        "a student in no class reported a class - COUNT(*) over a LEFT JOIN "
        "counts the NULL row"
    )
    assert counts[OTHER_STUDENT] == 1


def test_an_unenrolled_student_is_not_marked_absent_either(client):
    """
    The half of FS-3 that produced the original evidence. Absence was computed
    over `students`, so a student who had never been near the subject was
    reported Absent for it.
    """
    with db_cursor(commit=True) as cursor:
        subject_id = make_subject(cursor)
        make_student(cursor, STUDENT)
        make_student(cursor, OTHER_STUDENT, name="Not In This Class")
        make_enrolment(cursor, STUDENT, subject_id)

    sign_in_as(client)
    response = client.post("/end-attendance", data={"subject_id": str(subject_id)})

    assert response.status_code == 200

    recorded = {row["student_id"] for row in attendance_rows()}

    assert recorded == {STUDENT}, (
        f"The register covered {sorted(recorded)}. Only the enrolled student "
        "belongs in it; the other is not on this class list."
    )


# ===========================================================================
# FS-4 - Absent is persisted
#
# It was computed in memory and rendered, never stored. /reports counted
# Absent rows from a table nothing wrote an Absent row into, so it reported 0
# every time: two screens, contradictory numbers, same session.
# ===========================================================================


def test_ending_a_session_writes_absent_rows(client):
    with db_cursor(commit=True) as cursor:
        subject_id = make_subject(cursor)
        make_student(cursor, STUDENT)
        make_enrolment(cursor, STUDENT, subject_id)

    sign_in_as(client)
    client.post("/end-attendance", data={"subject_id": str(subject_id)})

    rows = attendance_rows(STUDENT)

    assert len(rows) == 1
    assert rows[0]["status"] == "Absent", (
        "No Absent row survived the session. This is FS-4: the value existed "
        "only in the rendered page."
    )


def test_an_absent_row_carries_no_arrival_time(client):
    """
    R4. Every Absent row used to get `time_in = NOW()` - the moment the
    operator pressed End, identical across the whole cohort - so reports and
    the export showed an arrival time for students who never arrived.

    ⚠️ **Asserted against the column, and it has to be.** `attendance.time_in`
    was TIME NOT NULL, and this server has no STRICT_TRANS_TABLES: writing NULL
    into it did not fail, it was silently converted to 00:00:00 on an INSERT
    reporting success. So the fix is migration 007 plus the code change, and a
    test that only checked "not the end time" would have passed on a register
    full of fabricated midnights (lessons.md L11).
    """
    with db_cursor(commit=True) as cursor:
        subject_id = make_subject(cursor)
        make_student(cursor, STUDENT)
        make_enrolment(cursor, STUDENT, subject_id)

    sign_in_as(client)
    client.post("/end-attendance", data={"subject_id": str(subject_id)})

    with db_cursor(dictionary=True) as cursor:
        cursor.execute(
            "SELECT time_in IS NULL AS is_null, time_in "
            "FROM attendance WHERE student_id = %s",
            (STUDENT,),
        )
        row = cursor.fetchone()

    assert row["is_null"] == 1, (
        f"An absence was stamped with an arrival time of {row['time_in']!r}. "
        "If that is 0:00:00 the column is still NOT NULL and the NULL was "
        "coerced - check that migration 007 has been applied."
    )


def test_the_register_column_permits_a_missing_arrival_time():
    """
    Migration 007, asserted as schema rather than as behaviour.

    The test above would also pass on a server that happens to be in strict
    mode and raised instead - this one names the condition the fix depends on.
    """
    with db_cursor(dictionary=True) as cursor:
        cursor.execute("SHOW COLUMNS FROM attendance LIKE 'time_in'")
        column = cursor.fetchone()

    assert column["Null"] == "YES", (
        "attendance.time_in is NOT NULL. On a server without "
        "STRICT_TRANS_TABLES that does not refuse a NULL, it converts it to "
        "00:00:00 - so every absence would record an arrival at midnight."
    )


def test_reports_counts_the_absent_rows_that_were_written(client):
    """
    FS-4's actual symptom. `total_absent` was structurally 0 - not miscounted,
    but counting rows that could not exist.
    """
    with db_cursor(commit=True) as cursor:
        subject_id = make_subject(cursor)
        make_student(cursor, STUDENT)
        make_student(cursor, OTHER_STUDENT, name="Second Student")
        make_enrolment(cursor, STUDENT, subject_id)
        make_enrolment(cursor, OTHER_STUDENT, subject_id)
        make_attendance(cursor, STUDENT, subject_id, status="Present")

    sign_in_as(client)
    client.post("/end-attendance", data={"subject_id": str(subject_id)})

    response = client.get("/reports")
    body = response.get_data(as_text=True)

    assert response.status_code == 200

    statuses = sorted(row["status"] for row in attendance_rows())
    assert statuses == ["Absent", "Present"]

    # The rendered page has to agree with the table. Two screens disagreeing
    # about one session is the finding itself.
    assert "Second Student" in body


# ===========================================================================
# FS-8 - Late is derived from the subject's scheduled start
#
# subjects.time_in was a VARCHAR while attendance.time_in is a TIME, so there
# was nothing sound to compare and everything recorded was "Present".
# Migration 005 made it a real TIME - and MySQL hands a TIME back as a
# timedelta, which is the part a unit test with a hand-made value would miss.
# ===========================================================================


def _subject_time_offset_from_now(hours):
    """
    A wall-clock time `hours` away from now, as HH:MM:SS.

    Anchored on the current time rather than a literal so the test does not
    depend on when it runs. Near midnight the offset would wrap and invert the
    meaning of the test, so those runs are skipped rather than asserted
    wrongly.
    """
    now = datetime.now()
    shifted = now + timedelta(hours=hours)

    if shifted.date() != now.date():
        pytest.skip(
            "Running within an hour of midnight would wrap the scheduled "
            "start onto another day and invert this test."
        )

    return shifted.strftime("%H:%M:%S")


@pytest.mark.parametrize(
    ("hours_from_now", "expected"),
    [
        (-1, "Late"),      # class started an hour ago
        (1, "Present"),    # class starts in an hour
    ],
)
def test_status_is_derived_from_the_scheduled_start(hours_from_now, expected):
    """
    The round trip is the point: the value goes into a TIME column and comes
    back as a timedelta, and derive_status() has to handle that. A unit test
    passing a `datetime.time` never exercises the conversion.

    ⚠️ **This test used to prove nothing about production.** It called
    `save_attendance(student, subject)` with no status - correct, and a
    signature the recognition path did not use: the only production caller
    passed a literal "Present" third argument, which short-circuited the
    derivation entirely. The harness exercised one branch and the system ran
    the other, green throughout (lessons.md L3). The parameter is gone now, so
    there is one signature and this is it.
    """
    from recognize_face import save_attendance

    scheduled = _subject_time_offset_from_now(hours_from_now)

    with db_cursor(commit=True) as cursor:
        subject_id = make_subject(cursor, time_in=scheduled)
        make_student(cursor, STUDENT)
        make_enrolment(cursor, STUDENT, subject_id)

    # The return value is the status that was written, so the overlay can show
    # what the register holds rather than a fixed word.
    assert save_attendance(STUDENT, active_subject(subject_id)) == expected

    rows = attendance_rows(STUDENT)

    assert len(rows) == 1
    assert rows[0]["status"] == expected, (
        f"Scheduled start {scheduled}, arrival now, expected {expected}. "
        "Everything this system recorded before Phase 4 was 'Present' "
        "because the comparison could not be made at all (FS-8)."
    )


def test_a_subject_with_no_scheduled_start_makes_nobody_late():
    """A statement about missing data, not about punctuality."""
    from recognize_face import save_attendance

    with db_cursor(commit=True) as cursor:
        subject_id = make_subject(cursor, time_in=None)
        make_student(cursor, STUDENT)
        make_enrolment(cursor, STUDENT, subject_id)

    save_attendance(STUDENT, active_subject(subject_id))

    assert attendance_rows(STUDENT)[0]["status"] == "Present"


# ===========================================================================
# RE-3 - the unique constraint, not a read-then-write check
#
# Duplicate suppression was a SELECT followed by an INSERT: a TOCTOU race
# under concurrent recognition, which a comment in the function admitted to.
# ===========================================================================


def test_the_unique_constraint_exists_on_the_offering():
    """
    On (student_id, subject_id, attendance_date) - `subject_id`, not
    `subject_code`, because a subjects row is a section offering and two
    sections share a code (migration 004).
    """
    with db_cursor(dictionary=True) as cursor:
        cursor.execute("SHOW INDEX FROM attendance")
        rows = cursor.fetchall()

    unique_indexes = {}
    for row in rows:
        if row["Non_unique"] == 0:
            unique_indexes.setdefault(row["Key_name"], []).append(
                (row["Seq_in_index"], row["Column_name"])
            )

    columns = {
        name: [column for _, column in sorted(parts)]
        for name, parts in unique_indexes.items()
    }

    assert ["student_id", "subject_id", "attendance_date"] in columns.values(), (
        f"No UNIQUE index on the register key. Found: {columns}. "
        "Without it, duplicate suppression is back to a Python race (RE-3)."
    )


def test_recording_the_same_student_twice_is_one_row():
    from recognize_face import save_attendance

    with db_cursor(commit=True) as cursor:
        subject_id = make_subject(cursor)
        make_student(cursor, STUDENT)
        make_enrolment(cursor, STUDENT, subject_id)

    subject = active_subject(subject_id)

    # Both must report success. The trap RE-3 left behind was the blanket
    # `except mysql.connector.Error`: IntegrityError is one, so an ordinary
    # duplicate would read as failure, render as a refusal on the overlay,
    # and be retried on every single frame.
    #
    # The assertion is "a status came back", not which one: `make_subject()`
    # schedules a start time that has already passed, so the derivation
    # correctly answers Late here. Pinning "Present" would make this test fail
    # for a reason that has nothing to do with RE-3 - which is what it did the
    # first time the derivation was allowed to run.
    assert save_attendance(STUDENT, subject) is not None
    assert save_attendance(STUDENT, subject) is not None

    assert len(attendance_rows(STUDENT)) == 1


def test_ending_a_session_twice_does_not_overwrite_a_present(client):
    """
    ON DUPLICATE KEY UPDATE id = id, rather than writing Absent again. A second
    click on End Session must not turn an attended student into an absentee.
    """
    with db_cursor(commit=True) as cursor:
        subject_id = make_subject(cursor)
        make_student(cursor, STUDENT)
        make_enrolment(cursor, STUDENT, subject_id)
        make_attendance(cursor, STUDENT, subject_id, status="Present")

    sign_in_as(client)
    client.post("/end-attendance", data={"subject_id": str(subject_id)})
    client.post("/end-attendance", data={"subject_id": str(subject_id)})

    rows = attendance_rows(STUDENT)

    assert len(rows) == 1
    assert rows[0]["status"] == "Present"


# ===========================================================================
# RE-4 - foreign keys
#
# There were none at all. Deleting a student left orphan attendance rows
# unless the application remembered to cascade by hand, which it did in one of
# two paths.
# ===========================================================================


def test_deleting_a_student_cascades_their_attendance_away():
    """
    ON DELETE CASCADE replaces the hand-rolled cascade - and RA 10173 erasure
    has to be real, so the rows must actually go.
    """
    with db_cursor(commit=True) as cursor:
        subject_id = make_subject(cursor)
        make_student(cursor, STUDENT)
        make_enrolment(cursor, STUDENT, subject_id)
        make_attendance(cursor, STUDENT, subject_id)

    assert len(attendance_rows(STUDENT)) == 1

    with db_cursor(commit=True) as cursor:
        cursor.execute("DELETE FROM students WHERE student_id = %s", (STUDENT,))

    assert attendance_rows(STUDENT) == []

    with db_cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM enrolments WHERE student_id = %s", (STUDENT,)
        )
        assert cursor.fetchone()[0] == 0, "The enrolment outlived the student."


def test_deleting_an_instructor_does_not_delete_their_subjects():
    """
    SET NULL, not CASCADE, and the distinction is deliberate: deleting an
    account must not delete the subjects that account taught.
    """
    with db_cursor(commit=True) as cursor:
        instructor_row_id = make_instructor(cursor)
        subject_id = make_subject(cursor, instructor_id=instructor_row_id)

    with db_cursor(commit=True) as cursor:
        cursor.execute("DELETE FROM instructors WHERE id = %s", (instructor_row_id,))

    with db_cursor(dictionary=True) as cursor:
        cursor.execute("SELECT id, instructor_id FROM subjects WHERE id = %s", (subject_id,))
        subject = cursor.fetchone()

    assert subject is not None, "Deleting an instructor deleted the subject they taught."
    assert subject["instructor_id"] is None


def test_attendance_cannot_reference_a_subject_that_does_not_exist():
    """
    The referential integrity migration 004 had to be rewritten twice to
    achieve. Its second draft left a row pointing at subject_id 0 and reported
    success (L11), so this asserts the constraint is real rather than declared.
    """
    import mysql.connector

    with db_cursor(commit=True) as cursor:
        make_student(cursor, STUDENT)

    with pytest.raises(mysql.connector.Error), db_cursor(commit=True) as cursor:
        make_attendance(cursor, STUDENT, 999999)


# ===========================================================================
# FS-5 - the dashboard counts things
#
# Total Students, Total Subjects, Today's Attendance and Active Sessions were
# hardcoded 0 in the template, and the route had no database access at all.
# ===========================================================================


def test_the_dashboard_counters_are_real_aggregates(client):
    with db_cursor(commit=True) as cursor:
        subject_id = make_subject(cursor)
        make_student(cursor, STUDENT)
        make_student(cursor, OTHER_STUDENT, name="Second Student")
        make_enrolment(cursor, STUDENT, subject_id)
        make_attendance(cursor, STUDENT, subject_id)

        cursor.execute(
            """
            INSERT INTO attendance_sessions
                (subject_id, session_date, started_at, started_by)
            VALUES (%s, CURDATE(), NOW(), %s)
            """,
            (subject_id, "int-test-admin"),
        )

    sign_in_as(client)
    body = client.get("/dashboard").get_data(as_text=True)

    # Two students, one subject, one attendance row today, one unclosed
    # session. Asserting on the rendered numbers rather than the query,
    # because FS-5 was a template printing literals over a route that never
    # asked the database anything.
    for label, expected in (
        ("students", 2),
        ("subjects", 1),
        ("attendance today", 1),
        ("active sessions", 1),
    ):
        assert str(expected) in body, f"Dashboard does not show {expected} for {label}"

    assert ">0<" not in body.replace(" ", ""), (
        "A hardcoded zero is still being rendered (FS-5)."
    )


# ===========================================================================
# FS-7 - the subject is chosen, not typed
#
# It was a free-text box, filled in twice per session, and a typo silently
# created an orphan session no report would find.
# ===========================================================================


def test_the_attendance_page_offers_the_subjects_that_exist(client):
    with db_cursor(commit=True) as cursor:
        make_subject(cursor, subject_code="INT-TEST-201", subject_name="Chosen From A List")

    sign_in_as(client)
    body = client.get("/attendance").get_data(as_text=True)

    assert "INT-TEST-201" in body
    assert "<select" in body.lower(), (
        "The subject is still a free-text field. A typo creates an orphan "
        "session that no report will find (FS-7)."
    )


def test_a_subject_that_does_not_exist_is_refused(client):
    """The dropdown is the usability half; this is the enforcement half."""
    sign_in_as(client)

    response = client.post("/end-attendance", data={"subject_id": "999999"})

    assert response.status_code == 404
    assert attendance_rows() == []
