"""
The mobile API is for students, and registers no one and captures no faces.

Registration and face capture belong to an administrator, or an instructor for
their own classes, on the website (`web/enrolment.py`). The mobile API used to
offer both: `/api/students/register` was public, so anyone with the link could
create a student whose face then went into `dataset/` and the model.

It also used to sign in administrators and instructors, whose app screens
repeated a small part of the website. `/api/login` now issues tokens to
students only, and every route refuses any other token - including one issued
before the change, which stays valid for `JWT_EXPIRY_HOURS`.

None of these tests touches the database: each refusal happens before it.
"""

from __future__ import annotations

import pytest

from web.api import _make_token

REMOVED = [
    ("POST", "/api/students/register"),
    ("GET", "/api/colleges_programs"),
    ("POST", "/api/enrol/start"),
    ("POST", "/api/enrol/frame"),
    ("POST", "/api/enrol/finish"),
    ("POST", "/api/enrol/cancel"),
    ("GET", "/api/students"),
    ("GET", "/api/subjects"),
    ("GET", "/api/dashboard"),
]

STUDENT_ROUTES = [
    ("GET", "/api/me"),
    ("GET", "/api/attendance/history"),
    ("GET", "/api/attendance/live"),
    ("POST", "/api/attendance/frame"),
]

STAFF_TOKENS = {
    "admin": {"role": "admin", "user_id": "admin", "admin_id": 1},
    "instructor": {"role": "instructor", "user_id": "INS-1", "instructor_pk": 1},
}


@pytest.fixture(scope="module")
def flask_app():
    import app as app_module

    app_module.app.config["TESTING"] = True
    return app_module.app


@pytest.mark.parametrize("method, path", REMOVED)
def test_the_route_does_not_exist(flask_app, method, path):
    response = flask_app.test_client().open(path, method=method, json={})

    assert response.status_code == 404


def test_no_api_route_reaches_enrolment(flask_app):
    """Catches a capture route added back under a new name, too."""
    enrolment_routes = [
        rule.rule
        for rule in flask_app.url_map.iter_rules()
        if rule.rule.startswith("/api/")
        and ("enrol" in rule.rule or "register" in rule.rule)
    ]

    assert enrolment_routes == []


@pytest.mark.parametrize("role", sorted(STAFF_TOKENS))
def test_staff_cannot_sign_in(flask_app, role):
    response = flask_app.test_client().post(
        "/api/login",
        json={"role": role, "user_id": "anyone", "password": "anything"},
    )
    body = response.get_json()

    assert response.status_code == 403
    assert "token" not in body
    assert "website" in body["message"]


@pytest.mark.parametrize("role", sorted(STAFF_TOKENS))
@pytest.mark.parametrize("method, path", STUDENT_ROUTES)
def test_a_staff_token_is_refused_everywhere(flask_app, role, method, path):
    token = _make_token(dict(STAFF_TOKENS[role]))

    response = flask_app.test_client().open(
        path, method=method, headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 403


@pytest.mark.parametrize("method, path", STUDENT_ROUTES)
def test_no_token_is_refused_everywhere(flask_app, method, path):
    response = flask_app.test_client().open(path, method=method)

    assert response.status_code == 401
