"""
Ending a session cannot write another subject's register (R3, FS-3/FS-7).

**The defect.** The End form carries its own subject dropdown, defaulting to
"Choose a subject…", and the route trusted it. Choose a different offering than
the one running and the absent register was written for *that* offering - each
row stamped with the *running* session's `session_id`, which belongs to a
different subject - and the running session was then closed anyway.

Nothing errored, and that is the whole problem. The foreign key to
`attendance_sessions` resolves, the schema is satisfied, and the result is a
referentially valid register that is factually wrong: students marked absent
from a class that was never held, attributed to another subject's session. No
exception, no log line, no failing test.

**Why the tests could not see it.** Exactly as with R1 - the disagreement is
only expressible while a session is *running*, and nothing in the suite put the
session machine into that state. So this file does, at the seam
`web/sessions.py` imports the session under, and drives the real route.

The refusal happens before `stop_camera()` and before the transaction opens, so
a mis-selection costs nothing: nothing is written and the session survives. A
refusal that closed the session would make the mistake unrecoverable - the
register that session was for could then never be written at all.
"""

from __future__ import annotations

import contextlib
import re

import pytest

from recognize_face import ActiveSubject
from tests.conftest import PROJECT_ROOT

RUNNING = ActiveSubject(subject_id=1, session_id=7, subject_code="CS401")
OTHER_SUBJECT_ID = 2


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


class FakeSession:
    def __init__(self, subject):
        self.subject = subject


class Recorder:
    """Records what the route asked the database to do, and does none of it."""

    def __init__(self):
        self.absences = []
        self.registers_read = []
        self.closed_sessions = []
        self.camera_stopped = False

        # Today's unended `attendance_sessions` rows. This is what the route
        # consults once the in-memory session is gone, and it is the reason
        # FS-16 cannot come back: a client choosing what to POST first cannot
        # change what is written here.
        self.open_sessions = []


@pytest.fixture
def recorder(monkeypatch):
    import web.sessions as sessions_module

    log = Recorder()

    @contextlib.contextmanager
    def fake_cursor(*_args, **_kwargs):
        yield object()

    def code_of(_cursor, subject_id):
        return {"subject_code": f"SUBJECT-{subject_id}"}

    def register_for(_cursor, subject_id):
        log.registers_read.append(subject_id)
        return [
            {
                "student_id": "SEC-TEST-NOBODY",
                "name": "Nobody",
                "status": None,
                "time_in": None,
            }
        ]

    def insert_absences(_cursor, rows):
        log.absences.extend(rows)

    def close_session(_cursor, session_row_id):
        log.closed_sessions.append(session_row_id)

    # B3: the attendance page's picker carries a class-list size now, so the
    # stub follows `_all_subjects()` to the function it actually calls. The
    # list is empty either way - nothing in this file is about the dropdown.
    def for_selection_with_class_list_size(_cursor):
        return []

    monkeypatch.setattr(sessions_module, "db_cursor", fake_cursor)
    monkeypatch.setattr(sessions_module.subjects_repo, "code_of", code_of)
    monkeypatch.setattr(
        sessions_module.subjects_repo,
        "for_selection_with_class_list_size",
        for_selection_with_class_list_size,
    )
    monkeypatch.setattr(
        sessions_module.attendance_repo, "register_for", register_for
    )
    monkeypatch.setattr(
        sessions_module.attendance_repo, "insert_absences", insert_absences
    )
    monkeypatch.setattr(
        sessions_module.attendance_repo, "close_session", close_session
    )
    monkeypatch.setattr(
        sessions_module.attendance_repo,
        "open_sessions_today",
        lambda _cursor: log.open_sessions,
    )

    def stop_camera():
        log.camera_stopped = True

        # ⚠️ **The real one clears the subject** - RecognitionSession.stop()
        # sets `_subject = None` - and this fake did not, which is precisely
        # why FS-16 was invisible here for two sprints. Ending a session in the
        # browser POSTs /stop_camera *before* the form, so the route the tests
        # below drive was never reached in the state production reaches it in.
        # Keep this faithful.
        sessions_module.recognition_session.subject = None

    monkeypatch.setattr(sessions_module, "stop_camera", stop_camera)

    return log


