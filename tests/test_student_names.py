"""
Real Filipino names are accepted, through the routes that take them (US-11).

**The finding.** Reported by the user 2026-08-29: `Juan Dela Cruz Jr.` was
refused at enrolment, and with it every `Jr.` and `Sr.` - the two commonest
suffixes in the names this system exists to handle, in the country it is
deployed in.

⚠️ **The rule was on the last character only**, so `Ma. Teresa Santos` and
`Jose P. Rizal` were always accepted. They are in the list below anyway,
because a fix that widened the allowlist instead of removing the positional
rule would look identical on `Jr.` and quietly change what else gets in.

`validate_student_name()` refused any name ending in a period. That was correct
**when it was written**: the dataset folder was `{student_id}_{student_name}`,
so the name was a path component, and Windows silently strips trailing dots
from those - `Jose Jr.` would have been created as `Jose Jr` and never found
again. Phase 5 made the folder `dataset/{student_id}` and the name stopped
being part of any path, but the rule stayed, guarding a filesystem behaviour it
could no longer reach and rejecting real students instead.

⚠️ **Through the routes, not the validator.** `tests/test_dataset_paths.py`
covers the rule itself. This file exists because the reported symptom was "the
system will not accept my name", and only a request can show that the whole
chain - form, validator, page - now does. That is lessons.md L5.

Every identifier here is fake (lessons.md L6).
"""

from __future__ import annotations

import contextlib

import pytest

from security.passwords import verify_password

# The forms the user reported, plus the two the allowlist has always had to
# carry: `Peña` is why the check is `str.isalnum()` rather than an ASCII
# pattern, and `O'Brien` is why the apostrophe is allowed (US-5).
FILIPINO_NAMES = [
    "Juan Dela Cruz Jr.",
    "Jose Rizal Sr.",
    "Ma. Teresa Santos",
    "Jose P. Rizal",
    "Andres Bonifacio III",
    "Maria Peña",
    "Siobhan O'Brien",
]

STUDENT = "SEC-TEST-NOBODY"


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


@pytest.fixture
def no_database(monkeypatch):
    """
    A cursor that answers nothing, for the routes that only have to get as far
    as validating. Nothing here is a question about SQL.
    """
    import web.enrolment as enrolment_module

    @contextlib.contextmanager
    def fake_cursor(*_args, **_kwargs):
        yield object()

    monkeypatch.setattr(enrolment_module, "db_cursor", fake_cursor)
    monkeypatch.setattr(
        enrolment_module.students_repo, "exists", lambda *args: False
    )


@pytest.mark.parametrize("name", FILIPINO_NAMES)
def test_enrolment_accepts_the_name(client, no_database, name):
    """The reported path: Register Student -> the capture page."""
    sign_in(client)

    response = client.post("/enrol", data={
        "mode": "new",
        "student_id": STUDENT,
        "name": name,
    })

    assert response.status_code == 200, (
        f"{name!r} was refused at enrolment with "
        f"{response.status_code}: {response.get_data(as_text=True)[:200]}"
    )


@pytest.mark.parametrize("name", FILIPINO_NAMES)
def test_the_capture_page_starts_for_the_name(client, monkeypatch, name):
    """
    ⚠️ **The second validation, and it is a different call site.**
    `/enrol/start` re-validates the name out of the JSON payload rather than
    trusting the page, so a rule fixed in one place and not the other would
    accept the student on the form and refuse them a step later - after the
    operator had already pointed a camera at them.
    """
    import web.enrolment as enrolment_module

    sign_in(client)

    started = {}

    class StubSession:
        def progress(self):
            return type("P", (), {"as_dict": lambda self: {}})()

    def fake_start(*, student_id, student_name, started_by, record):
        started["name"] = student_name
        return StubSession()

    monkeypatch.setattr(enrolment_module.enrolment_slot, "current", lambda: None)
    monkeypatch.setattr(enrolment_module.enrolment_slot, "start", fake_start)

    response = client.post("/enrol/start", json={
        "student_id": STUDENT,
        "student_name": name,
        "record": {},
    })

    assert response.status_code == 201, (
        f"{name!r} was refused by /enrol/start: "
        f"{response.get_data(as_text=True)[:200]}"
    )
    assert started["name"] == name


