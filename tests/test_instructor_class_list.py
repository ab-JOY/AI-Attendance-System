"""
Instructors build the class lists of their own subjects (expert panel request,
2026-10-23).

The routes are the administrator's Class List routes. What is tested here is
the part that differs: an instructor may read and change the class list of a
subject assigned to them and of no other, and the placement rules (DM-12)
still apply to them. No database - every repository call is replaced - and
every identifier is fake (lessons.md L6).
"""

from __future__ import annotations

import contextlib

import pytest

from security.access import INSTRUCTOR_KEY

OWN_SUBJECT = 999999991
OTHER_SUBJECT = 999999992
INSTRUCTOR = 999999901
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


@contextlib.contextmanager
def fake_cursor(*_args, **_kwargs):
    yield object()


def offering(subject_id):
    return {
        "id": subject_id, "subject_code": "SEC101",
        "subject_name": "Test Subject", "course": "BSIT", "section": "A",
        "time_in": None, "section_id": 1, "program_id": 1, "year_level": 1,
    }


@pytest.fixture
def school(monkeypatch):
    """
    Two subjects, one assigned to INSTRUCTOR. Records every repository call
    that reads a class list or writes one, so a refusal can be shown to have
    happened before any of them.
    """
    import web.subjects as subjects_module

    calls = []

    def recorded(name, result=None):
        def call(_cursor, *args, **_kwargs):
            calls.append((name, *args))
            return result
        return call

    monkeypatch.setattr(subjects_module, "db_cursor", fake_cursor)
    monkeypatch.setattr(
        subjects_module.subjects_repo, "is_taught_by",
        lambda _cursor, subject_id, instructor_id: (
            subject_id == OWN_SUBJECT and instructor_id == INSTRUCTOR
        ),
    )
    monkeypatch.setattr(
        subjects_module.subjects_repo, "summary",
        lambda _cursor, subject_id: (
            calls.append(("summary", subject_id)) or offering(subject_id)
        ),
    )
    repo = subjects_module.enrolments_repo
    monkeypatch.setattr(repo, "enrolled_in", recorded("enrolled_in", []))
    monkeypatch.setattr(repo, "not_enrolled_in", recorded("not_enrolled_in", []))
    monkeypatch.setattr(repo, "placement", recorded("placement", repo.REGULAR))
    monkeypatch.setattr(repo, "enrol", recorded("enrol"))
    monkeypatch.setattr(repo, "enrol_section", recorded("enrol_section", 3))
    monkeypatch.setattr(repo, "unenrol", recorded("unenrol"))

    return calls


def sign_in(client, role="instructor", instructor=INSTRUCTOR):
    with client.session_transaction() as session:
        session["user"] = f"test-{role}"
        session["role"] = role

        if role == "instructor" and instructor is not None:
            session[INSTRUCTOR_KEY] = instructor


def change(client, path, subject_id, **form):
    return client.post(path, data={"subject_id": str(subject_id), **form})


# ==============================
# Their own subject
# ==============================


def test_an_instructor_opens_the_class_list_of_their_subject(client, school):
    sign_in(client)

    response = client.get(f"/subject_enrolments/{OWN_SUBJECT}")

    assert response.status_code == 200
    assert b"Class List" in response.data
    # Subjects is an admin page; the way back is the page they came from.
    assert b'href="/my_classes"' in response.data
    assert b'href="/subjects"' not in response.data


def test_an_instructor_imports_a_registered_student(client, school):
    sign_in(client)

    response = change(client, "/enrol_student", OWN_SUBJECT, student_id=STUDENT)

    assert response.status_code == 302
    assert ("enrol", STUDENT, OWN_SUBJECT) in school


def test_an_instructor_imports_their_whole_section(client, school):
    sign_in(client)

    response = change(client, "/enrol_section", OWN_SUBJECT)

    assert response.status_code == 302
    assert ("enrol_section", OWN_SUBJECT, 1) in school


def test_an_instructor_removes_a_student_from_their_class(client, school):
    sign_in(client)

    response = change(client, "/unenrol_student", OWN_SUBJECT, student_id=STUDENT)

    assert response.status_code == 302
    assert ("unenrol", STUDENT, OWN_SUBJECT) in school


# ==============================
# Someone else's subject
# ==============================


def test_an_instructor_cannot_read_another_instructors_class_list(client, school):
    sign_in(client)

    response = client.get(f"/subject_enrolments/{OTHER_SUBJECT}")

    assert response.status_code == 403
    assert school == []


@pytest.mark.parametrize("path, form", [
    ("/enrol_student", {"student_id": STUDENT}),
    ("/enrol_section", {}),
    ("/unenrol_student", {"student_id": STUDENT}),
])
def test_an_instructor_cannot_change_another_instructors_class_list(
    client, school, path, form
):
    """⚠️ Refused before the repository is asked anything, let alone written."""
    sign_in(client)

    response = change(client, path, OTHER_SUBJECT, **form)

    assert response.status_code == 403
    assert school == []