def running(monkeypatch, subject):
    import web.sessions as sessions_module

    monkeypatch.setattr(
        sessions_module, "recognition_session", FakeSession(subject)
    )


def test_a_mismatched_subject_is_refused(client, monkeypatch, recorder):
    """
    The finding itself. CS401 is running; the form submits a different
    offering.
    """
    running(monkeypatch, RUNNING)
    sign_in(client)

    response = client.post(
        "/end-attendance", data={"subject_id": str(OTHER_SUBJECT_ID)}
    )

    assert response.status_code == 409


def test_nothing_is_written_when_the_subjects_disagree(
    client, monkeypatch, recorder
):
    """
    The consequence that matters. A referentially valid register that is
    factually wrong is worse than an error, because nothing ever flags it.
    """
    running(monkeypatch, RUNNING)
    sign_in(client)

    client.post("/end-attendance", data={"subject_id": str(OTHER_SUBJECT_ID)})

    assert recorder.absences == [], (
        "absences were written for a subject that was not the one running"
    )
    assert recorder.registers_read == [], "the wrong register was even read"
    assert recorder.closed_sessions == []


def test_the_session_survives_a_refusal(client, monkeypatch, recorder):
    """
    A mis-selection must be recoverable. Closing the session here would leave
    the operator unable to write the register the session was actually for.
    """
    running(monkeypatch, RUNNING)
    sign_in(client)

    client.post("/end-attendance", data={"subject_id": str(OTHER_SUBJECT_ID)})

    assert recorder.camera_stopped is False
    assert recorder.closed_sessions == []


def test_the_refusal_names_both_subjects(client, monkeypatch, recorder):
    """
    US-1: the page has to say what to do next. "Conflict" alone leaves the
    operator guessing which of the two dropdowns was wrong.
    """
    running(monkeypatch, RUNNING)
    sign_in(client)

    body = client.post(
        "/end-attendance", data={"subject_id": str(OTHER_SUBJECT_ID)}
    ).get_data(as_text=True)

    assert "CS401" in body
    assert "still running" in body


def test_the_matching_subject_ends_the_session(client, monkeypatch, recorder):
    """The ordinary path, unchanged."""
    running(monkeypatch, RUNNING)
    sign_in(client)

    response = client.post(
        "/end-attendance", data={"subject_id": str(RUNNING.subject_id)}
    )

    assert response.status_code == 200
    assert recorder.registers_read == [RUNNING.subject_id]
    assert recorder.absences == [
        ("SEC-TEST-NOBODY", RUNNING.subject_id, RUNNING.session_id)
    ]
    assert recorder.closed_sessions == [RUNNING.session_id]
    assert recorder.camera_stopped is True


def test_with_nothing_running_and_nothing_open_the_form_is_trusted(
    client, monkeypatch, recorder
):
    """
    Ending a session the server has forgotten *and* that left no open row - a
    second End on the same class. Nothing contradicts the form, so it is
    trusted and the register is still written; the rows carry no session_id,
    which is honest rather than a guess.

    ⚠️ The name used to say "the form is the only source", and that was the
    belief FS-16 lived inside. It is not: `attendance_sessions` outlives the
    process. The form is trusted only when nothing is open.
    """
    running(monkeypatch, None)
    recorder.open_sessions = []
    sign_in(client)

    response = client.post(
        "/end-attendance", data={"subject_id": str(OTHER_SUBJECT_ID)}
    )

    assert response.status_code == 200
    assert recorder.registers_read == [OTHER_SUBJECT_ID]
    assert recorder.absences == [("SEC-TEST-NOBODY", OTHER_SUBJECT_ID, None)]


def test_the_guard_survives_the_camera_being_stopped_first(
    client, monkeypatch, recorder
):
    """
    FS-16, reported by the user 2026-08-29 from a live run: the guard above was
    dead in the browser, and every test in this file passed anyway.

    `static/js/attendance.js` used to POST /stop_camera and submit the End form
    in its `.then()`. `RecognitionSession.stop()` sets the subject to None, so
    the route always took the "no session to prefer" fallback and trusted the
    dropdown - HTTP 200, the absent register written for the subject that was
    *not* taught, `session_id` NULL, and the running session's row never
    closed, so the dashboard counted it active for the rest of the day.

    The handler no longer pre-stops; /end-attendance stops the camera itself,
    after this guard. This test drives the two routes in the order the browser
    uses, so re-adding that fetch fails here.
    """
    running(monkeypatch, RUNNING)
    recorder.open_sessions = [
        {"id": RUNNING.session_id, "subject_id": RUNNING.subject_id}
    ]
    sign_in(client)

    client.post("/stop_camera")

    response = client.post(
        "/end-attendance", data={"subject_id": str(OTHER_SUBJECT_ID)}
    )

    assert response.status_code == 409
    assert recorder.absences == []
    assert recorder.registers_read == []
    assert recorder.closed_sessions == []


