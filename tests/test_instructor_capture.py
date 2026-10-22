"""
Instructors capture the faces of their own classes, through the admin's flow.

The routes are the administrator's /enrol routes. What is tested here is the
part that differs: which students an instructor may capture, and that a
capture belongs to the login that started it. No database - every repository
call is replaced - and every identifier is fake (lessons.md L6). Staging and
dataset are redirected to tmp_path.
"""

from __future__ import annotations

import contextlib

import pytest

from config.settings import settings
from security.access import INSTRUCTOR_KEY

OWN_STUDENT = "SEC-TEST-NOBODY"
OTHER_STUDENT = "SEC-TEST-NOONE"
NEW_STUDENT = "SEC-TEST-NEWBIE"
OWN_SUBJECT = 999999991
OTHER_SUBJECT = 999999992
INSTRUCTOR = 999999901
OTHER_INSTRUCTOR = 999999902


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


@pytest.fixture
def second_client(flask_app):
    return flask_app.test_client()


@pytest.fixture(autouse=True)
def scratch_dirs(tmp_path, monkeypatch):
    dataset = tmp_path / "dataset"
    staging = tmp_path / "dataset_staging"
    dataset.mkdir()
    staging.mkdir()

    monkeypatch.setattr(settings, "dataset_dir", dataset)
    monkeypatch.setattr(settings, "dataset_staging_dir", staging)

    return dataset, staging


@pytest.fixture(autouse=True)
def no_running_enrolment():
    import web.enrolment as enrolment_module

    yield

    enrolment_module.enrolment_slot.abandon()


@contextlib.contextmanager
def fake_cursor(*_args, **_kwargs):
    yield object()


@pytest.fixture(autouse=True)
def fake_school(monkeypatch):
    """
    Two students who exist - one in the instructor's class, one not - and two
    subjects, one theirs. Anyone else does not exist yet.
    """
    import web.enrolment as enrolment_module

    existing = {OWN_STUDENT: "Own Student", OTHER_STUDENT: "Other Student"}

    monkeypatch.setattr(enrolment_module, "db_cursor", fake_cursor)
    monkeypatch.setattr(
        enrolment_module.students_repo, "exists",
        lambda _cursor, student_id: student_id in existing,
    )
    monkeypatch.setattr(
        enrolment_module.students_repo, "identity",
        lambda _cursor, student_id: (
            {"student_id": student_id, "name": existing[student_id]}
            if student_id in existing else None
        ),
    )
    monkeypatch.setattr(
        enrolment_module.enrolments_repo, "is_in_class_of",
        lambda _cursor, student_id, instructor_id: (
            student_id == OWN_STUDENT and instructor_id == INSTRUCTOR
        ),
    )
    monkeypatch.setattr(
        enrolment_module.subjects_repo, "is_taught_by",
        lambda _cursor, subject_id, instructor_id: (
            subject_id == OWN_SUBJECT and instructor_id == INSTRUCTOR
        ),
    )


def sign_in(client, role="instructor", instructor=INSTRUCTOR):
    with client.session_transaction() as session:
        session["user"] = f"test-{role}"
        session["role"] = role

        if role == "instructor":
            session[INSTRUCTOR_KEY] = instructor


def start(client, student_id, subject_ids=None, name="Test Student"):
    record = {} if subject_ids is None else {"subject_ids": subject_ids}

    return client.post(
        "/enrol/start",
        json={"student_id": student_id, "student_name": name, "record": record},
    )


# ==============================
# Who an instructor may capture
# ==============================


def test_an_instructor_recaptures_a_student_in_their_class(client, scratch_dirs):
    _, staging = scratch_dirs
    sign_in(client)

    response = start(client, OWN_STUDENT)

    assert response.status_code == 201
    assert (staging / OWN_STUDENT).is_dir()


def test_an_instructor_cannot_recapture_another_instructors_student(
    client, scratch_dirs
):
    """A finished recapture replaces the student's images - so it is refused at start."""
    _, staging = scratch_dirs
    sign_in(client)

    response = start(client, OTHER_STUDENT)

    assert response.status_code == 403
    assert not (staging / OTHER_STUDENT).exists()


def test_naming_an_existing_student_as_new_is_still_a_recapture(client):
    """
    /enrol/finish treats any existing ID as a recapture and renames the
    student, so a "new" record with one of the instructor's classes attached
    must not get them past the ownership check.
    """
    sign_in(client)

    assert start(client, OTHER_STUDENT, [OWN_SUBJECT]).status_code == 403


def test_a_new_student_must_be_put_in_one_of_the_instructors_classes(client):
    sign_in(client)

    assert start(client, NEW_STUDENT, []).status_code == 403
    assert start(client, NEW_STUDENT, [OTHER_SUBJECT]).status_code == 403
    assert start(client, NEW_STUDENT, [OWN_SUBJECT, OTHER_SUBJECT]).status_code == 403

    assert start(client, NEW_STUDENT, [OWN_SUBJECT]).status_code == 201


