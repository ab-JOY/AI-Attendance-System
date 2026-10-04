"""Reads and writes against the `students` table."""

from __future__ import annotations

# Where a student belongs, read through the joins (migration 009, DM-2).
#
# The four facts used to be four free-text columns on `students`. They are one
# foreign key now - `section_id` - and the department, program and year level
# are properties of the section it points at.
#
# ⚠️ **The aliases are the old column names, deliberately.** Templates, the
# mobile API and the class-list screen all read `college_department`,
# `program`, `year_level` and `section`, and keeping those keys is what let
# the storage change without touching any of them.
#
# ⚠️ **Never `SELECT s.*` beside these.** The legacy columns are still on the
# table until the contract migration (todo.md §7.7), under the same names, and
# a dictionary cursor would hand back whichever of the two came last.
ACADEMIC_COLUMNS = """
    s.section_id,
    sec.program_id,
    p.department_id,
    d.department_name AS college_department,
    p.program_code AS program,
    sec.year_level AS year_level,
    sec.section_name AS section
"""

# LEFT JOINs: a student with no section is *Unassigned*, not missing.
ACADEMIC_JOIN = """
    LEFT JOIN sections sec ON sec.id = s.section_id
    LEFT JOIN programs p ON p.id = sec.program_id
    LEFT JOIN departments d ON d.id = p.department_id
"""

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
#
# Ordered by department, program, year and section before name, so the list
# screens can draw one heading per section. Unassigned students sort last.
ROSTER_WITH_CLASS_COUNT = f"""
    SELECT s.student_id, s.name, {ACADEMIC_COLUMNS},
           COUNT(e.subject_id) AS classes
    FROM students s
    {ACADEMIC_JOIN}
    LEFT JOIN enrolments e ON e.student_id = s.student_id
    {{where}}
    GROUP BY s.student_id
    ORDER BY
        d.department_name IS NULL,
        d.department_name,
        p.program_code,
        sec.year_level,
        sec.section_name,
        s.name
"""


def roster(cursor, query=None, department_id=None, program_id=None,
           year_level=None, section_id=None):
    """
    Students with their placement and class count, narrowed by any filter.

    The WHERE clause is assembled, but only from fixed fragments - every value
    is a bound parameter, the same shape as `attendance._filtered_query()`.
    """
    conditions = []
    values = []

    if query:
        conditions.append("(s.student_id LIKE %s OR s.name LIKE %s)")
        values += [f"%{query}%", f"%{query}%"]

    if department_id is not None:
        conditions.append("p.department_id = %s")
        values.append(department_id)

    if program_id is not None:
        conditions.append("sec.program_id = %s")
        values.append(program_id)

    if year_level is not None:
        conditions.append("sec.year_level = %s")
        values.append(year_level)

    if section_id is not None:
        conditions.append("s.section_id = %s")
        values.append(section_id)

    where = "WHERE " + " AND ".join(conditions) if conditions else ""

    cursor.execute(ROSTER_WITH_CLASS_COUNT.format(where=where), tuple(values))

    return cursor.fetchall()


def all_students(cursor):
    """Every student, for the list screens, with their class count."""
    return roster(cursor)


def search(cursor, query):
    """Students whose ID or name contains `query`, with their class count."""
    return roster(cursor, query=query)


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
    cursor.execute(
        f"""
        SELECT s.student_id, s.name, {ACADEMIC_COLUMNS}
        FROM students s
        {ACADEMIC_JOIN}
        WHERE s.student_id = %s
        """,
        (student_id,)
    )

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

    `record['section_id']` is where they belong, already resolved by the
    caller through `academics.section_for()` in this same transaction. None is
    a student who is Unassigned, which the list screens say out loud.
    """
    cursor.execute(
        """
        INSERT INTO students (student_id, name, section_id, password)
        VALUES (%s, %s, %s, %s)
        """,
        (
            student_id,
            name,
            record.get('section_id'),
            record.get('password_hash'),
        ),
    )


def rename(cursor, student_id, name):
    cursor.execute(
        "UPDATE students SET name = %s WHERE student_id = %s",
        (name, student_id),
    )


def update_details(cursor, student_id, name, section_id):
    """
    Update the editable fields. Returns the affected row count.

    A rename is now one statement. Under `dataset/{student_id}_{name}` it was
    this plus an `os.rename()` under the same cursor, plus a compensating
    rename in the caller's `except` branch - see todo.md §7.5.
    """
    cursor.execute(
        "UPDATE students SET name = %s, section_id = %s WHERE student_id = %s",
        (name, section_id, student_id),
    )

    return cursor.rowcount


def update_details_and_password(cursor, student_id, name, section_id,
                                password_hash):
    """Update the editable fields and replace the student's password."""
    cursor.execute(
        """
        UPDATE students
        SET name = %s, section_id = %s, password = %s
        WHERE student_id = %s
        """,
        (name, section_id, password_hash, student_id),
    )

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
