"""
Enrolment writes the student's class list with the student row (US-10).

**The finding.** `/enrol/finish` wrote the `students` row and started a
retrain, and nothing anywhere in the enrolment path wrote `enrolments` - the
relation `save_attendance()` actually checks (FS-3). That relation was created
only from Subjects -> Class List, a screen the enrolment flow never linked to
or mentioned. So the obvious path - enrol a student, start a session, stand
them in front of the camera - ended in "Not Enrolled" for a student who had
just been enrolled, and nothing said so until the camera did.

Reported by the user 2026-08-29 from a live run.

**What is pinned here**, in the order it matters:

* the ticked subjects survive the whole capture and are written at the end
* they are written in the *same transaction* as the student row, so a student
  cannot commit without the class list that makes them recordable
* a subject deleted during the capture is dropped, not fatal
* a recapture does not touch the class list - it replaces a face, not a
  timetable
* the "in no class yet" warning is flashed, so it survives the redirect the
  capture page performs a second and a half after finishing

**No camera, no MediaPipe, no database.** The capture session is a stub with
`done` already true, because none of the above is a question about capturing.
Every identifier is fake (lessons.md L6).
"""

from __future__ import annotations

import contextlib

import pytest

STUDENT = "SEC-TEST-NOBODY"
NAME = "Test Student"


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


class StubCapture:
    """A finished capture. `record` is the bag the subject ids ride in."""

    def __init__(self, record):
        self.done = True
        self.captured = 100
        self.student_id = STUDENT
        self.student_name = NAME
        self.record = record


class Recorder:
    def __init__(self):
        self.events = []
        self.existing_subject_ids = []
        self.student_exists = False
        self.class_count = 0

    def names(self):
        return [event[0] for event in self.events]


@pytest.fixture
def wired(monkeypatch):
    """
    Everything /enrol/finish touches, replaced.

    The cursor handed out is a fresh sentinel per `with`, and every call
    records the one it was given - so a test can assert two writes shared a
    transaction rather than merely that both happened.
    """
    import web.enrolment as enrolment_module

    log = Recorder()

    @contextlib.contextmanager
    def fake_cursor(*_args, **_kwargs):
        cursor = object()
        log.events.append(("open", cursor))
        yield cursor
        log.events.append(("commit", cursor))

    monkeypatch.setattr(enrolment_module, "db_cursor", fake_cursor)

    monkeypatch.setattr(
        enrolment_module.dataset_store, "promote",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        enrolment_module.training_job, "start", lambda **kwargs: None
    )
    monkeypatch.setattr(
        enrolment_module.enrolment_slot, "release", lambda: None
    )

    monkeypatch.setattr(
        enrolment_module.students_repo, "exists",
        lambda cursor, student_id: log.student_exists,
    )
    monkeypatch.setattr(
        enrolment_module.students_repo, "insert",
        lambda cursor, student_id, name, record: log.events.append(
            ("insert", cursor, record)
        ),
    )
    monkeypatch.setattr(
        enrolment_module.students_repo, "rename",
        lambda cursor, student_id, name: log.events.append(
            ("rename", cursor, None)
        ),
    )
    monkeypatch.setattr(
        enrolment_module.subjects_repo, "existing_ids",
        lambda cursor, ids: [i for i in ids if i in log.existing_subject_ids],
    )
    monkeypatch.setattr(
        enrolment_module.enrolments_repo, "enrol_many",
        lambda cursor, student_id, ids: log.events.append(
            ("enrol_many", cursor, list(ids))
        ),
    )
    monkeypatch.setattr(
        enrolment_module.enrolments_repo, "count_for_student",
        lambda cursor, student_id: log.class_count,
    )

    return log


def finish(client, monkeypatch, record):
    import web.enrolment as enrolment_module

    monkeypatch.setattr(
        enrolment_module.enrolment_slot, "current",
        lambda: StubCapture(record),
    )

    return client.post("/enrol/finish")


def flashes(client):
    with client.session_transaction() as session:
        return session.get("_flashes", [])


def render_enrol_page(client, monkeypatch, subject_ids):
    """POST /enrol the way the registration form does, and return the HTML."""
    import web.enrolment as enrolment_module

    @contextlib.contextmanager
    def fake_cursor(*_args, **_kwargs):
        yield object()

    monkeypatch.setattr(enrolment_module, "db_cursor", fake_cursor)
    monkeypatch.setattr(
        enrolment_module.students_repo, "exists", lambda *args: False
    )

    sign_in(client)

    return client.post("/enrol", data={
        "mode": "new",
        "student_id": STUDENT,
        "name": NAME,
        "subject_ids": subject_ids,
    }).get_data(as_text=True)


# ==============================
# The choice survives the capture
# ==============================


def test_the_registration_form_carries_the_chosen_subjects(
    client, monkeypatch
):
    """
    The first link in the chain: /enrol renders the capture page, and the ids
    have to reach it or nothing later can write them.

    They travel inside `record`, which `vision/` and `services/` treat as
    opaque and hand back untouched at the end - so the choice needs no new
    plumbing through three layers, and never becomes a column on `students`.
    """
    page = render_enrol_page(client, monkeypatch, ["3", "7"])

    assert '"subject_ids": [3, 7]' in page.replace("&#34;", '"')


