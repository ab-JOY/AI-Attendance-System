"""Reads and writes against the `instructors` table."""

from __future__ import annotations

# Every read names its columns, and `password` is not one of them (SE-17): the
# list and edit screens never need the hash, and `SELECT *` put it in every
# template context. Authentication reads it through repositories/credentials.py.
#
# The department is joined on rather than stored as text (migration 009, DM-3).
# LEFT JOIN: an instructor with no department is Unassigned, not missing.
INSTRUCTOR_COLUMNS = """
    i.id,
    i.instructor_id,
    i.fullname,
    i.must_change_password,
    i.department_id,
    d.department_code,
    d.department_name
"""

INSTRUCTOR_FROM = """
    FROM instructors i
    LEFT JOIN departments d ON d.id = i.department_id
"""

# By department, then name, so the list screens can draw one heading per
# department. Unassigned instructors sort last.
INSTRUCTOR_ORDER = """
    ORDER BY d.department_name IS NULL, d.department_name, i.fullname
"""


def roster(cursor, query=None, department_id=None):
    """
    Instructors with their department, narrowed by any filter.

    The WHERE clause is assembled, but only from fixed fragments - every value
    is a bound parameter.
    """
    conditions = []
    values = []

    if query:
        conditions.append("(i.instructor_id LIKE %s OR i.fullname LIKE %s)")
        values += [f"%{query}%", f"%{query}%"]

    if department_id is not None:
        conditions.append("i.department_id = %s")
        values.append(department_id)

    where = "WHERE " + " AND ".join(conditions) if conditions else ""

    cursor.execute(
        f"SELECT {INSTRUCTOR_COLUMNS} {INSTRUCTOR_FROM} {where} {INSTRUCTOR_ORDER}",
        tuple(values),
    )

    return cursor.fetchall()


def all_instructors(cursor):
    return roster(cursor)


def search(cursor, query):
    return roster(cursor, query=query)


def get(cursor, instructor_id):
    cursor.execute(
        f"""
        SELECT {INSTRUCTOR_COLUMNS}
        {INSTRUCTOR_FROM}
        WHERE i.instructor_id = %s
        """,
        (instructor_id,)
    )

    return cursor.fetchone()


def insert(cursor, instructor_id, fullname, password_hash, department_id=None):
    """
    Create an instructor account.

    `must_change_password` is 1 and not a parameter, deliberately (SE-1): an
    administrator who types someone else's password knows it, so the account
    can reach nothing but the change-password screen until the instructor
    replaces it with one only they know.
    """
    cursor.execute("""
        INSERT INTO instructors
        (
            instructor_id,
            fullname,
            password,
            must_change_password,
            department_id
        )
        VALUES (%s,%s,%s,1,%s)
    """, (
        instructor_id,
        fullname,
        password_hash,
        department_id
    ))


def update_details(cursor, instructor_id, fullname, department_id):
    """
    Rename and re-assign without touching the password.

    The form marks the password field optional, and leaving it blank must not
    overwrite the stored hash with an empty string - which is why this is a
    separate statement rather than one UPDATE with a possibly-empty value.
    """
    cursor.execute("""
        UPDATE instructors
        SET fullname=%s,
            department_id=%s
        WHERE instructor_id=%s
    """, (
        fullname,
        department_id,
        instructor_id
    ))


def update_details_and_password(cursor, instructor_id, fullname,
                                department_id, password_hash):
    cursor.execute("""
        UPDATE instructors
        SET
            fullname=%s,
            department_id=%s,
            password=%s,
            must_change_password=1
        WHERE instructor_id=%s
    """, (
        fullname,
        department_id,
        password_hash,
        instructor_id
    ))


def delete(cursor, instructor_id):
    """
    Remove an instructor account.

    `subjects.instructor_id` is `ON DELETE SET NULL` (RE-4), so deleting the
    account does not delete the offerings they taught.
    """
    cursor.execute(
        "DELETE FROM instructors WHERE instructor_id=%s",
        (instructor_id,)
    )
