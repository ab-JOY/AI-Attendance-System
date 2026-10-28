"""
The administrator's Activity page, and the sign-in history behind it.

Requested by the user on 2026-10-28: the administrator should see what the
system is doing - an instructor signing in, a class being run - without
reading the server log.

The rule that matters most is not on the page: **recording a sign-in must
never decide whether someone signs in.** A database hiccup, or a server that
started before migration 013, must cost a history row and nothing else.
No database here; tests/integration covers the SQL.
"""

from __future__ import annotations

import contextlib
from datetime import datetime

import pytest

from tests.test_login_without_role import (  # noqa: F401 - fixtures
    ADMIN_PASSWORD,
    accounts,
    client,
    flask_app,
    hashes,
    login,
)
from tests.test_route_security import sign_in_as


# ---------------------------------------------------------------------------
# 1. Every sign-in outcome is recorded - website
# ---------------------------------------------------------------------------


def test_a_successful_sign_in_is_recorded(client, accounts, login_history_calls):
    login(client, "INS-001", "instructor-password-456")

    assert login_history_calls == [("INS-001", "instructor", "success", "web")]


def test_a_wrong_password_is_recorded_without_a_role(client, accounts, login_history_calls):
    login(client, "admin", "not-the-password")

    assert login_history_calls == [("admin", None, "failed", "web")]


def test_a_locked_out_attempt_is_recorded(client, accounts, login_history_calls):
    for _ in range(10):
        login(client, "admin", "not-the-password")
    login_history_calls.clear()

    response = login(client, "admin", ADMIN_PASSWORD)

    assert response.status_code == 429
    assert login_history_calls == [("admin", None, "locked", "web")]


def test_a_forced_password_change_is_still_a_recorded_sign_in(
    client, accounts, login_history_calls, monkeypatch
):
    import web.auth as auth_module

    monkeypatch.setattr(auth_module, "is_known_default", lambda *_args: True)

    login(client, "admin", ADMIN_PASSWORD)

    assert login_history_calls == [("admin", "admin", "success", "web")]


# ---------------------------------------------------------------------------
# 2. Recording can never block a sign-in
# ---------------------------------------------------------------------------


def test_a_failing_history_write_does_not_block_the_sign_in(
    client, accounts, login_history_calls, monkeypatch
):
    """The real recorder, over a database that refuses the write."""
    from services import login_history

    @contextlib.contextmanager
    def broken_cursor(*_args, **_kwargs):
        raise RuntimeError("Table 'login_events' doesn't exist")
        yield

    monkeypatch.setattr(login_history, "db_cursor", broken_cursor)
    monkeypatch.setattr(login_history, "record", login_history_calls.real)

    response = login(client, "admin", ADMIN_PASSWORD)

    assert response.status_code == 302
    with client.session_transaction() as session:
        assert session["role"] == "admin"


def test_the_recorder_swallows_and_logs(login_history_calls, monkeypatch, caplog):
    from services import login_history

    @contextlib.contextmanager
    def broken_cursor(*_args, **_kwargs):
        raise RuntimeError("database is down")
        yield

    monkeypatch.setattr(login_history, "db_cursor", broken_cursor)

    login_history_calls.real("INS-001", "instructor", "success", "web")

    assert "Could not record a success sign-in for 'INS-001'" in caplog.text


def test_a_long_typed_username_is_cut_to_the_column(login_history_calls):
    from repositories import activity

    class Cursor:
        def execute(self, _sql, params):
            self.params = params

    cursor = Cursor()
    activity.record_login(cursor, "x" * 500, None, "failed", "web")

    assert len(cursor.params[0]) == activity.USERNAME_MAX


# ---------------------------------------------------------------------------
# 3. The page
# ---------------------------------------------------------------------------


