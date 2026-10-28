"""
Recording a sign-in for the Activity page, without ever getting in its way.

⚠️ **A history write must never decide whether someone signs in.** It runs
after the credential check and swallows every failure - a database hiccup, or
a server started before migration 013 created the table - with a log line.
Losing one history row is a gap in a report; refusing a sign-in because the
report could not be written would be an outage.
"""

from __future__ import annotations

import logging

from infra.db import db_cursor
from repositories import activity as activity_repo
from repositories.activity import FAILED, LOCKED, SUCCESS  # noqa: F401 - re-exported

logger = logging.getLogger(__name__)


def record(username, role, outcome, channel):
    try:
        # commit=True: without it db_cursor rolls the insert back, and every
        # sign-in would vanish with no error. Unit tests stub the database and
        # cannot see that; tests/integration/test_activity_history.py can.
        with db_cursor(commit=True) as cursor:
            activity_repo.record_login(cursor, username, role, outcome, channel)

    except Exception:
        logger.warning(
            "Could not record a %s sign-in for %r in login_events",
            outcome,
            username,
            exc_info=True,
        )
