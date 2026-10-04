"""
The register: `attendance`, `attendance_sessions` and `attendance_audit`.

Three findings live in this file's statements and each is worth knowing before
editing one.

**FS-3 / FS-4 - `register_for()`.** Absence used to be computed in memory over
*every student in the database* and never written down, so `/reports` counted
Absent rows in a table nothing wrote Absent rows into and reported 0 for ever.
The register is one LEFT JOIN from `enrolments`, and the absentees are the rows
where the join found nothing.

**FS-10 - the correction pair.** `set_status()` and `record_correction()` are
two functions on purpose and must be called inside **one** `db_cursor` block,
which is one transaction. There is no path that changes a biometric
determination without recording who did it, when, from what, to what and why -
and that is structural rather than a thing the next person has to remember.

**RE-3 - no read-then-write.** `save_attendance()` in recognize_face.py writes
the Present rows through `INSERT ... ON DUPLICATE KEY UPDATE` against a UNIQUE
index, not a SELECT followed by an INSERT. `insert_absences()` below does the
same thing for the same reason.
"""

from __future__ import annotations

from repositories.students import ACADEMIC_JOIN

# The register row as every screen wants to see it: the *joined* student name,
# never a copy stored on the attendance row. There used to be a
# `student_name` column here - a copy taken at write time that `update_student`
# never refreshed - so a renamed student appeared under their old name for
# ever.
REGISTER_COLUMNS = """
    a.id,
    a.attendance_date,
    a.student_id,
    s.name AS student_name,
    sub.subject_code,
    sub.subject_name,
    sub.section,
    COALESCE(i.fullname, sub.instructor) AS instructor,
    a.time_in,
    a.status,
    s.section_id AS student_section_id,
    d.department_code AS department_code,
    d.department_name AS department,
    p.program_code AS program,
    sec.year_level AS year_level,
    sec.section_name AS student_section
"""

# LEFT JOIN to `instructors`: an offering with no linked account still has a
# register, and an inner join would drop its rows from every report.
#
# The student's placement comes through `students.ACADEMIC_JOIN` - the same
# LEFT JOINs the student lists use, so the register and the roster cannot
# disagree about where somebody belongs. `section` above is the *offering's*
# typed section; `student_section` is the student's own.
REGISTER_JOIN = f"""
    FROM attendance a
    JOIN students s ON s.student_id = a.student_id
    JOIN subjects sub ON sub.id = a.subject_id
    LEFT JOIN instructors i ON i.id = sub.instructor_id
    {ACADEMIC_JOIN}
"""

# The four ways a report can be narrowed by where the *student* belongs, and
# the column each compares. The keys are the keyword names the functions below
# take and the query-string names /reports reads.
#
# A fixed mapping, so the WHERE clause is still assembled only from literals
# written in this file - the value is always a bound parameter.
PLACEMENT_FILTERS = {
    "department_id": "p.department_id",
    "program_id": "sec.program_id",
    "year_level": "sec.year_level",
    "section_id": "s.section_id",
}

# How the register can be ordered. An allowlist for the same reason: `sort`
# arrives in a query string, and an ORDER BY cannot be a bound parameter, so
# the request chooses a *key* and the SQL comes from here.
#
# `placement` puts unplaced students last and keeps each section together, so
# the screen can draw one heading per section.
DEFAULT_SORT = "date"

SORT_ORDERS = {
    "date": "a.attendance_date DESC, a.time_in DESC",
    "placement": (
        "d.department_name IS NULL, d.department_name, p.program_code, "
        "sec.year_level, sec.section_name, s.name, a.attendance_date DESC"
    ),
}


def _placement_conditions(placement):
    """
    `(sql, values)` for the placement filters that are set.

    Unknown keys raise rather than being ignored: a filter that silently does
    nothing returns the whole register under a heading that says it is one
    section's.
    """
    sql = ""
    values = []

    for key, value in placement.items():
        column = PLACEMENT_FILTERS[key]

        if value is not None:
            sql += f" AND {column} = %s"
            values.append(value)

    return sql, values


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