@pytest.fixture
def page_data(monkeypatch):
    import web.activity as activity_module

    data = {
        "logins": [
            {"username": "INS-001", "role": "instructor", "outcome": "success",
             "channel": "web", "occurred_at": datetime(2026, 10, 28, 22, 40, 5)},
            {"username": "<script>alert(1)</script>", "role": None,
             "outcome": "failed", "channel": "web",
             "occurred_at": datetime(2026, 10, 28, 22, 39, 0)},
            {"username": "23-1-1-0918", "role": "student", "outcome": "success",
             "channel": "app", "occurred_at": datetime(2026, 10, 28, 22, 38, 0)},
        ],
        "sessions": [
            {"id": 214, "started_by": "Ana Cruz", "subject_code": "new",
             "subject_name": "New Subject", "section": "B",
             "started_at": datetime(2026, 10, 28, 22, 41), "ended_at": None,
             "present": 1, "late": None, "absent": 6},
        ],
    }

    @contextlib.contextmanager
    def fake_cursor(*_args, **_kwargs):
        yield object()

    monkeypatch.setattr(activity_module, "db_cursor", fake_cursor)
    monkeypatch.setattr(activity_module.activity_repo, "recent_logins", lambda _c: data["logins"])
    monkeypatch.setattr(activity_module.activity_repo, "recent_sessions", lambda _c: data["sessions"])
    return data


def test_the_admin_sees_sign_ins_and_sessions(client, page_data):
    sign_in_as(client, "admin")

    page = client.get("/activity").get_data(as_text=True)

    assert "INS-001" in page and "Signed in" in page
    assert "Wrong ID or password" in page
    assert "Phone app" in page and "23-1-1-0918" in page
    assert "Ana Cruz" in page and "new - New Subject" in page
    assert "Not ended" in page


def test_a_typed_username_is_shown_as_text_not_run(client, page_data):
    sign_in_as(client, "admin")

    page = client.get("/activity").get_data(as_text=True)

    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_an_instructor_cannot_see_the_activity_page(client, page_data):
    sign_in_as(client, "instructor")

    assert client.get("/activity").status_code == 403


def test_the_admin_menu_links_to_it(client, page_data):
    sign_in_as(client, "admin")
    assert 'href="/activity"' in client.get("/activity").get_data(as_text=True)


def test_a_database_failure_is_an_error_page_not_a_traceback(client, monkeypatch):
    import mysql.connector

    import web.activity as activity_module

    @contextlib.contextmanager
    def broken_cursor(*_args, **_kwargs):
        raise mysql.connector.Error("down")
        yield

    monkeypatch.setattr(activity_module, "db_cursor", broken_cursor)
    sign_in_as(client, "admin")

    response = client.get("/activity")

    assert response.status_code == 500
    assert "Traceback" not in response.get_data(as_text=True)


# ---------------------------------------------------------------------------
# 4. Student sign-ins on the phone app
# ---------------------------------------------------------------------------


@pytest.fixture
def student_row(monkeypatch):
    import web.api as api_module
    from security.passwords import hash_password

    row = {"student_id": "23-1-1-0918", "name": "Jeff", "password": hash_password("pw-123456")}

    class Cursor:
        def execute(self, _sql, params):
            self.found = row if params[0] == row["student_id"] else None

        def fetchone(self):
            return self.found

    @contextlib.contextmanager
    def fake_cursor(*_args, **_kwargs):
        yield Cursor()

    monkeypatch.setattr(api_module, "db_cursor", fake_cursor)
    return row


def test_a_student_app_sign_in_is_recorded(flask_app, student_row, login_history_calls):
    response = flask_app.test_client().post(
        "/api/login", json={"user_id": "23-1-1-0918", "password": "pw-123456"}
    )

    assert response.status_code == 200
    assert login_history_calls == [("23-1-1-0918", "student", "success", "app")]


@pytest.mark.parametrize("user_id, password", [
    ("23-1-1-0918", "wrong"),
    ("no-such-student", "pw-123456"),
])
def test_a_failed_student_app_sign_in_is_recorded(
    flask_app, student_row, login_history_calls, user_id, password
):
    response = flask_app.test_client().post(
        "/api/login", json={"user_id": user_id, "password": password}
    )

    assert response.status_code == 401
    assert login_history_calls == [(user_id, None, "failed", "app")]
