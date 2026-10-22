"""
Instructor IDs may contain letters (e.g. INS-001), not only digits.

The Add Instructor field was `type="number"`, so a browser refused letters
although the column is VARCHAR(100) and login compares text. No database -
the insert is replaced and recorded.
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
def admin(flask_app, monkeypatch):
    import web.instructors as module

    inserted = []

    @contextlib.contextmanager
    def fake_cursor(*_args, **_kwargs):
        yield object()

    monkeypatch.setattr(module, "db_cursor", fake_cursor)
    monkeypatch.setattr(module, "_department_from_form", lambda _c: (None, None))
    monkeypatch.setattr(module.instructors_repo, "insert",
                        lambda _c, instructor_id, *_rest: inserted.append(instructor_id))

    flask_app.config["WTF_CSRF_ENABLED"] = False
    client = flask_app.test_client()

    with client.session_transaction() as session:
        session["user"], session["role"], session["admin_id"] = "admin", "admin", 1

    yield client, inserted
    flask_app.config["WTF_CSRF_ENABLED"] = True


def add(client, instructor_id):
    return client.post("/add_instructor", data={
        "instructor_id": instructor_id, "fullname": "Ana Cruz",
        "password": "temporary-123",
    })


@pytest.mark.parametrize("instructor_id", ["INS-001", "2024_ccit.07", "1234567890", "abc"])
def test_ids_with_letters_are_accepted(admin, instructor_id):
    client, inserted = admin

    assert add(client, instructor_id).status_code == 302
    assert inserted == [instructor_id]


@pytest.mark.parametrize("instructor_id", ["INS/001", "INS 001", "x" * 101, "ñame"])
def test_ids_that_would_break_the_edit_link_are_refused(admin, instructor_id):
    client, inserted = admin

    assert add(client, instructor_id).status_code == 400
    assert inserted == []


def test_the_form_field_takes_text():
    from pathlib import Path

    page = (Path(__file__).resolve().parent.parent / "templates" / "instructors.html").read_text(
        encoding="utf-8"
    )
    at = page.index('name="instructor_id"')
    field = page[page.rindex("<input", 0, at):page.index(">", at)]

    assert 'type="text"' in field
    assert 'type="number"' not in field
