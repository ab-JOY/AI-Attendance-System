"""
Reads and writes behind the administrator's Activity page.

Sign-ins live in `login_events` (migration 013). Sessions already lived in
`attendance_sessions` (migration 003), with who started each one; this only
reads them back with the counts the register holds for each.
"""

from __future__ import annotations

# The column widths in migration 013. A typed username longer than this is
# cut rather than refused: the attempt is still worth recording.
USERNAME_MAX = 100

SUCCESS = "success"
FAILED = "failed"
LOCKED = "locked"


def record_login(cursor, username, role, outcome, channel):
    cursor.execute(
        """
        INSERT INTO login_events (username, role, outcome, channel)
        VALUES (%s, %s, %s, %s)
        """,
        (str(username)[:USERNAME_MAX], role, outcome, channel),
    )


def recent_logins(cursor, limit=100):
    cursor.execute(
        """
        SELECT username, role, outcome, channel, occurred_at
        FROM login_events
        ORDER BY occurred_at DESC, id DESC
        LIMIT %s
        """,
        (limit,),
    )
    return cursor.fetchall()


def recent_sessions(cursor, limit=50):
    """
    The latest attendance sessions, newest first, with the register's counts.

    `ended_at` is NULL for a session still running, or one the server never
    saw end (a crash or a restart mid-class).
    """
    cursor.execute(
        """
        SELECT s.id,
               s.started_by,
               s.started_at,
               s.ended_at,
               sub.subject_code,
               sub.subject_name,
               sub.section,
               SUM(a.status = 'Present') AS present,
               SUM(a.status = 'Late') AS late,
               SUM(a.status = 'Absent') AS absent
        FROM attendance_sessions s
        JOIN subjects sub ON sub.id = s.subject_id
        LEFT JOIN attendance a ON a.session_id = s.id
        GROUP BY s.id, s.started_by, s.started_at, s.ended_at,
                 sub.subject_code, sub.subject_name, sub.section
        ORDER BY s.started_at DESC, s.id DESC
        LIMIT %s
        """,
        (limit,),
    )
    return cursor.fetchall()
