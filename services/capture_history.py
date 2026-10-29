"""
Recording how each face capture ended, for the Activity page.

⚠️ **A history write must never change how a capture ends.** It runs after
the capture has been saved, cancelled or discarded, and swallows every
failure with a log line - exactly as services/login_history.py does for
sign-ins. Losing a history row is a gap in a report; failing a capture that
was saved, because its report could not be written, would be wrong twice.
"""

from __future__ import annotations

import logging

from infra.db import db_cursor
from repositories import activity as activity_repo
from repositories.activity import (  # noqa: F401 - re-exported
    ABANDONED,
    CANCELLED,
    CAPTURE_FAILED,
    COMPLETED,
)

logger = logging.getLogger(__name__)


def describe(capture_session):
    """What kind of capture this was, in the words the page shows."""
    added = getattr(capture_session, "added_session", None)

    if added is not None:
        return f"Added session {added.number} ({added.condition})"

    # A recapture carries an empty record: the student row already exists
    # and is not rewritten (web/enrolment.py, enrol_finish).
    return "New student" if capture_session.record else "Recapture"


def record(capture_session, outcome):
    try:
        with db_cursor(commit=True) as cursor:
            activity_repo.record_capture(
                cursor,
                capture_session.student_id,
                capture_session.student_name,
                capture_session.started_by,
                describe(capture_session),
                outcome,
                capture_session.captured,
            )

    except Exception:
        logger.warning(
            "Could not record a %s capture of %r in capture_events",
            outcome,
            getattr(capture_session, "student_id", None),
            exc_info=True,
        )
