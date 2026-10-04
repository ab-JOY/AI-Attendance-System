"""The landing page and its four counters (FS-5)."""

from __future__ import annotations

import logging

import mysql.connector
from flask import Blueprint, render_template

from infra.db import db_cursor
from repositories import attendance as attendance_repo
from security.access import authenticated, instructor_scope

logger = logging.getLogger(__name__)

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route('/dashboard')
@authenticated
def dashboard():
    """
    The four counters, from the database (FS-5).

    They were hardcoded `0` in the template, and the route had no database
    access at all - four lines returning a render. So this was a route change
    rather than a template tidy-up.
    """
    counts = {
        "students": 0,
        "subjects": 0,
        "attendance_today": 0,
        "active_sessions": 0,
    }

    try:
        with db_cursor(dictionary=True) as cursor:
            # DM-5: an instructor's dashboard counts their own classes. None
            # for an administrator, which is the whole school.
            counts = attendance_repo.counters(
                cursor, instructor_id=instructor_scope()
            ) or counts

    except mysql.connector.Error:
        # A dashboard that cannot count is still a usable navigation page, and
        # an error page here would lock the operator out of every other screen.
        logger.exception("Dashboard counters unavailable")

    return render_template('dashboard.html', counts=counts)