@pytest.mark.parametrize("name", FILIPINO_NAMES)
def test_editing_a_student_accepts_the_name(client, monkeypatch, name):
    """
    The other way a name enters the database. A student enrolled before this
    was fixed has to be correctable, and `update_student` runs the same
    validator.
    """
    import web.students as students_module

    @contextlib.contextmanager
    def fake_cursor(*_args, **_kwargs):
        yield object()

    monkeypatch.setattr(students_module, "db_cursor", fake_cursor)
    monkeypatch.setattr(
        students_module.students_repo,
        "update_details",
        lambda *args, **kwargs: 1,
    )

    sign_in(client)

    response = client.post(f"/update_student/{STUDENT}", data={
        "name": name,
        "college_department": "College of Computing",
        "program": "BSCS",
        "year_level": "3",
        "section": "B",
    })

    assert response.status_code in (200, 302), (
        f"{name!r} was refused when editing a student: "
        f"{response.get_data(as_text=True)[:200]}"
    )


def test_a_name_that_is_only_punctuation_is_still_refused(
    client, no_database
):
    """
    The rule that replaced the trailing-dot one. A period is legitimate inside
    a name, so `..` clears the character allowlist; it is refused for not being
    a name, which is a statement about names rather than about filesystems.
    """
    sign_in(client)

    response = client.post("/enrol", data={
        "mode": "new",
        "student_id": STUDENT,
        "name": "..",
    })

    assert response.status_code == 400


def test_editing_a_student_hashes_a_new_password(client, monkeypatch):
    import web.students as students_module

    calls = []

    @contextlib.contextmanager
    def fake_cursor(*_args, **_kwargs):
        yield object()

    def update_details_and_password(*args):
        calls.append(args)
        return 1

    monkeypatch.setattr(students_module, "db_cursor", fake_cursor)
    monkeypatch.setattr(
        students_module.students_repo,
        "update_details_and_password",
        update_details_and_password,
    )

    sign_in(client)

    response = client.post(f"/update_student/{STUDENT}", data={
        "name": "Updated Student",
        "college_department": "College of Computing",
        "program": "BSCS",
        "year_level": "3",
        "section": "B",
        "password": "new-secret",
    })

    assert response.status_code == 302
    assert len(calls) == 1
    assert verify_password("new-secret", calls[0][-1]) is True
    assert calls[0][-1] != "new-secret"


def test_editing_a_student_rejects_a_short_password_without_updating(
    client, monkeypatch
):
    import web.students as students_module

    updates = []

    @contextlib.contextmanager
    def fake_cursor(*_args, **_kwargs):
        yield object()

    monkeypatch.setattr(students_module, "db_cursor", fake_cursor)
    monkeypatch.setattr(
        students_module.students_repo,
        "update_details_and_password",
        lambda *args: updates.append(args),
    )

    sign_in(client)

    response = client.post(f"/update_student/{STUDENT}", data={
        "name": "Updated Student",
        "college_department": "College of Computing",
        "program": "BSCS",
        "year_level": "3",
        "section": "B",
        "password": "short",
    })

    assert response.status_code == 400
    assert updates == []


def test_a_path_separator_in_a_name_is_still_refused(client, no_database):
    """
    SE-3 has not been relaxed. The name no longer becomes a path, but the
    allowlist is what keeps a hostile value out of the database in the first
    place, and widening it for `Jr.` must not have widened it for this.
    """
    sign_in(client)

    for hostile in ("../etc/passwd", "Jose/../..", "Jose\\Cruz"):
        response = client.post("/enrol", data={
            "mode": "new",
            "student_id": STUDENT,
            "name": hostile,
        })

        assert response.status_code == 400, f"{hostile!r} was accepted"
