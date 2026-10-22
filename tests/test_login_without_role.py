"""
Signing in without choosing a role.

The panel found the "User Type" dropdown slow, so it was removed: the login
form sends an identifier and a password, and the account they open decides
whether the session is an administrator's or an instructor's. No database -
both lookups are replaced, and passwords are real bcrypt hashes.
"""

from __future__ import annotations

import contextlib

import pytest

from security.access import INSTRUCTOR_KEY
from security.passwords import hash_password
from security.rate_limit import LoginRateLimiter

ADMIN_PASSWORD = "admin-password-123"
INSTRUCTOR_PASSWORD = "instructor-password-456"


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


@contextlib.contextmanager
def fake_cursor(*_args, **_kwargs):
    yield object()


@pytest.fixture(scope="module")
def hashes():
    return {
        "admin": hash_password(ADMIN_PASSWORD),
        "instructor": hash_password(INSTRUCTOR_PASSWORD),
    }


@pytest.fixture
def accounts(monkeypatch, hashes):
    """
    One admin and one instructor. Tests set `admins` / `instructors` to choose
    which identifiers exist; the login rate limiter is reset so failures in one
    test cannot lock out the next.
    """
    import web.auth as auth_module

    store = {
        "admins": {
            "admin": {
                "id": 1, "username": "admin",
                "password": hashes["admin"], "must_change_password": 0,
            },
        },
        "instructors": {
            "INS-001": {
                "id": 42, "instructor_id": "INS-001", "fullname": "Ana Cruz",
                "password": hashes["instructor"], "must_change_password": 0,
            },
        },
    }

    monkeypatch.setattr(auth_module, "db_cursor", fake_cursor)
    monkeypatch.setattr(
        auth_module.credentials_repo, "admin_by_username",
        lambda _cursor, user_id: store["admins"].get(user_id),
    )
    monkeypatch.setattr(
        auth_module.credentials_repo, "instructor_by_id",
        lambda _cursor, user_id: store["instructors"].get(user_id),
    )
    monkeypatch.setattr(auth_module, "is_known_default", lambda *_args: False)
    monkeypatch.setattr(auth_module, "login_rate_limiter", LoginRateLimiter())

    return store


def login(client, user_id, password):
    return client.post("/login", data={"user_id": user_id, "password": password})


def test_the_login_page_has_no_role_dropdown(client):
    page = client.get("/").get_data(as_text=True)

    assert "<select" not in page
    assert 'name="role"' not in page
    assert 'name="user_id"' in page
    assert 'name="password"' in page


def test_an_admin_signs_in_with_username_and_password_only(client, accounts):
    response = login(client, "admin", ADMIN_PASSWORD)

    assert response.status_code == 302

    with client.session_transaction() as session:
        assert session["role"] == "admin"
        assert session["admin_id"] == 1


def test_an_instructor_signs_in_with_id_and_password_only(client, accounts):
    response = login(client, "INS-001", INSTRUCTOR_PASSWORD)

    assert response.status_code == 302

    with client.session_transaction() as session:
        assert session["role"] == "instructor"
        assert session["instructor_id"] == "INS-001"
        assert session[INSTRUCTOR_KEY] == 42


@pytest.mark.parametrize("user_id, password", [
    ("admin", "wrong"),
    ("INS-001", "wrong"),
    ("nobody", ADMIN_PASSWORD),
    # One account's password does not open the other account.
    ("admin", INSTRUCTOR_PASSWORD),
    ("INS-001", ADMIN_PASSWORD),
    # SE-15: the identifier is compared exactly, not by the collation.
    ("ADMIN", ADMIN_PASSWORD),
])
def test_wrong_credentials_are_refused(client, accounts, user_id, password):
    response = login(client, user_id, password)

    assert response.status_code == 401
    assert "Invalid Login Credentials" in response.get_data(as_text=True)

    with client.session_transaction() as session:
        assert "role" not in session


def test_an_instructor_whose_id_is_an_admin_username_still_signs_in_as_themselves(
    client, accounts, hashes
):
    """
    Nothing stops the two identifiers colliding. The password decides which
    account opens, so the instructor does not become an administrator.
    """
    accounts["instructors"]["admin"] = {
        "id": 7, "instructor_id": "admin", "fullname": "Same Name",
        "password": hashes["instructor"], "must_change_password": 0,
    }

    response = login(client, "admin", INSTRUCTOR_PASSWORD)

    assert response.status_code == 302

    with client.session_transaction() as session:
        assert session["role"] == "instructor"
        assert session[INSTRUCTOR_KEY] == 7
        assert "admin_id" not in session