def counters(cursor, instructor_id=None):
    """
    The four dashboard numbers (FS-5), in one round trip.

    They were hardcoded `0` in the template and the route had no database
    access at all.

    "Active sessions" counts today's `attendance_sessions` rows with no
    `ended_at`. That includes sessions started and never ended as well as one
    running right now, which is honest: an unclosed session is a real thing
    that happened and the operator should see it.

    With `instructor_id`, the same four numbers for that instructor's own
    offerings (DM-5): the students on their class lists, their subjects,
    today's marks in them, and their open sessions. A student in two of their
    classes is one student, hence the DISTINCT.
    """
    if instructor_id is None:
        cursor.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM students) AS students,
                (SELECT COUNT(*) FROM subjects) AS subjects,
                (SELECT COUNT(*) FROM attendance
                 WHERE attendance_date = CURDATE()) AS attendance_today,
                (SELECT COUNT(*) FROM attendance_sessions
                 WHERE session_date = CURDATE()
                 AND ended_at IS NULL) AS active_sessions
            """
        )

        return cursor.fetchone()

    cursor.execute(
        """
        SELECT
            (SELECT COUNT(DISTINCT e.student_id)
             FROM enrolments e
             JOIN subjects sub ON sub.id = e.subject_id
             WHERE sub.instructor_id = %s) AS students,
            (SELECT COUNT(*) FROM subjects
             WHERE instructor_id = %s) AS subjects,
            (SELECT COUNT(*)
             FROM attendance a
             JOIN subjects sub ON sub.id = a.subject_id
             WHERE a.attendance_date = CURDATE()
             AND sub.instructor_id = %s) AS attendance_today,
            (SELECT COUNT(*)
             FROM attendance_sessions ses
             JOIN subjects sub ON sub.id = ses.subject_id
             WHERE ses.session_date = CURDATE()
             AND ses.ended_at IS NULL
             AND sub.instructor_id = %s) AS active_sessions
        """,
        (instructor_id,) * 4,
    )

    return cursor.fetchone()


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def open_session(cursor, subject_id, started_by):
    """
    Record that a class met, and return the new session's id.

    Written *before* the camera opens, so a session that starts and is never
    ended is still a recorded fact. Absence is a fact about a session rather
    than about a date: without this row, "the class never met" and "the class
    met and nobody was recognised" are the same picture.
    """
    cursor.execute(
        """
        INSERT INTO attendance_sessions
            (subject_id, session_date, started_at, started_by)
        VALUES (%s, CURDATE(), NOW(), %s)
        """,
        (subject_id, started_by)
    )

    return cursor.lastrowid


def open_sessions_today(cursor):
    """
    Today's sessions that were started and never ended, oldest first.

    ⚠️ **This is what makes /end-attendance's subject check survive the
    browser (FS-16).** The check used to compare the submitted subject against
    the *in-memory* recognition session - and the browser stopped the camera
    before submitting the form, which sets that subject to None, so the check
    was skipped and the form was trusted. The register was then written for a
    class that was never held.

    A row here is the same fact, written down: `open_session()` records it
    before the camera opens, precisely so that a session is a recorded fact
    rather than a process's memory of one. It therefore survives a stop, a
    restart, and a second operator's browser, which is what the in-memory
    subject cannot do.

    `subject_id` comes back with the id so the caller can name the class it is
    refusing to end, rather than saying only that something is open.
    """
    cursor.execute(
        """
        SELECT id, subject_id
        FROM attendance_sessions
        WHERE session_date = CURDATE()
        AND ended_at IS NULL
        ORDER BY id ASC
        """
    )

    return cursor.fetchall()


def close_session(cursor, session_row_id):
    """
    Stamp `ended_at`, unless it is already stamped.

    `AND ended_at IS NULL` so ending a session twice does not move the first
    end time - the operator pressing the button again is not new information.
    """
    cursor.execute(
        """
        UPDATE attendance_sessions
        SET ended_at = NOW()
        WHERE id = %s AND ended_at IS NULL
        """,
        (session_row_id,)
    )


# ---------------------------------------------------------------------------
# The register
# ---------------------------------------------------------------------------


def register_for(cursor, subject_id):
    """
    Everyone enrolled in this offering, with today's attendance if any.

    A LEFT JOIN rather than two queries and a set difference in Python: the
    absentees are exactly the rows where `status` came back NULL.
    """
    cursor.execute(
        """
        SELECT s.student_id, s.name, a.status, a.time_in
        FROM enrolments e
        JOIN students s ON s.student_id = e.student_id
        LEFT JOIN attendance a
            ON a.student_id = s.student_id
            AND a.subject_id = e.subject_id
            AND a.attendance_date = CURDATE()
        WHERE e.subject_id = %s
        ORDER BY s.name ASC
        """,
        (subject_id,)
    )

    return cursor.fetchall()


def insert_absences(cursor, rows):
    """
    Write the Absent rows for one session end (FS-4).

    ⚠️ **One `executemany`, not a loop.** `db_cursor()` is one transaction per
    `with`, so a loop of calls in separate blocks would be N transactions and a
    failure half way would leave half a register written.

    `ON DUPLICATE KEY UPDATE id = id` because ending a session twice must not
    fail, and must not overwrite a Present that arrived between the two.

    ⚠️ **`time_in` is NULL, and it used to be `NOW()`.** This statement reused
    the Present row's column list, so every absentee was stamped with the
    moment the operator pressed End - an arrival time, identical across the
    cohort, for students who never arrived. `set_status()` below states the
    principle this violated: a fabricated timestamp in a biometric register is
    worse than an obviously untouched field. It was written for corrections and
    never applied to the write that creates the rows corrections operate on.

    The column had to be widened before the code could mean it - migration 007.
    Until then it was TIME NOT NULL, and this server has no
    STRICT_TRANS_TABLES, so a NULL here was silently converted to 00:00:00 on
    an INSERT that reported success (lessons.md L11).
    """
    cursor.executemany(
        """
        INSERT INTO attendance
            (student_id, subject_id, session_id,
             attendance_date, time_in, status)
        VALUES (%s, %s, %s, CURDATE(), NULL, 'Absent')
        ON DUPLICATE KEY UPDATE id = id
        """,
        rows,
    )


# How many rows to pull from the server per round trip in `iter_filtered()`.
# The point of the batch is that it is bounded, not that it is any particular
# size: 500 register rows is a few tens of kilobytes, and the alternative being
# avoided is the whole result set at once.
EXPORT_FETCH_SIZE = 500


def _filtered_query(selected_date=None, subject_id=None, instructor_id=None,
                    sort=DEFAULT_SORT, **placement):
    """
    The filtered-register SELECT and its bound values.

    `placement` is any of `PLACEMENT_FILTERS` - the student's department,
    program, year level or section - and `sort` is a key of `SORT_ORDERS`. An
    unrecognised sort falls back to the default rather than failing: it comes
    from a URL, and a stale bookmark should still show a register.

    `instructor_id` narrows the register to one instructor's offerings (DM-5).
    For a signed-in instructor it is their own key and the caller does not let
    the request choose it; for an administrator it is a filter like the others.

    The WHERE clause is assembled, but only from fixed fragments - every value
    is a bound parameter. `subject_id` and not `subject_code`: two sections
    sharing a code are two different registers.

    One function rather than two nearly-identical ones, because `filtered()`
    and `iter_filtered()` must return the same rows in the same order. A filter
    added to one and not the other would give the screen and the spreadsheet
    different answers, which is the FS-11 complaint the export already had once.
    """
    query = f"SELECT {REGISTER_COLUMNS} {REGISTER_JOIN} WHERE 1=1"
    values = []

    if selected_date:
        query += " AND a.attendance_date = %s"
        values.append(selected_date)

    if subject_id is not None:
        query += " AND a.subject_id = %s"
        values.append(subject_id)

    if instructor_id is not None:
        query += " AND sub.instructor_id = %s"
        values.append(instructor_id)

    placed, placed_values = _placement_conditions(placement)

    query += placed
    values += placed_values

    query += " ORDER BY " + SORT_ORDERS.get(sort, SORT_ORDERS[DEFAULT_SORT])

    return query, values


# The sessions table on /reports shows this many, newest first. A bound rather
# than a page: the register beside it is the record, and this is the index to
# it.
SESSION_LIST_LIMIT = 100


def sessions_filtered(cursor, selected_date=None, subject_id=None,
                      instructor_id=None, **placement):
    """
    The class meetings behind the register, newest first, with their tallies.

    Takes the same three filters as `_filtered_query()` so the two tables on
    /reports always describe the same slice.

    ⚠️ **The tallies count the rows this session wrote**, through
    `attendance.session_id`. The register holds one row per student per subject
    per day (RE-3), so a second session for the same class on the same day
    shows the marks it added and not the ones the first already held.

    ⚠️ Conditional SUMs over a LEFT JOIN, COALESCEd: a session that recorded
    nobody still appears, with zeros, which is the row an instructor most needs
    to see.

    ⚠️ **With a placement filter the joins become inner ones.** "Sessions for
    year 4, section B" means the sessions in which a student of that section
    was recorded, and the tallies then count those students only. A session
    that recorded none of them is not listed - under that filter it has
    nothing to say.
    """
    placed, placed_values = _placement_conditions(placement)

    # `s`, `sec`, `p` and `d` only exist in the query when something filters on
    # them, so the unfiltered list keeps its LEFT JOIN and its empty sessions.
    marks = (
        f"""
        JOIN attendance a ON a.session_id = ses.id
        JOIN students s ON s.student_id = a.student_id
        {ACADEMIC_JOIN}
        """
        if placed else
        "LEFT JOIN attendance a ON a.session_id = ses.id"
    )

    query = f"""
        SELECT
            ses.id,
            ses.session_date,
            ses.started_at,
            ses.ended_at,
            ses.started_by,
            sub.subject_code,
            sub.subject_name,
            sub.section,
            COALESCE(i.fullname, sub.instructor) AS instructor,
            COALESCE(SUM(a.status = 'Present'), 0) AS present,
            COALESCE(SUM(a.status = 'Late'), 0) AS late,
            COALESCE(SUM(a.status = 'Absent'), 0) AS absent
        FROM attendance_sessions ses
        JOIN subjects sub ON sub.id = ses.subject_id
        LEFT JOIN instructors i ON i.id = sub.instructor_id
        {marks}
        WHERE 1=1
    """
    values = []

    if selected_date:
        query += " AND ses.session_date = %s"
        values.append(selected_date)

    if subject_id is not None:
        query += " AND ses.subject_id = %s"
        values.append(subject_id)

    if instructor_id is not None:
        query += " AND sub.instructor_id = %s"
        values.append(instructor_id)

    query += placed
    values += placed_values

    query += f"""
        GROUP BY ses.id
        ORDER BY ses.started_at DESC, ses.id DESC
        LIMIT {SESSION_LIST_LIMIT}
    """

    cursor.execute(query, values)

    return cursor.fetchall()


def filtered(cursor, selected_date=None, subject_id=None, instructor_id=None,
             sort=DEFAULT_SORT, **placement):
    """
    The register, narrowed by the filters the operator chose, as a list.

    For `/reports`, which renders the rows into a template and needs to know
    how many there are. The export uses `iter_filtered()` below.
    """
    query, values = _filtered_query(
        selected_date, subject_id, instructor_id, sort, **placement
    )

    cursor.execute(query, values)

    return cursor.fetchall()


def iter_filtered(cursor, selected_date=None, subject_id=None,
                  instructor_id=None, sort=DEFAULT_SORT, **placement):
    """
    The same register, yielded a batch at a time.

    ⚠️ **This function is PE-8's claim, made true.** Three documents - the
    `services/reporting.py` docstring, `todo.md` PE-8 and
    `handover-phase-5.md` §4.3 - said export rows "come from a server-side
    cursor one at a time" and that "the sheet never holds them". The second
    half was true: openpyxl's write-only mode serialises each row and drops it.
    The first was not. `register_workbook()` iterated `filtered()`, whose last
    statement is `return cursor.fetchall()`, so the entire filtered register
    was materialised as a list of dicts *before* the first row reached the
    sheet - the memory behaviour the rewrite existed to remove, one layer down
    from where it was removed.

    A benchmark's assumptions are measurements too (lessons.md L9), and this
    figure is bound for the manuscript. Making it true was cheaper than
    defending the narrower version of it.

    `db_cursor()` hands out an unbuffered mysql-connector cursor, so
    `fetchmany()` genuinely round-trips rather than slicing a list the driver
    already holds.

    ⚠️ The cursor must stay open for the whole iteration. That is why
    `register_workbook()` writes the sheet *inside* its `with db_cursor()`
    block rather than collecting first.
    """
    query, values = _filtered_query(
        selected_date, subject_id, instructor_id, sort, **placement
    )

    cursor.execute(query, values)

    while True:
        batch = cursor.fetchmany(EXPORT_FETCH_SIZE)

        if not batch:
            return

        yield from batch


def recognised_today(cursor, subject_id, student_ids):
    """
    Names and statuses for students already recorded in today's session.

    Feeds the live recognised-students list (US-3), which is why it takes the
    IDs the recognition session has confirmed rather than reading the whole
    register: the answer must reflect what the camera has actually done.
    """
    if not student_ids:
        return []

    placeholders = ",".join(["%s"] * len(student_ids))

    cursor.execute(
        f"""
        SELECT a.student_id, s.name, a.time_in, a.status
        FROM attendance a
        JOIN students s ON s.student_id = a.student_id
        WHERE a.subject_id = %s
        AND a.attendance_date = CURDATE()
        AND a.student_id IN ({placeholders})
        ORDER BY a.time_in ASC
        """,
        (subject_id, *student_ids),
    )

    return cursor.fetchall()


def recorded_today(cursor, subject_id):
    """
    `{student_id: status}` for everyone already recorded for this subject today.

    Seeds a starting recognition session (FS-18), so a student the register
    already holds is not asked to pass the liveness challenge a second time for
    a mark they have. Deliberately the whole register for the subject rather
    than a filtered list: the question here is the opposite of
    `recognised_today()`'s. That one asks what *this camera run* has done and
    must not see an earlier session's rows; this one exists precisely to see
    them.

    ⚠️ **Keyed on `subject_id` and `CURDATE()`, matching the UNIQUE index that
    suppresses duplicate writes** (RE-3). If this query and that index ever
    disagree about what "already recorded" means, a student is either asked to
    verify twice or skipped when they should not be.

    ⚠️ **An Absent row is not a mark, and reading it as one cost a class their
    attendance.** `insert_absences()` writes an Absent row for every enrolled
    student who was not recognised when a session ended. This query used to
    select those too, so the *next* session that day seeded them into
    `recognized`, logged "will not be asked to verify again", and skipped them
    for the rest of the day - a student absent from the 8am period could not be
    recorded present at 9am. Measured on the dev database on 2026-09-12: three
    students carried Absent rows for CS401 and the next session's seed reported
    exactly those three. The statuses that mean "this student was recorded" are
    the ones `derive_status()` can produce, and Absent is not among them.
    """
    cursor.execute(
        """
        SELECT student_id, status
        FROM attendance
        WHERE subject_id = %s
        AND attendance_date = CURDATE()
        AND status IN ('Present', 'Late')
        """,
        (subject_id,),
    )

    return {row["student_id"]: row["status"] for row in cursor.fetchall()}


# ---------------------------------------------------------------------------
# Corrections (FS-10)
# ---------------------------------------------------------------------------


def record(cursor, attendance_id):
    """One register row with the student and subject joined on, or None."""
    cursor.execute(
        f"""
        SELECT
            a.id,
            a.attendance_date,
            a.student_id,
            s.name AS student_name,
            a.subject_id,
            sub.instructor_id,
            sub.subject_code,
            sub.subject_name,
            sub.section,
            a.time_in,
            a.status
        {REGISTER_JOIN}
        WHERE a.id = %s
        """,
        (attendance_id,)
    )

    return cursor.fetchone()


def correction_history(cursor, attendance_id):
    """Every correction ever made to this record, newest first."""
    cursor.execute(
        """
        SELECT changed_by, changed_at, old_status, new_status, reason
        FROM attendance_audit
        WHERE attendance_id = %s
        ORDER BY changed_at DESC, id DESC
        """,
        (attendance_id,)
    )

    return cursor.fetchall()


def lock_for_correction(cursor, attendance_id):
    """
    Read the current status and hold the row until the transaction ends.

    ⚠️ **FOR UPDATE is the point of this function.** Two operators correcting
    the same record at once would otherwise both read the same "before" value,
    and the audit trail would claim two changes from a state only one of them
    saw.
    """
    cursor.execute(
        "SELECT id, status FROM attendance WHERE id = %s FOR UPDATE",
        (attendance_id,)
    )

    return cursor.fetchone()


def set_status(cursor, attendance_id, status):
    """
    Change one record's status.

    ⚠️ **`time_in` is deliberately not written.** Correcting an Absent to
    Present would have to invent an arrival time, and a fabricated timestamp in
    a biometric register is worse than an obviously untouched field. The audit
    row is where the truth lives: this status was set by a named person, at a
    recorded moment, for a stated reason.

    ⚠️ **Never call this without `record_correction()` in the same
    transaction.** See the module docstring.
    """
    cursor.execute(
        "UPDATE attendance SET status = %s WHERE id = %s",
        (status, attendance_id)
    )


def record_correction(cursor, attendance_id, changed_by, old_status,
                      new_status, reason):
    """The audit row. See `set_status()`."""
    cursor.execute(
        """
        INSERT INTO attendance_audit
            (attendance_id, changed_by, old_status, new_status, reason)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (
            attendance_id,
            changed_by,
            old_status,
            new_status,
            reason,
        )
    )
