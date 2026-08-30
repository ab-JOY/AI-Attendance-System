"""Reads and writes against the `instructors` table."""

from __future__ import annotations


def all_instructors(cursor):
    cursor.execute("""
        SELECT *
        FROM instructors
        ORDER BY fullname ASC
    """)

    return cursor.fetchall()


def search(cursor, query):
    cursor.execute("""
        SELECT *
        FROM instructors
        WHERE instructor_id LIKE %s
        OR fullname LIKE %s
        ORDER BY fullname ASC
    """, (
        f"%{query}%",
        f"%{query}%"
    ))

    return cursor.fetchall()


def get(cursor, instructor_id):
    cursor.execute(
        "SELECT * FROM instructors WHERE instructor_id=%s",
        (instructor_id,)
    )

    return cursor.fetchone()


def insert(cursor, instructor_id, fullname, password_hash):
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
            must_change_password
        )
        VALUES (%s,%s,%s,1)
    """, (
        instructor_id,
        fullname,
        password_hash
    ))


def update_name(cursor, instructor_id, fullname):
    """
    Rename without touching the password.

    The form marks the password field optional, and leaving it blank must not
    overwrite the stored hash with an empty string - which is why this is a
    separate statement rather than one UPDATE with a possibly-empty value.
    """
    cursor.execute("""
        UPDATE instructors
        SET fullname=%s
        WHERE instructor_id=%s
    """, (
        fullname,
        instructor_id
    ))


def update_name_and_password(cursor, instructor_id, fullname, password_hash):
    cursor.execute("""
        UPDATE instructors
        SET
            fullname=%s,
            password=%s,
            must_change_password=1
        WHERE instructor_id=%s
    """, (
        fullname,
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
