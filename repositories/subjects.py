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


# Who teaches an offering, as every screen shows it: the linked account's name,
# falling back to the free text the subject form used to write (DM-4).
#
# `subjects.instructor_id` is the fact. `subjects.instructor` is what was typed
# before the form offered accounts to choose from, and is kept only for an
# offering whose instructor has no account - nothing writes it any more.
INSTRUCTOR_NAME = "COALESCE(i.fullname, s.instructor)"


def _owned_by(instructor_id, prefix="WHERE"):
    """
    `(sql, values)` narrowing a subject query to one instructor's offerings.

    None means unrestricted - an administrator. Anything else is an
    `instructors.id`, and 0 (a signed-in instructor the session cannot
    identify) matches nothing, which is the point: the scope fails closed.
    """
    if instructor_id is None:
        return "", ()

    return f"{prefix} s.instructor_id = %s", (instructor_id,)


def all_subjects(cursor):
    """Every offering, newest first, for the management screen."""
    cursor.execute(f"""
        SELECT s.*, {INSTRUCTOR_NAME} AS instructor_name
        FROM subjects s
        LEFT JOIN instructors i ON i.id = s.instructor_id
        ORDER BY s.id DESC
    """)
    return cursor.fetchall()


def is_taught_by(cursor, subject_id, instructor_id):
    """
    Whether this offering is assigned to this instructor account (DM-5).

    The check behind every instructor-scoped write: starting a session, ending
    one, correcting a record. Hiding another instructor's subject from a
    dropdown is a convenience; this is what refuses the request that names it
    anyway.
    """
    cursor.execute(
        "SELECT id FROM subjects WHERE id = %s AND instructor_id = %s",
        (subject_id, instructor_id),
    )

    return cursor.fetchone() is not None


def for_selection(cursor, instructor_id=None):
    """
    Every offering, ordered for a dropdown - or one instructor's (DM-5).

    FS-7/US-4: the subject used to be typed by hand, twice - once to start a
    session and again to end it - and nothing checked either against this
    table, so a typo silently created a session no report could find. Ordered
    by code *and section* because two sections share one code and the operator
    has to be able to tell them apart.
    """
    owned, values = _owned_by(instructor_id)

    cursor.execute(f"""
        SELECT {QUALIFIED_SELECTION_COLUMNS}
        FROM subjects s
        {owned}
        ORDER BY s.subject_code, s.section
    """, values)

    return cursor.fetchall()


def for_selection_with_class_list_size(cursor, instructor_id=None):
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
    owned, values = _owned_by(instructor_id)

    cursor.execute(f"""
        SELECT {QUALIFIED_SELECTION_COLUMNS}, COUNT(e.subject_id) AS enrolled
        FROM subjects s
        LEFT JOIN enrolments e ON e.subject_id = s.id
        {owned}
        GROUP BY {QUALIFIED_SELECTION_COLUMNS}
        ORDER BY s.subject_code, s.section
    """, values)

    return cursor.fetchall()


def for_report_filter(cursor, instructor_id=None):
    owned, values = _owned_by(instructor_id)

    cursor.execute(f"""
        SELECT s.id, s.subject_code, s.subject_name, s.course, s.section
        FROM subjects s
        {owned}
        ORDER BY s.subject_code, s.section
    """, values)

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


def insert(cursor, subject_code, subject_name, instructor_id, day, course,
           section, time_in, time_out):
    """
    Create an offering.

    `instructor_id` is an `instructors.id` or None. It used to be a typed name
    written to `subjects.instructor`, which linked the offering to nobody - so
    nothing could be scoped to the instructor who teaches it (DM-4).
    """
    cursor.execute("""
        INSERT INTO subjects
        (subject_code, subject_name, instructor_id, day, course, section, time_in, time_out)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """, (subject_code, subject_name, instructor_id, day, course, section, time_in, time_out))


def update(cursor, subject_id, subject_code, subject_name, instructor_id, day,
           course, section, time_in, time_out):
    """
    Update an offering.

    ⚠️ `subjects.instructor` - the legacy typed name - is left alone. It is
    what the screens fall back to when no account is chosen, and blanking it
    here would erase the only record of who an unlinked offering belonged to.
    """
    cursor.execute("""
        UPDATE subjects
        SET subject_code=%s,
            subject_name=%s,
            instructor_id=%s,
            day=%s,
            course=%s,
            section=%s,
            time_in=%s,
            time_out=%s
        WHERE id=%s
    """, (subject_code, subject_name, instructor_id, day, course, section,
          time_in, time_out, subject_id))


def delete(cursor, subject_id):
    cursor.execute("DELETE FROM subjects WHERE id=%s", (subject_id,))
