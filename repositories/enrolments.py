"""
The `enrolments` join table - who is in which class (FS-3).

This is the relation whose absence made `/end-attendance` mark *every student
in the database* absent for *every subject*: with `CS401` selected, an
unrelated test student was reported Absent for it. Migration 002 added the
table; the Class List screen fills it; `save_attendance()` and the absent
register both scope themselves by it.
"""

from __future__ import annotations

# The student columns the class-list screen shows on both sides of the
# transfer. One string because the two queries below have to agree - a column
# in one list and not the other makes the two tables render differently for no
# stated reason.
ROSTER_COLUMNS = "s.student_id, s.name, s.program, s.year_level, s.section"


def enrolled_in(cursor, subject_id):
    """Everyone taking this offering."""
    cursor.execute(
        f"""
        SELECT {ROSTER_COLUMNS}
        FROM enrolments e
        JOIN students s ON s.student_id = e.student_id
        WHERE e.subject_id = %s
        ORDER BY s.name ASC
        """,
        (subject_id,)
    )

    return cursor.fetchall()


def not_enrolled_in(cursor, subject_id):
    """Everyone who could be added to it."""
    cursor.execute(
        f"""
        SELECT {ROSTER_COLUMNS}
        FROM students s
        WHERE NOT EXISTS (
            SELECT 1 FROM enrolments e
            WHERE e.student_id = s.student_id
            AND e.subject_id = %s
        )
        ORDER BY s.name ASC
        """,
        (subject_id,)
    )

    return cursor.fetchall()


def enrol(cursor, student_id, subject_id):
    """
    Add one student to one offering.

    The primary key already forbids a duplicate; ON DUPLICATE KEY makes a
    double-submitted form a no-op rather than a 500.
    """
    cursor.execute(
        """
        INSERT INTO enrolments (student_id, subject_id)
        VALUES (%s, %s)
        ON DUPLICATE KEY UPDATE student_id = student_id
        """,
        (student_id, subject_id)
    )


def enrol_many(cursor, student_id, subject_ids):
    """
    Put one student into several offerings at once (US-10).

    ⚠️ **Called inside the same transaction as the student row**, so an
    enrolment that names subjects either produces a student *in* those classes
    or produces nothing at all. A student row that committed while its class
    list did not is the exact state US-10 is about - recognised by the model,
    refused by the register - and it must not be reachable by a partial write.

    Same `ON DUPLICATE KEY` as `enrol()`: re-submitting is a no-op, not a 500.
    """
    if not subject_ids:
        return

    cursor.executemany(
        """
        INSERT INTO enrolments (student_id, subject_id)
        VALUES (%s, %s)
        ON DUPLICATE KEY UPDATE student_id = student_id
        """,
        [(student_id, subject_id) for subject_id in subject_ids]
    )


def count_for_student(cursor, student_id):
    """How many offerings this student is in. Zero is the interesting answer."""
    cursor.execute(
        "SELECT COUNT(*) AS classes FROM enrolments WHERE student_id = %s",
        (student_id,)
    )

    row = cursor.fetchone()

    # dictionary=True everywhere this is called, but a plain cursor returns a
    # tuple and silently indexing [0] on a dict would give a KeyError far from
    # here.
    return row["classes"] if isinstance(row, dict) else row[0]


def count_for_subject(cursor, subject_id):
    """
    How many students are on this offering's class list. Zero is the answer
    that matters.

    The mirror of `count_for_student()`, and it exists for the same reason at
    the other end of the relation: a subject with an empty class list is one
    where recognition will refuse **every** face with "Not in this class"
    (FS-3, deliberately) and ending the session writes a register of nobody.
    Nothing said so before the session was over.

    ⚠️ `COUNT(*)` over `enrolments` alone is correct here precisely because
    there is no LEFT JOIN - every row is a real enrolment. The classless-subject
    query in `subjects.for_selection()` cannot use it, and says why.
    """
    cursor.execute(
        "SELECT COUNT(*) AS enrolled FROM enrolments WHERE subject_id = %s",
        (subject_id,)
    )

    row = cursor.fetchone()

    # Same guard as count_for_student: a plain cursor returns a tuple, and
    # indexing [0] on a dict raises a KeyError a long way from here.
    return row["enrolled"] if isinstance(row, dict) else row[0]


def unenrol(cursor, student_id, subject_id):
    """
    Remove one student from one offering.

    ⚠️ This does not delete their attendance. Removing somebody from a class
    list is a statement about the future, and deleting the record of days they
    attended would be a quiet rewriting of history. The attendance rows keep
    their foreign key to the subject and remain in reports.
    """
    cursor.execute(
        "DELETE FROM enrolments WHERE student_id = %s AND subject_id = %s",
        (student_id, subject_id)
    )
