"""
The subject routes fail the way their neighbours do (R5, R10, R14).

Three small findings, one theme: these routes were written before the error
handling the rest of the application settled on, and nothing swept them.

**R5** - `/edit_subject/<id>` for a subject that does not exist returned 500,
`jinja2.UndefinedError: 'None' has no attribute 'id'`. The route passed
`subjects_repo.get()`'s None straight to a template that calls
`url_for(..., id=subject.id)`, and a Jinja Undefined in a URL build raises.
`subject_enrolments`, `edit_student` and `edit_instructor` all handle this
case; this one was missed. A stale bookmark is a 404.

**R10** - the add and update forms read `request.form['subject_code']` and
seven more like it, so a missing field became a `BadRequestKeyError` and a bare
400 naming nothing. Every other form route in this application names the field.

**R14** - six routes relied on the catch-all 500 handler, which renders the
right page and logs "Unhandled exception while serving /add_subject" with no
mention of which subject or what was being attempted.

No database: every case here is decided before or instead of a query.
"""

from __future__ import annotations

import contextlib

import mysql.connector
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


def sign_in(client):
    with client.session_transaction() as session:
        session["user"] = "test-admin"
        session["role"] = "admin"
        session["admin_id"] = 1


@pytest.fixture
def no_database(monkeypatch):
    """A cursor that hands back nothing, for the routes that read."""
    import web.subjects as subjects_module

    @contextlib.contextmanager
    def fake_cursor(*_args, **_kwargs):
        yield object()

    monkeypatch.setattr(subjects_module, "db_cursor", fake_cursor)
    return subjects_module


COMPLETE_FORM = {
    "subject_code": "SEC-TEST-101",
    "subject_name": "Nothing In Particular",
    "instructor": "",
    "day": "Monday",
    "course": "BSIT",
    "section": "A",
    "time_in": "08:00",
    "time_out": "10:00",
}


# ---------------------------------------------------------------------------
# R5
# ---------------------------------------------------------------------------


def test_editing_a_subject_that_does_not_exist_is_a_404(
    client, no_database, monkeypatch
):
    monkeypatch.setattr(
        no_database.subjects_repo, "get", lambda _cursor, _id: None
    )

    sign_in(client)
    response = client.get("/edit_subject/999999")

    assert response.status_code == 404, (
        "a missing subject reached the template as None; edit_subject.html "
        "then calls url_for(..., id=subject.id) and raises"
    )


def test_editing_a_subject_that_exists_still_renders(
    client, no_database, monkeypatch
):
    """The ordinary path, so the guard above cannot be a blanket 404."""
    monkeypatch.setattr(
        no_database.subjects_repo,
        "get",
        lambda _cursor, _id: {
            "id": 1,
            "subject_code": "SEC-TEST-101",
            "subject_name": "Nothing In Particular",
            "instructor": "",
            "day": "Monday",
            "course": "BSIT",
            "section": "A",
            "time_in": "08:00",
            "time_out": "10:00",
        },
    )

    sign_in(client)

    assert client.get("/edit_subject/1").status_code == 200


# ---------------------------------------------------------------------------
# R10
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("missing", ["subject_code", "subject_name"])
def test_a_missing_required_field_is_named(client, no_database, missing):
    form = {key: value for key, value in COMPLETE_FORM.items() if key != missing}

    sign_in(client)
    response = client.post("/add_subject", data=form)
    body = response.get_data(as_text=True)

    assert response.status_code == 400
    assert missing.replace("_", " ") in body, (
        "the operator was told the request could not be processed and left to "
        f"guess which of eight fields was at fault; expected {missing!r} named"
    )


def test_the_optional_fields_stay_optional(client, no_database, monkeypatch):
    """
    A subject with no instructor and no scheduled time is a state the schema
    allows on purpose - `subjects.time_in` is TIME NULL and RE-4 made
    `instructor_id` nullable. Validating all eight fields would refuse it.
    """
    inserted = []

    monkeypatch.setattr(
        no_database.subjects_repo,
        "insert",
        lambda _cursor, *values: inserted.append(values),
    )

    form = dict(COMPLETE_FORM, instructor="", time_in="", time_out="")

    sign_in(client)
    response = client.post("/add_subject", data=form, follow_redirects=False)

    assert response.status_code == 302
    assert len(inserted) == 1


# ---------------------------------------------------------------------------
# R14
# ---------------------------------------------------------------------------


def test_a_database_failure_is_a_500_with_context(
    client, no_database, monkeypatch, caplog
):
    def explode(_cursor, *_values):
        raise mysql.connector.Error("the server went away")

    monkeypatch.setattr(no_database.subjects_repo, "insert", explode)

    sign_in(client)

    with caplog.at_level("ERROR"):
        response = client.post("/add_subject", data=COMPLETE_FORM)

    assert response.status_code == 500
    assert "the server went away" not in response.get_data(as_text=True), (
        "the driver's message reached the client (SE-10)"
    )
    assert any(
        "SEC-TEST-101" in record.getMessage() for record in caplog.records
    ), (
        "the log does not say which subject failed - the catch-all handler "
        "records only the URL, which is R14"
    )
