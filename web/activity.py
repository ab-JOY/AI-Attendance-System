"""
The administrator's Activity page: who signed in, which classes ran, and
whose face was captured by whom - including a capture running right now.

Requested by the user on 2026-10-28 - the administrator should see what the
system is doing, such as an instructor signing in, without reading the server
log. Read-only: nothing here changes a record.
"""

from __future__ import annotations

import logging

import mysql.connector
from flask import Blueprint, render_template

from infra.db import db_cursor
from repositories import activity as activity_repo
from security.access import role_required
from services import capture_history
from web.errors import error_page

logger = logging.getLogger(__name__)

activity_bp = Blueprint("activity", __name__)


@activity_bp.route('/activity')
@role_required('admin')
def activity():
    try:
        with db_cursor(dictionary=True) as cursor:
            logins = activity_repo.recent_logins(cursor)
            sessions = activity_repo.recent_sessions(cursor)
            captures = activity_repo.recent_captures(cursor)

    except mysql.connector.Error:
        logger.exception("Could not load the activity page")
        return error_page(500)

    return render_template(
        "activity.html",
        logins=logins,
        sessions=sessions,
        captures=captures,
        live_capture=_capture_in_progress(),
    )


def _capture_in_progress():
    """
    The one face capture running now, or None.

    There is only ever one (`enrolment_slot`), so it is read live rather than
    stored: it is in this process's memory until it ends, and only then is it
    written to capture_events. Imported here, not at module scope, so this
    blueprint does not import another one at load (web/__init__.py).
    """
    from web.enrolment import enrolment_slot

    capture = enrolment_slot.current()

    if capture is None:
        return None

    return {
        "student_id": capture.student_id,
        "student_name": capture.student_name,
        "performed_by": capture.started_by,
        "kind": capture_history.describe(capture),
        "captured": capture.captured,
        "total": capture.plan.total,
        "minutes": int(capture.running_seconds // 60),
    }