@pytest.mark.parametrize("subject_ids", ["1", [True], ["1"], [1.5], {"a": 1}])
def test_a_class_list_that_is_not_a_list_of_ids_is_refused(client, subject_ids):
    sign_in(client)

    assert start(client, NEW_STUDENT, subject_ids).status_code == 400


def test_an_instructor_with_no_key_in_their_session_captures_nobody(client):
    """Fails closed, through instructor_scope()."""
    with client.session_transaction() as session:
        session["user"] = "test-instructor"
        session["role"] = "instructor"

    assert start(client, OWN_STUDENT).status_code == 403
    assert start(client, NEW_STUDENT, [OWN_SUBJECT]).status_code == 403


def test_the_capture_page_refuses_another_instructors_student(client):
    sign_in(client)

    response = client.post(
        "/enrol", data={"mode": "recapture", "student_id": OTHER_STUDENT}
    )

    assert response.status_code == 403


def test_the_capture_page_returns_an_instructor_to_face_capture(client):
    sign_in(client)

    response = client.post(
        "/enrol", data={"mode": "recapture", "student_id": OWN_STUDENT}
    )

    assert response.status_code == 200
    assert b'data-done-url="/class_capture"' in response.data


def test_the_capture_page_returns_an_admin_to_manage_students(client):
    sign_in(client, role="admin")

    response = client.post(
        "/enrol", data={"mode": "recapture", "student_id": OTHER_STUDENT}
    )

    assert response.status_code == 200
    assert b'data-done-url="/manage_students"' in response.data


def test_an_administrator_is_not_narrowed(client):
    sign_in(client, role="admin")

    assert start(client, OTHER_STUDENT).status_code == 201


# ==============================
# A capture belongs to who started it
# ==============================


@pytest.mark.parametrize("path", ["/enrol/frame", "/enrol/finish", "/enrol/cancel"])
def test_another_instructor_cannot_drive_the_capture(
    client, second_client, scratch_dirs, path
):
    _, staging = scratch_dirs
    sign_in(client)
    assert start(client, OWN_STUDENT).status_code == 201

    sign_in(second_client, instructor=OTHER_INSTRUCTOR)

    response = second_client.post(path, data=b"", content_type="image/jpeg")

    assert response.status_code == 403
    assert (staging / OWN_STUDENT).is_dir(), "The capture must survive"


def test_the_instructor_who_started_it_can_cancel_it(client, scratch_dirs):
    _, staging = scratch_dirs
    sign_in(client)
    start(client, OWN_STUDENT)

    response = client.post("/enrol/cancel")

    assert response.status_code == 200
    assert not (staging / OWN_STUDENT).exists()


def test_an_administrator_can_still_cancel_an_instructors_capture(
    client, second_client, scratch_dirs
):
    _, staging = scratch_dirs
    sign_in(client)
    start(client, OWN_STUDENT)

    sign_in(second_client, role="admin")

    assert second_client.post("/enrol/cancel").status_code == 200
    assert not (staging / OWN_STUDENT).exists()


# ==============================
# The Face Capture page
# ==============================


def test_the_face_capture_page_lists_only_the_instructors_classes(
    client, monkeypatch
):
    import web.enrolment as enrolment_module

    asked_for = []

    def offerings(_cursor, instructor_id=None):
        asked_for.append(instructor_id)
        return [{
            "id": OWN_SUBJECT, "subject_code": "SEC101",
            "subject_name": "Test Subject", "course": "BSIT", "section": "A",
            "time_in": None, "section_id": 1, "program_id": 1, "year_level": 1,
        }]

    monkeypatch.setattr(enrolment_module.subjects_repo, "for_selection", offerings)
    monkeypatch.setattr(
        enrolment_module.enrolments_repo, "enrolled_in",
        lambda _cursor, _subject_id: [{
            "student_id": OWN_STUDENT, "name": "Own Student",
            "program": "BSIT", "year_level": 1, "section": "A",
        }],
    )
    monkeypatch.setattr(
        enrolment_module.academics_repo, "programs", lambda _cursor: []
    )

    sign_in(client)
    response = client.get("/class_capture")

    assert response.status_code == 200
    assert asked_for == [INSTRUCTOR]
    assert b"SEC101" in response.data
    assert OWN_STUDENT.encode() in response.data
    # Their own Class List, where registered students are imported.
    assert f'href="/subject_enrolments/{OWN_SUBJECT}"'.encode() in response.data
    # The manual retrain is admin-only, so the button is not offered.
    assert b'id="retrain-button"\n            hidden' in response.data


def test_the_face_capture_page_is_for_instructors(client):
    sign_in(client, role="admin")

    assert client.get("/class_capture").status_code == 403
