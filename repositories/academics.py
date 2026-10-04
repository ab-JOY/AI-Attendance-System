"""
Departments, programs and sections - where a student or instructor belongs.

The hierarchy is department -> program -> section, and a section carries its
year level: "BSCS, 4th year, section B" is one `sections` row. A student points
at a section and an instructor at a department; everything above is read
through the joins, so the facts cannot disagree with each other (DM-2).

Before migration 009 these were four free-text columns on `students`, filled
from dropdowns hardcoded in two templates and a mobile screen, and nothing on
`instructors` at all.
"""

from __future__ import annotations

# `sections.section_name` is VARCHAR(100). Checked by callers rather than left
# to the column: this server has no STRICT_TRANS_TABLES, so an over-long value
# is silently truncated on an INSERT that reports success (lessons.md L11).
MAX_SECTION_NAME = 100

# What the forms offer. A bound on the value, not a claim about any curriculum:
# without one a typed 40 becomes a section nobody can find again.
YEAR_LEVELS = (1, 2, 3, 4, 5, 6)


def departments(cursor):
    cursor.execute("""
        SELECT id, department_code, department_name
        FROM departments
        ORDER BY department_name
    """)

    return cursor.fetchall()


def programs(cursor):
    """Every program with its department, ordered for a grouped dropdown."""
    cursor.execute("""
        SELECT
            p.id,
            p.program_code,
            p.program_name,
            p.department_id,
            d.department_code,
            d.department_name
        FROM programs p
        JOIN departments d ON d.id = p.department_id
        ORDER BY d.department_name, p.program_code
    """)

    return cursor.fetchall()


def sections(cursor):
    """Every section that exists, for the list filters."""
    cursor.execute("""
        SELECT
            sec.id,
            sec.program_id,
            sec.year_level,
            sec.section_name,
            p.program_code
        FROM sections sec
        JOIN programs p ON p.id = sec.program_id
        ORDER BY p.program_code, sec.year_level, sec.section_name
    """)

    return cursor.fetchall()


def placements(cursor, instructor_id=None):
    """
    Every section that has somebody to report on, with its program and
    department - the choices the report filters offer.

    With `instructor_id`, only the sections of students on that instructor's
    class lists: an instructor filters by the departments, programs, years and
    sections they actually teach, and is not shown the rest of the school.
    None is an administrator, who is offered every section.

    One row per section, ordered by department, program, year and name. The
    department, program and year lists are derived from these rows by the
    caller, so the four dropdowns can never offer a combination that does not
    exist.
    """
    taught = ""
    values = ()

    if instructor_id is not None:
        taught = """
            JOIN students st ON st.section_id = sec.id
            JOIN enrolments e ON e.student_id = st.student_id
            JOIN subjects sub ON sub.id = e.subject_id
            WHERE sub.instructor_id = %s
        """
        values = (instructor_id,)

    cursor.execute(
        f"""
        SELECT DISTINCT
            d.id AS department_id,
            d.department_code,
            d.department_name,
            p.id AS program_id,
            p.program_code,
            sec.id AS section_id,
            sec.year_level,
            sec.section_name
        FROM sections sec
        JOIN programs p ON p.id = sec.program_id
        JOIN departments d ON d.id = p.department_id
        {taught}
        ORDER BY d.department_name, p.program_code, sec.year_level,
                 sec.section_name
        """,
        values,
    )

    return cursor.fetchall()


def _first_column(row):
    # dictionary=True nearly everywhere, but a plain cursor returns a tuple -
    # the same guard as repositories/enrolments.py.
    if row is None:
        return None

    return next(iter(row.values())) if isinstance(row, dict) else row[0]


def program_exists(cursor, program_id):
    cursor.execute("SELECT id FROM programs WHERE id = %s", (program_id,))

    return cursor.fetchone() is not None


def program_id_for_code(cursor, program_code):
    """The program with this code, or None. The mobile app sends the code."""
    cursor.execute(
        "SELECT id FROM programs WHERE program_code = %s", (program_code,)
    )

    return _first_column(cursor.fetchone())


def department_exists(cursor, department_id):
    cursor.execute("SELECT id FROM departments WHERE id = %s", (department_id,))

    return cursor.fetchone() is not None


def section_for(cursor, program_id, year_level, section_name):
    """
    The id of this program's section for this year, creating it if it is new.

    Returns None when the program does not exist, so a caller holding a
    program id that came from a browser gets "unassigned" rather than a
    foreign-key error half way through writing a student.

    One statement rather than a SELECT followed by an INSERT (RE-3): the unique
    key on (program_id, year_level, section_name) decides, and
    `LAST_INSERT_ID(id)` makes `lastrowid` the existing row's id on a
    duplicate. The key's collation is case-insensitive, so `4b` finds `4B`.
    """
    if not program_exists(cursor, program_id):
        return None

    cursor.execute(
        """
        INSERT INTO sections (program_id, year_level, section_name)
        VALUES (%s, %s, %s)
        ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(id)
        """,
        (program_id, year_level, section_name),
    )

    return cursor.lastrowid