def test_a_non_numeric_subject_id_is_dropped(client, monkeypatch):
    """
    The multi-select is filled in by the browser, so anything else is tampering
    rather than a typo. It is dropped here, and the ids that survive are still
    checked against the subjects table before anything is written.
    """
    page = render_enrol_page(
        client, monkeypatch, ["3", "; DROP TABLE students", ""]
    )

    assert '"subject_ids": [3]' in page.replace("&#34;", '"')


# ==============================
# The write
# ==============================


def test_the_class_list_is_written_with_the_student_row(
    client, monkeypatch, wired
):
    """US-10 itself. Before this, only the student row was ever written."""
    wired.existing_subject_ids = [3, 7]
    wired.class_count = 2
    sign_in(client)

    response = finish(client, monkeypatch, {"subject_ids": [3, 7]})

    assert response.status_code == 200

    enrolled = [event for event in wired.events if event[0] == "enrol_many"]

    assert len(enrolled) == 1
    assert enrolled[0][2] == [3, 7]


def test_the_row_and_the_class_list_share_one_transaction(
    client, monkeypatch, wired
):
    """
    ⚠️ **The property, not the call.** A student row that committed while its
    class list did not is exactly the state US-10 describes - known to the
    model, refused by the register - so it must not be reachable by a partial
    write. `db_cursor()` is one transaction per `with`, so sharing the cursor
    object *is* sharing the transaction.
    """
    wired.existing_subject_ids = [3]
    wired.class_count = 1
    sign_in(client)

    finish(client, monkeypatch, {"subject_ids": [3]})

    insert = next(e for e in wired.events if e[0] == "insert")
    enrol = next(e for e in wired.events if e[0] == "enrol_many")

    assert insert[1] is enrol[1], (
        "the student row and the class list were written through different "
        "cursors, so they can commit separately"
    )

    names = wired.names()
    between = names[names.index("insert"):names.index("enrol_many")]

    assert "commit" not in between, "a transaction closed between the two"


def test_the_class_list_never_reaches_the_students_insert(
    client, monkeypatch, wired
):
    """
    `subject_ids` is a relation, not a column. It rides inside `record`
    because that bag is opaque to the layers in between, and it has to be taken
    out again before the record is handed to the INSERT.
    """
    wired.existing_subject_ids = [3]
    wired.class_count = 1
    sign_in(client)

    finish(client, monkeypatch, {"program": "BSCS", "subject_ids": [3]})

    insert = next(e for e in wired.events if e[0] == "insert")

    assert "subject_ids" not in insert[2]
    assert insert[2]["program"] == "BSCS", "the real record was damaged"


def test_a_subject_deleted_during_the_capture_is_dropped(
    client, monkeypatch, wired
):
    """
    A capture takes minutes and a subject can be deleted inside one. The
    foreign key would refuse the whole write - rolling back a student whose
    images are already promoted to disk - so the surviving subjects are written
    and the rest is logged.
    """
    wired.existing_subject_ids = [3]
    wired.class_count = 1
    sign_in(client)

    response = finish(client, monkeypatch, {"subject_ids": [3, 999]})

    assert response.status_code == 200

    enrolled = next(e for e in wired.events if e[0] == "enrol_many")

    assert enrolled[2] == [3]


def test_a_recapture_does_not_touch_the_class_list(client, monkeypatch, wired):
    """Recapture replaces a face, not a timetable."""
    wired.student_exists = True
    wired.existing_subject_ids = [3]
    wired.class_count = 1
    sign_in(client)

    finish(client, monkeypatch, {"subject_ids": [3]})

    assert "rename" in wired.names()
    assert "enrol_many" not in wired.names()


# ==============================
# Saying so - the half that catches everybody else
# ==============================


def test_enrolling_into_no_class_warns_and_survives_the_redirect(
    client, monkeypatch, wired
):
    """
    ⚠️ **flash(), not the JSON message.** The capture page shows the response
    message for about 1.5 s and then navigates to Manage Students, so a warning
    put there is one nobody finishes reading. A flash is rendered by base.html
    on the page they land on.

    This is the case a control on the registration form cannot reach: students
    enrolled before subject selection existed, and anyone unenrolled from every
    class afterwards.
    """
    wired.class_count = 0
    sign_in(client)

    finish(client, monkeypatch, {})

    queued = flashes(client)

    assert queued, "nothing was flashed, so the redirect loses the warning"

    category, message = queued[-1]

    assert category == "warning"
    assert "not in any class" in message
    assert "attendance cannot be recorded" in message


def test_enrolling_into_classes_confirms_how_many(client, monkeypatch, wired):
    """
    US-1/US-2: an outcome that is not a failure must not be silent, and the
    confirmation names what happened rather than only that something did.
    """
    wired.existing_subject_ids = [3, 7]
    wired.class_count = 2
    sign_in(client)

    finish(client, monkeypatch, {"subject_ids": [3, 7]})

    category, message = flashes(client)[-1]

    assert category == "success"
    assert "2 classes" in message


def test_one_class_is_not_reported_as_1_classes(client, monkeypatch, wired):
    wired.existing_subject_ids = [3]
    wired.class_count = 1
    sign_in(client)

    finish(client, monkeypatch, {"subject_ids": [3]})

    assert "1 class." in flashes(client)[-1][1]
