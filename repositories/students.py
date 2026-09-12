"""Reads and writes against the `students` table."""

from __future__ import annotations

# ⚠️ **`classes` is not decoration on the list screens - it is US-10.**
#
# A student can be enrolled for *recognition* - row written, face captured,
# model retrained - and be in no class at all, because `enrolments` is written
# on a different screen. Nothing said so, so the first time anybody found out
# was a student standing in front of the camera being told "Not Enrolled".
# Counting the relation here is what lets the list say it beforehand.
#
# LEFT JOIN rather than a subquery per row, and `COUNT(e.subject_id)` rather
# than `COUNT(*)`: with no matching row the join supplies one NULL row, and
# `COUNT(*)` would count it as 1 - a student in no class would report one
# class, which is precisely the wrong answer to the question being asked.
ROSTER_WITH_CLASS_COUNT = """
    SELECT s.*, COUNT(e.subject_id) AS classes
    FROM students s
    LEFT JOIN enrolments e ON e.student_id = s.student_id
    {where}
    GROUP BY s.student_id
    ORDER BY s.name ASC
"""


def all_students(cursor):
    """Every student, for the list screens, with their class count."""
    cursor.execute(ROSTER_WITH_CLASS_COUNT.format(where=""))
    return cursor.fetchall()


def search(cursor, query):
    """Students whose ID or name contains `query`, with their class count."""
    cursor.execute(
        ROSTER_WITH_CLASS_COUNT.format(
            where="WHERE s.student_id LIKE %s OR s.name LIKE %s"
        ),
        (f"%{query}%", f"%{query}%"),
    )

    return cursor.fetchall()


def identity(cursor, student_id):
    """`(student_id, name)` for one student, or None."""
    cursor.execute("""
        SELECT
            student_id,
            name
        FROM students
        WHERE student_id = %s
    """, (student_id,))

    return cursor.fetchone()


def details(cursor, student_id):
    """The editable fields for one student, or None."""
    cursor.execute("""
        SELECT
            student_id,
            name,
            college_department,
            program,
            year_level,
            section
        FROM students
        WHERE student_id = %s
    """, (student_id,))

    return cursor.fetchone()


def exists(cursor, student_id):
    cursor.execute(
        "SELECT student_id FROM students WHERE student_id = %s",
        (student_id,),
    )

    return cursor.fetchone() is not None


def insert(cursor, student_id, name, record):
    """
    Create a student.

    ⚠️ **Called only after a complete dataset is on disk** (FS-9). The old
    enrolment route inserted first and captured afterwards, so a cancelled
    capture left a student with no images - which then failed every retrain.
    See infra/dataset_store.py.
    """
    cursor.execute(
        """
        INSERT INTO students
        (
            student_id,
            name,
            college_department,
            program,
            year_level,
            section,
            password
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            student_id,
            name,
            record.get('college_department', ''),
            record.get('program', ''),
            record.get('year_level') or None,
            record.get('section', ''),
            record.get('password_hash'),
        ),
    )


def rename(cursor, student_id, name):
    cursor.execute(
        "UPDATE students SET name = %s WHERE student_id = %s",
        (name, student_id),
    )


def update_details(cursor, student_id, name, college_department, program,
                   year_level, section):
    """
    Update the editable fields. Returns the affected row count.

    A rename is now one statement. Under `dataset/{student_id}_{name}` it was
    this plus an `os.rename()` under the same cursor, plus a compensating
    rename in the caller's `except` branch - see todo.md §7.5.
    """
    cursor.execute("""
        UPDATE students
        SET
            name = %s,
            college_department = %s,
            program = %s,
            year_level = %s,
            section = %s
        WHERE student_id = %s
    """, (
        name,
        college_department,
        program,
        year_level,
        section,
        student_id
    ))

    return cursor.rowcount


def update_details_and_password(cursor, student_id, name, college_department,
                                program, year_level, section, password_hash):
    """Update the editable fields and replace the student's password."""
    cursor.execute("""
        UPDATE students
        SET
            name = %s,
            college_department = %s,
            program = %s,
            year_level = %s,
            section = %s,
            password = %s
        WHERE student_id = %s
    """, (
        name,
        college_department,
        program,
        year_level,
        section,
        password_hash,
        student_id
    ))

    return cursor.rowcount


def delete(cursor, student_id):
    """
    Remove a student and their attendance.

    ⚠️ The attendance delete is redundant against the Phase 4 schema -
    `attendance.student_id` is `ON DELETE CASCADE` (RE-4) - and is kept
    deliberately. It is what makes this correct against a database created
    before migration 004 and never migrated, and it costs one statement
    against a table that is empty for the deleted student either way.
    """
    cursor.execute("""
        DELETE FROM attendance
        WHERE student_id = %s
    """, (student_id,))

    cursor.execute("""
        DELETE FROM students
        WHERE student_id = %s
    """, (student_id,))
