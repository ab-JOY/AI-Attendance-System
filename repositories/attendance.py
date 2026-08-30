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
    a.time_in,
    a.status
"""

REGISTER_JOIN = """
    FROM attendance a
    JOIN students s ON s.student_id = a.student_id
    JOIN subjects sub ON sub.id = a.subject_id
"""


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


def counters(cursor):
    """
    The four dashboard numbers (FS-5), in one round trip.

    They were hardcoded `0` in the template and the route had no database
    access at all.

    "Active sessions" counts today's `attendance_sessions` rows with no
    `ended_at`. That includes sessions started and never ended as well as one
    running right now, which is honest: an unclosed session is a real thing
    that happened and the operator should see it.
    """
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


def _filtered_query(selected_date=None, subject_id=None):
    """
    The filtered-register SELECT and its bound values.

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

    query += " ORDER BY a.attendance_date DESC, a.time_in DESC"

    return query, values


def filtered(cursor, selected_date=None, subject_id=None):
    """
    The register, narrowed by the filters the operator chose, as a list.

    For `/reports`, which renders the rows into a template and needs to know
    how many there are. The export uses `iter_filtered()` below.
    """
    query, values = _filtered_query(selected_date, subject_id)

    cursor.execute(query, values)

    return cursor.fetchall()


def iter_filtered(cursor, selected_date=None, subject_id=None):
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
    query, values = _filtered_query(selected_date, subject_id)

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
    """
    cursor.execute(
        """
        SELECT student_id, status
        FROM attendance
        WHERE subject_id = %s
        AND attendance_date = CURDATE()
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
