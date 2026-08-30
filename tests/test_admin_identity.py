"""
The account screens act on the administrator who is signed in (R8, SE-16).

**The defect, and why it was invisible.** Login authenticates *any* row in
`admin` by username. The account screens assumed there was only ever one:
`_credential_target()` returned a literal `1`, `rename_admin()` wrote
`WHERE id=1`, and `/settings` displayed whatever `SELECT ... LIMIT 1` returned.
With one row those three are indistinguishable from correct behaviour, which is
why this survived - it is latent on this deployment and would only surface the
day a second administrator account existed.

SE-16 is the same finding one step earlier: /change_password "used to update
`admin WHERE id=1` no matter who was signed in", which blocked the forced
change for instructors entirely. That fix corrected the *role* handling and
carried the hardcoded key across.

**A session with no key is a 403, deliberately.** The alternative - falling
back to row 1 - is the defect, and it would be a silent write to somebody
else's account. An administrator signed in across the deployment has to sign in
again, which is a one-time cost and the honest one.

No database: `_credential_target()` reads the Flask session and nothing else,
and the routes below are refused before any query runs.
"""

from __future__ import annotations

import contextlib

import pytest


@pytest.fixture(scope="module")
def flask_app():
    import app as app_module

    app_module.app.config["TESTING"] = True
    return app_module.app


@pytest.fixture
def client(flask_app):
    flask_app.config["WTF_CSRF_ENABLED"] = False
    yield flask_app.test_client()
    flask_app.config["WTF_CSRF_ENABLED"] = True


def sign_in(client, **session_values):
    with client.session_transaction() as session:
        session.update(session_values)


def test_the_target_is_the_signed_in_administrator(flask_app):
    from web.auth import _credential_target

    with flask_app.test_request_context():
        from flask import session

        session["role"] = "admin"
        session["admin_id"] = 42

        assert _credential_target() == ("admin", "id", 42), (
            "the password change is not keyed on the account that signed in"
        )


def test_a_second_administrator_is_not_row_one(flask_app):
    """
    The scenario the literal `1` produced: two accounts, one target.

    This is the assertion that would have failed on the old code, and it needs
    no second row in the database to make the point - the key is read from the
    session either way.
    """
    from web.auth import _credential_target

    with flask_app.test_request_context():
        from flask import session

        session["role"] = "admin"
        session["admin_id"] = 2

        _table, _column, key = _credential_target()

    assert key == 2, "an administrator's password change targeted row 1"


def test_an_admin_session_with_no_key_has_no_target(flask_app):
    from web.auth import _credential_target

    with flask_app.test_request_context():
        from flask import session

        session["role"] = "admin"

        assert _credential_target()[2] is None


def test_the_instructor_branch_is_unchanged(flask_app):
    from web.auth import _credential_target

    with flask_app.test_request_context():
        from flask import session

        session["role"] = "instructor"
        session["instructor_id"] = "SEC-TEST-NOONE"

        assert _credential_target() == (
            "instructors",
            "instructor_id",
            "SEC-TEST-NOONE",
        )


def test_change_password_refuses_a_session_with_no_admin_key(client):
    """The 403 the target above turns into, at the route."""
    sign_in(client, user="test-admin", role="admin")

    assert client.get("/change_password").status_code == 403


def test_settings_refuses_a_session_with_no_admin_key(client):
    sign_in(client, user="test-admin", role="admin")

    assert client.get("/settings").status_code == 403


def test_update_admin_refuses_a_session_with_no_admin_key(client, monkeypatch):
    """
    ⚠️ The important half: the refusal must happen *before* the UPDATE.

    A 403 rendered after the write would look identical to the operator and
    would still have renamed the wrong account.
    """
    import web.account as account_module

    renames = []

    @contextlib.contextmanager
    def fake_cursor(*_args, **_kwargs):
        yield object()

    monkeypatch.setattr(account_module, "db_cursor", fake_cursor)
    monkeypatch.setattr(
        account_module.credentials_repo,
        "rename_admin",
        lambda _cursor, admin_id, username: renames.append((admin_id, username)),
    )

    sign_in(client, user="test-admin", role="admin")

    response = client.post("/update_admin", data={"username": "someone-else"})

    assert response.status_code == 403
    assert renames == [], "an account was renamed from a session with no key"


def test_update_admin_renames_the_signed_in_account(client, monkeypatch):
    import web.account as account_module

    renames = []

    @contextlib.contextmanager
    def fake_cursor(*_args, **_kwargs):
        yield object()

    monkeypatch.setattr(account_module, "db_cursor", fake_cursor)
    monkeypatch.setattr(
        account_module.credentials_repo,
        "rename_admin",
        lambda _cursor, admin_id, username: renames.append((admin_id, username)),
    )

    sign_in(client, user="test-admin", role="admin", admin_id=7)

    client.post("/update_admin", data={"username": "new-name"})

    assert renames == [(7, "new-name")], (
        f"expected a rename of admin 7; got {renames}. A hardcoded 1 here "
        "renames whichever account happens to be first."
    )