def test_the_right_subject_still_ends_after_the_camera_was_stopped(
    client, monkeypatch, recorder
):
    """
    The other half of FS-16, and the one that proves the fix is not just a
    refusal. Same browser order, matching subject: the open row is found, so
    the register is stamped with the real session id instead of NULL and *that*
    session is closed.

    Before this, the fallback wrote `session_id = NULL` and never closed
    anything, so the dashboard's Active Sessions counter kept showing a class
    that had finished (see `repositories.attendance.counters`).
    """
    running(monkeypatch, RUNNING)
    recorder.open_sessions = [
        {"id": RUNNING.session_id, "subject_id": RUNNING.subject_id}
    ]
    sign_in(client)

    client.post("/stop_camera")

    response = client.post(
        "/end-attendance", data={"subject_id": str(RUNNING.subject_id)}
    )

    assert response.status_code == 200
    assert recorder.absences == [
        ("SEC-TEST-NOBODY", RUNNING.subject_id, RUNNING.session_id)
    ]
    assert recorder.closed_sessions == [RUNNING.session_id]


def test_a_missing_subject_is_still_a_400(client, monkeypatch, recorder):
    """The submitted value is validated before either branch above."""
    running(monkeypatch, None)
    sign_in(client)

    response = client.post("/end-attendance", data={"subject_id": ""})

    assert response.status_code == 400


# ---------------------------------------------------------------------------
# The client half of FS-16.
#
# The route is now safe on its own - the two tests above prove it refuses even
# when the camera was stopped first. What follows pins the other half: the
# browser should not be creating that situation on every single End in the
# first place. A pre-flight /stop_camera turns an ordinary end-of-class into
# the recovery path, and the recovery path cannot distinguish "the operator
# just stopped it" from "the server restarted".
#
# ⚠️ **Comments are stripped before searching.** This codebase comments
# heavily and the comment explaining FS-16 names the very construction being
# banned, so a plain text search matches the prose about the bug and fails on
# correct code. That is lessons.md L12/L21, and tests/test_templates.py takes
# the same precaution with the stylesheet.
# ---------------------------------------------------------------------------

JS_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
JS_LINE_COMMENT = re.compile("//.*")

JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)


def _attendance_js_without_comments():
    source = (PROJECT_ROOT / "static" / "js" / "attendance.js").read_text(
        encoding="utf-8"
    )

    stripped = JS_LINE_COMMENT.sub("", JS_BLOCK_COMMENT.sub("", source))

    # The stripper has to be doing something, or this whole file's protection
    # is a no-op that nobody notices. attendance.js opens with a block comment
    # and carries line comments throughout.
    assert len(stripped) < len(source), "no comments were stripped"
    assert "US-3" not in stripped, "block comments survived the strip"

    return stripped


def test_the_attendance_page_does_not_stop_the_camera_before_ending():
    """
    FS-16's client half: re-adding the pre-flight stop fails here.

    /end-attendance stops the camera itself, after the guard, so the fetch
    bought nothing and cost the guard its subject on every end.
    """
    code = _attendance_js_without_comments()

    assert "STOP_URL" not in code, (
        "attendance.js references the stop-camera URL again - a pre-flight "
        "/stop_camera before the End form is what made FS-16 fire on every "
        "session"
    )


def test_the_attendance_template_does_not_hand_out_the_stop_url():
    """
    The URL reaches the script as a data- attribute, so removing it is what
    makes the fetch above impossible to restore by accident rather than
    merely absent.
    """
    markup = (PROJECT_ROOT / "templates" / "attendance.html").read_text(
        encoding="utf-8"
    )

    assert "stop_camera_route" not in JINJA_COMMENT.sub("", markup)
