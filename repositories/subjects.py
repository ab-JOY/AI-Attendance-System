"""Reads and writes against the `subjects` table."""

from __future__ import annotations


# Which section an offering is taught to (migration 010, DM-7).
#
# `subjects.section_id` is the fact, and the program and year level are the
# section's - the same shape as a student's placement, so "which subjects are
# for this program" is a comparison of two keys rather than of two typed
# strings.
#
# ⚠️ **`course` and `section` keep their names, deliberately.** They are the
# program code and the section name read through the joins, falling back to
# what was typed before the form offered a list - which is all an offering
# migration 010 could not link has. Every reader used those two keys already.
#
# ⚠️ **Never `SELECT s.*` beside these**, for the reason
# `students.ACADEMIC_COLUMNS` gives: the typed columns are still on the table
# under the same names, and a dictionary cursor keeps whichever came last.
#
# `osec` / `op`, not `sec` / `p`: the register joins an offering's section and
# a student's in one query, and the student's already has those aliases.
def offering_join(subject="s"):
    """The LEFT JOINs that reach an offering's section and program."""
    return f"""
        LEFT JOIN sections osec ON osec.id = {subject}.section_id
        LEFT JOIN programs op ON op.id = osec.program_id
    """


def offering_course(subject="s"):
    return f"COALESCE(op.program_code, {subject}.course)"


def offering_section(subject="s"):
    return f"COALESCE(osec.section_name, {subject}.section)"


OFFERING_JOIN = offering_join()

# The columns every subject *picker* needs: the attendance dropdown, the
# report filter, the Classes list on the registration form. Written once
# because the pickers used to carry slightly different column lists for no
# reason anybody could state.
SELECTION_COLUMNS = f"""
    s.id,
    s.subject_code,
    s.subject_name,
    {offering_course()} AS course,
    {offering_section()} AS section,
    s.time_in,
    s.section_id,
    osec.program_id,
    osec.year_level
"""

# What a query that counts across a join groups by. The three primary keys,
# not the column list: every selected column depends on one of them, and
# MySQL 8's ONLY_FULL_GROUP_BY accepts nothing less from an outer join.
SELECTION_GROUP = "s.id, osec.id, op.id"


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
    """
    Every offering, for the management screen: one subject's sections together.

    Ordered by subject, then by where each section sits, so the screen can
    group the offerings of one subject under one heading. It used to be newest
    first, which scattered the sections of one subject across the list.
    """
    cursor.execute(f"""
        SELECT {SELECTION_COLUMNS}, s.instructor_id, s.day, s.time_out,
               {INSTRUCTOR_NAME} AS instructor_name
        FROM subjects s
        {OFFERING_JOIN}
        LEFT JOIN instructors i ON i.id = s.instructor_id
        ORDER BY s.subject_code, s.subject_name, course, osec.year_level,
                 section, s.id
    """)
    return cursor.fetchall()


def is_offered_to(cursor, subject_code, section_id, excluding=None):
    """
    Whether an offering of this subject code is already taught to this section.

    `excluding` is an offering to leave out - the one being edited. The
    comparison is the column's collation, case-insensitive, so `cs101` is
    `CS101`, as it is everywhere else this code is matched.
    """
    cursor.execute(
        """
        SELECT id FROM subjects
        WHERE subject_code = %s AND section_id = %s AND id <> %s
        LIMIT 1
        """,
        (subject_code, section_id, excluding or 0),
    )

    return cursor.fetchone() is not None


def name_for_code(cursor, subject_code, excluding=None):
    """
    The name the other offerings of this code carry, or None if there are none.

    One subject, one name (DM-11): the routes refuse a second spelling, so on a
    consistent table every row of a code agrees and any one of them answers.
    """
    cursor.execute(
        """
        SELECT subject_name FROM subjects
        WHERE subject_code = %s AND id <> %s
        ORDER BY id
        LIMIT 1
        """,
        (subject_code, excluding or 0),
    )

    row = cursor.fetchone()

    if row is None:
        return None

    return row["subject_name"] if isinstance(row, dict) else row[0]


