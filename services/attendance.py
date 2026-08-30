"""
Opening and closing an attendance session.

Both operations are a database write *and* a camera operation, in an order that
matters, with a compensating step when the second half fails. That is exactly
the kind of thing that should not live in a view function, because the only way
to exercise it there is to send an HTTP request.
"""

from __future__ import annotations

import logging

import mysql.connector

from infra.db import db_cursor
from repositories import attendance as attendance_repo
from repositories import subjects as subjects_repo

logger = logging.getLogger(__name__)


def open_session(subject_id, started_by):
    """
    Record that a class is meeting. Returns `(subject_row, session_id)`.

    Returns `(None, None)` if the subject no longer exists, and raises nothing
    on a database failure - it logs and returns `(None, None)` too, because
    both mean the same thing to the caller: no session was opened.

    ⚠️ **The row is written before the camera is touched**, so a session that
    starts and is never ended is still a recorded fact. Absence is a fact about
    a session rather than about a date: without the row, "the class never met"
    and "the class met and nobody was recognised" are the same picture.
    """
    try:
        with db_cursor(dictionary=True, commit=True) as cursor:
            subject_row = subjects_repo.code_of(cursor, subject_id)

            if subject_row is None:
                return None, None

            session_row_id = attendance_repo.open_session(
                cursor, subject_id, started_by
            )

    except mysql.connector.Error:
        logger.exception("Could not open an attendance session")
        return None, None

    return subject_row, session_row_id


def close_session(session_row_id):
    """
    Stamp `ended_at`. Best effort - never the reason a request fails.

    Called both on the normal path and as compensation when the camera refuses
    to start: the row was already written by then, and leaving it open would
    show for ever on the dashboard as a session in progress.
    """
    if session_row_id is None:
        return

    try:
        with db_cursor(commit=True) as cursor:
            attendance_repo.close_session(cursor, session_row_id)
    except mysql.connector.Error:
        logger.exception("Could not close attendance session %s", session_row_id)
