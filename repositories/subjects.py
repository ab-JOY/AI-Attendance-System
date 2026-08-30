"""Reads and writes against the `subjects` table."""

from __future__ import annotations

# The columns every subject *picker* needs: the attendance dropdown and the
# report filter. Written once because the two used to carry slightly different
# column lists for no reason anybody could state.
SELECTION_COLUMNS = "id, subject_code, subject_name, course, section, time_in"

# The same columns, qualified, for the one picker query that joins. Derived
# rather than written out a second time: a column added above and forgotten
# here would give the attendance dropdown a different shape from every other
# subject picker, which is the drift SELECTION_COLUMNS exists to prevent.
QUALIFIED_SELECTION_COLUMNS = ", ".join(
    f"s.{column.strip()}" for column in SELECTION_COLUMNS.split(",")
)


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


def for_selection_with_class_list_size(cursor):
    """
    `for_selection()`, plus how many students are on each offering's class list.

    So the attendance dropdown can mark the subjects on which a session would
    record nobody, **before** one is chosen. A subject with an empty class list
    is not broken - it is a roster nobody has filled in - but starting a session
    on one is guaranteed to refuse every face (FS-3) and write a register of
    nobody, and until now the first sign of that was a student standing in front
    of the camera being told "Not in this class".

    ⚠️ **`COUNT(e.subject_id)`, never `COUNT(*)`.** A LEFT JOIN with no match
    still produces one row of NULLs, so `COUNT(*)` reports **1** for a subject
    with nobody on it - the exact opposite of what this asks, and it would mark
    every empty class list as full. Same trap as the Classes column in
    `repositories/students.py` and the classless-student check in
    `scripts/preflight.py`, both of which say so in the same words.
    """
    cursor.execute(f"""
        SELECT {QUALIFIED_SELECTION_COLUMNS}, COUNT(e.subject_id) AS enrolled
        FROM subjects s
        LEFT JOIN enrolments e ON e.subject_id = s.id
        GROUP BY {QUALIFIED_SELECTION_COLUMNS}
        ORDER BY s.subject_code, s.section
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