def rename_code(cursor, subject_code, subject_name, excluding=None):
    """Give every other offering of this code this name. Returns how many changed."""
    cursor.execute(
        """
        UPDATE subjects SET subject_name = %s
        WHERE subject_code = %s AND id <> %s
          -- Binary: the collation is case-insensitive, and `pe 1` -> `PE 1` is a rename.
          AND CAST(subject_name AS BINARY) <> CAST(%s AS BINARY)
        """,
        (subject_name, subject_code, excluding or 0, subject_name),
    )

    return cursor.rowcount


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
        SELECT {SELECTION_COLUMNS}
        FROM subjects s
        {OFFERING_JOIN}
        {owned}
        ORDER BY s.subject_code, osec.year_level, section
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

    Also carries the schedule (`day`, `time_out`) for the instructor's My
    Classes page. Both depend on `s.id`, so the GROUP BY still accepts them.
    """
    owned, values = _owned_by(instructor_id)

    cursor.execute(f"""
        SELECT {SELECTION_COLUMNS}, s.day, s.time_out,
               COUNT(e.subject_id) AS enrolled
        FROM subjects s
        {OFFERING_JOIN}
        LEFT JOIN enrolments e ON e.subject_id = s.id
        {owned}
        GROUP BY {SELECTION_GROUP}
        ORDER BY s.subject_code, osec.year_level, section
    """, values)

    return cursor.fetchall()


def for_report_filter(cursor, instructor_id=None):
    owned, values = _owned_by(instructor_id)

    cursor.execute(f"""
        SELECT {SELECTION_COLUMNS}
        FROM subjects s
        {OFFERING_JOIN}
        {owned}
        ORDER BY s.subject_code, osec.year_level, section
    """, values)

    return cursor.fetchall()


def get(cursor, subject_id):
    """
    One offering as the edit form needs it, or None.

    `typed_course` and `typed_section` are the legacy text, under their own
    names: the form shows them for an offering that is not linked to a section
    yet, so whoever links it can see what was meant.
    """
    cursor.execute(
        f"""
        SELECT {SELECTION_COLUMNS}, s.instructor, s.instructor_id, s.day,
               s.time_out, s.course AS typed_course, s.section AS typed_section
        FROM subjects s
        {OFFERING_JOIN}
        WHERE s.id = %s
        """,
        (subject_id,)
    )

    return cursor.fetchone()


def summary(cursor, subject_id):
    """The identifying fields of one offering, or None."""
    cursor.execute(
        f"""
        SELECT {SELECTION_COLUMNS}
        FROM subjects s
        {OFFERING_JOIN}
        WHERE s.id = %s
        """,
        (subject_id,)
    )

    return cursor.fetchone()


def existing_ids(cursor, subject_ids, program_id=None):
    """
    Which of these offerings still exist, in the order they were given - and,
    with `program_id`, are offered to that program (DM-7).

    ⚠️ **The placeholders are generated from the *count*, never from the
    values.** `IN (%s)` takes one parameter, so a list needs one marker per id;
    building that by interpolating the ids themselves would be the one piece of
    string-built SQL in the codebase. The ids are already ints by the time they
    arrive here, and they are still not spliced in.

    Used by enrolment (US-10): a subject can be deleted between the
    registration form and the end of a two-minute capture, and a foreign-key
    error at that point would roll back a student whose images are already on
    disk.

    ⚠️ **The program check is the guard; the filtered Classes list is the
    convenience.** The form only shows the chosen program's subjects, but the
    ids come back from a browser. An offering linked to no section belongs to
    no program, so it is offered to every one - refusing it would make a
    subject migration 010 could not link impossible to enrol into.
    """
    if not subject_ids:
        return []

    placeholders = ", ".join(["%s"] * len(subject_ids))
    offered = ""
    values = tuple(subject_ids)

    if program_id is not None:
        offered = "AND (s.section_id IS NULL OR osec.program_id = %s)"
        values += (program_id,)

    cursor.execute(
        f"""
        SELECT s.id
        FROM subjects s
        LEFT JOIN sections osec ON osec.id = s.section_id
        WHERE s.id IN ({placeholders})
        {offered}
        """,
        values,
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


def meeting_day(cursor, subject_id):
    """The stored `subjects.day` text, or None for NULL or no such subject."""
    cursor.execute("SELECT day FROM subjects WHERE id = %s", (subject_id,))

    row = cursor.fetchone()

    if row is None:
        return None

    return row['day'] if isinstance(row, dict) else row[0]


def insert(cursor, subject_code, subject_name, instructor_id, day,
           section_id, time_in, time_out):
    """
    Create an offering.

    `instructor_id` is an `instructors.id` or None. It used to be a typed name
    written to `subjects.instructor`, which linked the offering to nobody - so
    nothing could be scoped to the instructor who teaches it (DM-4).

    `section_id` is the section it is taught to, already resolved by the caller
    through `academics.section_for()` in this same transaction (DM-7). It used
    to be a typed course and a typed section, which linked it to no program.
    """
    cursor.execute("""
        INSERT INTO subjects
        (subject_code, subject_name, instructor_id, day, section_id, time_in, time_out)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, (subject_code, subject_name, instructor_id, day, section_id, time_in, time_out))


def update(cursor, subject_id, subject_code, subject_name, instructor_id, day,
           section_id, time_in, time_out):
    """
    Update an offering.

    ⚠️ `subjects.instructor`, `course` and `section` - the legacy typed text -
    are left alone. Nothing reads them for an offering that has an account and
    a section, and blanking them here would erase the only record of what an
    unlinked offering was typed as.
    """
    cursor.execute("""
        UPDATE subjects
        SET subject_code=%s,
            subject_name=%s,
            instructor_id=%s,
            day=%s,
            section_id=%s,
            time_in=%s,
            time_out=%s
        WHERE id=%s
    """, (subject_code, subject_name, instructor_id, day, section_id,
          time_in, time_out, subject_id))


def delete(cursor, subject_id):
    cursor.execute("DELETE FROM subjects WHERE id=%s", (subject_id,))
