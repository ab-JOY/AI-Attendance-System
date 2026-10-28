"""
The administrator's Activity page: who signed in, and which classes ran.

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

    except mysql.connector.Error:
        logger.exception("Could not load the activity page")
        return error_page(500)

    return render_template("activity.html", logins=logins, sessions=sessions)
