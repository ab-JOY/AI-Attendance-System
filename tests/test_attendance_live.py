"""
`/attendance/live` answers while a session is running (US-3, R1).

**This route had never worked.** It read `recognition_session.recognized_ids`
without calling it - the attribute is a method on `RecognitionSession`, so
`sorted()` was handed a bound method and raised
`TypeError: 'method' object is not iterable`. Every poll of a running session
answered 500, `attendance.js` stopped polling and showed "The live list stopped
updating (status 500)", and reloading failed identically.

**Why 1,101 tests and a clean `ruff` said nothing.** The route returns early
unless a session is *running*, and no test in this suite had ever constructed
that state - `grep -rl "attendance_live" tests/` returned nothing at all. The
defect sat behind a guard whose other side nobody had visited. That is the
shape lessons.md L5 describes: the check one layer away from where the failure
lives.

So the point of this file is the *state*, not the assertion. Every test below
stubs a session that is running, because that is the half of the route the rest
of the suite cannot reach.

**No camera, no model, no database.** The session is replaced at the seam
`web/sessions.py` imports it under, and the register lookup is stubbed the same
way. What is under test is the route's behaviour, not MySQL's.
"""

from __future__ import annotations

import contextlib
from datetime import timedelta

import mysql.connector
import pytest

from recognize_face import ActiveSubject

SUBJECT = ActiveSubject(subject_id=1, session_id=7, subject_code="CS401")


@pytest.fixture(scope="module")
def flask_app():
    import app as app_module

    app_module.app.config["TESTING"] = True
    return app_module.app


@pytest.fixture
def client(flask_app):
    return flask_app.test_client()


def sign_in(client):
    """Fabricate a session without touching the database."""
    with client.session_transaction() as session:
        session["user"] = "test-admin"
        session["role"] = "admin"


class FakeSession:
    """
    Enough of `RecognitionSession` for this route.

    ⚠️ `recognized_ids` is a **method** here, exactly as it is on the real
    class. Making it a property in the stub would make this whole file pass
    against the broken route, which is the vacuous-test failure lessons.md L12
    is about.
    """

    def __init__(self, *, running, subject=None, ids=()):
        self.is_running = running
        self.subject = subject
        self._ids = set(ids)

    def recognized_ids(self):
        return set(self._ids)


def stub_session(monkeypatch, session):
    import web.sessions as sessions_module

    monkeypatch.setattr(sessions_module, "recognition_session", session)


def stub_register(monkeypatch, rows=None, error=None):
    """Replace the database read with rows, or with a driver failure."""
    import web.sessions as sessions_module

    @contextlib.contextmanager
    def fake_cursor(*_args, **_kwargs):
        yield object()

    def recognised_today(_cursor, _subject_id, _student_ids):
        if error is not None:
            raise error
        return list(rows or [])

    monkeypatch.setattr(sessions_module, "db_cursor", fake_cursor)
    monkeypatch.setattr(
        sessions_module.attendance_repo, "recognised_today", recognised_today
    )


def test_the_live_list_requires_authentication(client):
    """SE-2's rule holds here too: this names who is in a room."""
    response = client.get("/attendance/live")

    assert response.status_code in (302, 401, 403)


def test_no_session_running_reports_not_running(client, monkeypatch):
    """The idle answer, and the only state the suite could reach before."""
    stub_session(monkeypatch, FakeSession(running=False))

    sign_in(client)
    response = client.get("/attendance/live")

    assert response.status_code == 200
    assert response.get_json() == {"running": False, "students": []}


def test_a_running_session_answers_200_not_500(client, monkeypatch):
    """
    R1, at the layer the operator meets it.

    This is the whole file in one assertion: before the fix this returned 500
    for every poll of every session that had recognised anybody.
    """
    stub_session(
        monkeypatch,
        FakeSession(running=True, subject=SUBJECT, ids={"23-1-1-0559"}),
    )
    stub_register(
        monkeypatch,
        rows=[
            {
                "student_id": "23-1-1-0559",
                "name": "A Student",
                "time_in": timedelta(hours=8, minutes=3),
                "status": "Present",
            }
        ],
    )

    sign_in(client)
    response = client.get("/attendance/live")

    assert response.status_code == 200, (
        "the live list is 500ing again - `recognized_ids` is a method and the "
        "route must call it"
    )

    payload = response.get_json()

    assert payload["running"] is True
    assert payload["subject_code"] == "CS401"
    assert payload["recognised"] == 1
    assert payload["students"] == [
        {
            "student_id": "23-1-1-0559",
            "name": "A Student",
            "time_in": "8:03:00",
            "status": "Present",
        }
    ]


def test_a_running_session_with_nobody_recognised_yet(client, monkeypatch):
    """
    The state FS-14 made indistinguishable from a broken one.

    "Running, nobody yet" and "running, silently recording nothing" must be
    different answers on the screen, and this is the first of the two.
    """
    stub_session(monkeypatch, FakeSession(running=True, subject=SUBJECT))
    stub_register(monkeypatch, rows=[])

    sign_in(client)
    response = client.get("/attendance/live")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["running"] is True
    assert payload["recognised"] == 0
    assert payload["students"] == []


def test_an_absent_row_has_no_arrival_time(client, monkeypatch):
    """
    R4: `time_in` is NULL on an absence, and the poller must not stringify it.

    The route guards on it already; this pins the guard so the NULL change in
    `insert_absences()` cannot start rendering "None" here.
    """
    stub_session(
        monkeypatch,
        FakeSession(running=True, subject=SUBJECT, ids={"SEC-TEST-NOBODY"}),
    )
    stub_register(
        monkeypatch,
        rows=[
            {
                "student_id": "SEC-TEST-NOBODY",
                "name": "Nobody",
                "time_in": None,
                "status": "Absent",
            }
        ],
    )

    sign_in(client)
    payload = client.get("/attendance/live").get_json()

    assert payload["students"][0]["time_in"] is None


def test_a_failed_lookup_does_not_stop_the_poll(client, monkeypatch):
    """
    A database hiccup must not take the live list down.

    The count comes from the recognition session rather than from the register,
    so it is still true when the join fails - and the page keeps showing that
    the session is alive, which is the question US-3 exists to answer.
    """
    stub_session(
        monkeypatch,
        FakeSession(running=True, subject=SUBJECT, ids={"a", "b"}),
    )
    stub_register(monkeypatch, error=mysql.connector.Error("connection lost"))

    sign_in(client)
    response = client.get("/attendance/live")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["running"] is True
    assert payload["recognised"] == 2
    assert payload["students"] == []


def test_a_fault_answers_json_rather_than_an_html_page(client, monkeypatch):
    """
    R7. This route is polled by `fetch()`, so a fault must not hand it markup
    to `JSON.parse` - the operator would see a syntax error instead of a
    message. R1 presented exactly that way.
    """
    import web.sessions as sessions_module

    class Exploding:
        is_running = True
        subject = SUBJECT

        def recognized_ids(self):
            raise RuntimeError("something unexpected")

    monkeypatch.setattr(sessions_module, "recognition_session", Exploding())

    sign_in(client)
    response = client.get("/attendance/live")

    assert response.status_code == 500
    assert response.is_json, (
        "a @json_api route answered a fault with HTML; see web/errors.py"
    )
    assert "something unexpected" not in response.get_data(as_text=True)