@pytest.mark.parametrize("path, form", [
    ("/enrol_student", {"student_id": STUDENT}),
    ("/enrol_section", {}),
    ("/unenrol_student", {"student_id": STUDENT}),
])
def test_a_session_without_an_instructor_key_changes_nothing(
    client, school, path, form
):
    """
    A session from before `instructor_pk` existed is scoped to nobody
    (`instructor_scope` fails closed), so even their own subject is refused.
    """
    sign_in(client, instructor=None)

    response = change(client, path, OWN_SUBJECT, **form)

    assert response.status_code == 403
    assert school == []


# ==============================
# The placement rules still apply
# ==============================


def test_an_instructor_cannot_import_another_programs_student(
    client, school, monkeypatch
):
    import web.subjects as subjects_module

    repo = subjects_module.enrolments_repo
    monkeypatch.setattr(repo, "placement", lambda *_args: repo.OTHER_PROGRAM)
    sign_in(client)

    response = change(
        client, "/enrol_student", OWN_SUBJECT, student_id=STUDENT, irregular="1"
    )

    assert response.status_code == 400
    assert not any(call[0] == "enrol" for call in school)


def test_an_instructor_adds_an_irregular_student_only_on_purpose(
    client, school, monkeypatch
):
    import web.subjects as subjects_module

    repo = subjects_module.enrolments_repo
    monkeypatch.setattr(repo, "placement", lambda *_args: repo.IRREGULAR)
    sign_in(client)

    refused = change(client, "/enrol_student", OWN_SUBJECT, student_id=STUDENT)

    assert refused.status_code == 400
    assert not any(call[0] == "enrol" for call in school)

    added = change(
        client, "/enrol_student", OWN_SUBJECT, student_id=STUDENT, irregular="1"
    )

    assert added.status_code == 302
    assert ("enrol", STUDENT, OWN_SUBJECT) in school


# ==============================
# The administrator is unchanged
# ==============================


def test_an_administrator_changes_any_class_list(client, school, monkeypatch):
    """No ownership question is asked of an administrator at all."""
    import web.subjects as subjects_module

    def must_not_be_asked(*_args):
        raise AssertionError("is_taught_by was called for an administrator")

    monkeypatch.setattr(
        subjects_module.subjects_repo, "is_taught_by", must_not_be_asked
    )
    sign_in(client, role="admin")

    page = client.get(f"/subject_enrolments/{OTHER_SUBJECT}")
    response = change(client, "/enrol_student", OTHER_SUBJECT, student_id=STUDENT)

    assert page.status_code == 200
    assert b'href="/subjects"' in page.data
    assert response.status_code == 302
    assert ("enrol", STUDENT, OTHER_SUBJECT) in school


# ==============================
# My Classes
# ==============================


def test_my_classes_lists_only_the_instructors_subjects(client, monkeypatch):
    """
    Asked for this instructor's offerings, with the schedule and the class
    list size, and each row leads to its Class List.
    """
    from datetime import timedelta

    import web.subjects as subjects_module

    asked_for = []

    def classes(_cursor, instructor_id=None):
        asked_for.append(instructor_id)
        return [{
            **offering(OWN_SUBJECT), "day": "Mon,Wed",
            "time_in": timedelta(hours=7, minutes=30),
            "time_out": timedelta(hours=9), "enrolled": 0,
        }]

    monkeypatch.setattr(subjects_module, "db_cursor", fake_cursor)
    monkeypatch.setattr(
        subjects_module.subjects_repo,
        "for_selection_with_class_list_size", classes,
    )
    sign_in(client)

    response = client.get("/my_classes")
    page = response.get_data(as_text=True)

    assert response.status_code == 200
    assert asked_for == [INSTRUCTOR]
    assert "SEC101" in page
    assert "07:30–09:00" in page
    assert "none yet" in page
    assert f'href="/subject_enrolments/{OWN_SUBJECT}"' in page


def test_my_classes_is_in_the_instructors_menu(client, monkeypatch):
    import web.subjects as subjects_module

    monkeypatch.setattr(subjects_module, "db_cursor", fake_cursor)
    monkeypatch.setattr(
        subjects_module.subjects_repo,
        "for_selection_with_class_list_size", lambda *_args, **_kwargs: [],
    )
    sign_in(client)

    page = client.get("/my_classes").get_data(as_text=True)

    assert 'href="/my_classes"' in page
    assert "no subjects assigned" in page


def test_my_classes_is_for_instructors(client):
    """An administrator has the full Subjects page instead."""
    sign_in(client, role="admin")

    assert client.get("/my_classes").status_code == 403
