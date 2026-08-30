"""Reads and writes against the `subjects` table."""

from __future__ import annotations

# The columns every subject *picker* needs: the attendance dropdown and the
# report filter. Written once because the two used to carry slightly different
# column lists for no reason anybody could state.
SELECTION_COLUMNS = "id, subject_code, subject_name, course, section, time_in"


def all_subjects(cursor):
    """Every offering, newest first, for the management screen."""
    cursor.execute("SELECT * FROM subjects ORDER BY id DESC")
    return cursor.fetchall()


def for_selection(cursor):
    """
    Every offering, ordered for a dropdown.

    FS-7/US-4: the subject used to be typed by hand, twice - once to start a
    session and again to end it - and nothing checked either against this
    table, so a typo silently created a session no report could find. Ordered
    by code *and section* because two sections share one code and the operator
    has to be able to tell them apart.
    """
    cursor.execute(f"""
        SELECT {SELECTION_COLUMNS}
        FROM subjects
        ORDER BY subject_code, section
    """)

    return cursor.fetchall()


def for_report_filter(cursor):
    cursor.execute("""
        SELECT id, subject_code, subject_name, course, section
        FROM subjects
        ORDER BY subject_code, section
    """)

    return cursor.fetchall()


def get(cursor, subject_id):
    cursor.execute("SELECT * FROM subjects WHERE id=%s", (subject_id,))
    return cursor.fetchone()


def summary(cursor, subject_id):
    """The identifying fields of one offering, or None."""
    cursor.execute(
        f"""
        SELECT {SELECTION_COLUMNS}
        FROM subjects WHERE id = %s
        """,
        (subject_id,)
    )

    return cursor.fetchone()


def existing_ids(cursor, subject_ids):
    """
    Which of these offerings still exist, in the order they were given.

    ⚠️ **The placeholders are generated from the *count*, never from the
    values.** `IN (%s)` takes one parameter, so a list needs one marker per id;
    building that by interpolating the ids themselves would be the one piece of
    string-built SQL in the codebase. The ids are already ints by the time they
    arrive here, and they are still not spliced in.

    Used by enrolment (US-10): a subject can be deleted between the
    registration form and the end of a two-minute capture, and a foreign-key
    error at that point would roll back a student whose images are already on
    disk.
    """
    if not subject_ids:
        return []

    placeholders = ", ".join(["%s"] * len(subject_ids))

    cursor.execute(
        f"SELECT id FROM subjects WHERE id IN ({placeholders})",
        tuple(subject_ids),
    )

    found = {
        row["id"] if isinstance(row, dict) else row[0]
        for row in cursor.fetchall()
    }

    return [subject_id for subject_id in subject_ids if subject_id in found]


def code_of(cursor, subject_id):
    """`(id, subject_code)`, or None. Used when opening and closing a session."""
    cursor.execute(
        "SELECT id, subject_code FROM subjects WHERE id = %s",
        (subject_id,)
    )

    return cursor.fetchone()


def insert(cursor, subject_code, subject_name, instructor, day, course,
           section, time_in, time_out):
    cursor.execute("""
        INSERT INTO subjects
        (subject_code, subject_name, instructor, day, course, section, time_in, time_out)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """, (subject_code, subject_name, instructor, day, course, section, time_in, time_out))


def update(cursor, subject_id, subject_code, subject_name, instructor, day,
           course, section, time_in, time_out):
    cursor.execute("""
        UPDATE subjects
        SET subject_code=%s,
            subject_name=%s,
            instructor=%s,
            day=%s,
            course=%s,
            section=%s,
            time_in=%s,
            time_out=%s
        WHERE id=%s
    """, (subject_code, subject_name, instructor, day, course, section,
          time_in, time_out, subject_id))


def delete(cursor, subject_id):
    cursor.execute("DELETE FROM subjects WHERE id=%s", (subject_id,))
