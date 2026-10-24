"""
Phone attendance is once per class per day.

A student already Present or Late for the running subject today is refused by
`/api/attendance/frame` **before any face is judged**: no detector call, no
predict, nothing for a repeated attempt to probe. The register is the
authority, not the session's in-memory list, so a restarted server cannot
reopen it - and when the register cannot be read the route fails closed.

An Absent row is not a mark: a student marked absent by an earlier session
today can still be verified, as `save_attendance()` allows.

None of these touch the database: the register lookup is replaced. The
student holds a classroom-code pass throughout (that refusal is
tests/test_api_checkin.py), and the face pipeline is a fake
(services/phone_verification.py has its own tests).
"""

from __future__ import annotations

from types import SimpleNamespace

import cv2
import mysql.connector
import numpy as np
import pytest

import recognize_face
import web.api as api
from services.phone_verification import PhoneResult
from web.api import _make_token

STUDENT_ID = "23-1-1-0001"
SUBJECT = SimpleNamespace(subject_id=3, session_id=40, subject_code="CS101")


class Detector:
    def __init__(self):
        self.calls = 0

    def process(self, rgb):
        self.calls += 1
        return SimpleNamespace(multi_face_landmarks=[object()])


class FakeSession:
    def __init__(self, recognized=None):
        self.marked = {}
        self.detector = Detector()
        self.subject = SUBJECT
        self.is_running = True
        self._snapshot = SimpleNamespace(
            running=True,
            subject=SUBJECT,
            detector=self.detector,
            recognizer=SimpleNamespace(predict=lambda face: (0, 40.0)),
            label_map={0: {"student_id": STUDENT_ID, "name": "Test"}},
            recognized=recognized if recognized is not None else {},
        )

    def snapshot(self):
        return self._snapshot

    def recognized_ids(self):
        return set(self._snapshot.recognized)

    def mark_recognized(self, student_id, status=None):
        # As RecognitionSession does: into the same mapping the next
        # snapshot() returns.
        self.marked[student_id] = status
        self._snapshot.recognized[student_id] = status


@pytest.fixture(scope="module")
def flask_app():
    import app as app_module

    app_module.app.config["TESTING"] = True
    return app_module.app


@pytest.fixture
def session(monkeypatch):
    fake = FakeSession()
    monkeypatch.setattr(recognize_face, "session", fake)
    return fake


class Passes:
    def seconds_left(self, student_id, key):
        return 180


@pytest.fixture(autouse=True)
def checked_in(monkeypatch):
    monkeypatch.setattr(api, "_check_ins", Passes())


@pytest.fixture
def register(monkeypatch):
    """The register's answer for this student, set per test."""
    state = SimpleNamespace(status=None, error=False, asked=0)

    def recorded_status_today(student_id, subject_id):
        state.asked += 1
        if state.error:
            raise mysql.connector.Error("down")
        return state.status

    monkeypatch.setattr(api, "_recorded_status_today", recorded_status_today)
    return state


class Phones(list):
    """
    Stands in for the phone pipeline. Each verify() is one frame judged;
    `result` is its outcome, and a done result marks the session as the real
    one does (only after a row is written).
    """

    result = PhoneResult(True, "Verified! Marked as Present.")

    def verify(self, student_id, snapshot, frame, mark_recognized, **timing):
        self.append(student_id)
        if self.result.done:
            mark_recognized(student_id, "Present")
        return self.result

    def discard_all(self):
        pass


@pytest.fixture
def saves(monkeypatch):
    """The frames that reached face judgement (named for what it guards)."""
    phones = Phones()
    monkeypatch.setattr(api, "_phones", phones)
    return phones


def headers():
    token = _make_token({"role": "student", "user_id": STUDENT_ID})
    return {"Authorization": f"Bearer {token}"}


def upload(flask_app):
    ok, buffer = cv2.imencode(".jpg", np.full((1080, 1920, 3), 128, np.uint8))
    assert ok
    return flask_app.test_client().post(
        "/api/attendance/frame",
        data=buffer.tobytes(),
        content_type="image/jpeg",
        headers=headers(),
    )


@pytest.mark.parametrize("status", ["Present", "Late"])
def test_a_student_recorded_today_is_refused_before_any_face_is_judged(
    flask_app, session, register, saves, status
):
    register.status = status

    body = upload(flask_app).get_json()

    assert body["done"] is True
    assert body["already"] is True
    assert status in body["message"]
    assert session.detector.calls == 0
    assert saves == []


def test_the_register_decides_even_when_the_session_forgot(
    flask_app, session, register, saves
):
    """A restarted server has an empty in-memory list; the register does not."""
    assert session.snapshot().recognized == {}
    register.status = "Present"

    upload(flask_app)

    assert register.asked == 1
    assert session.detector.calls == 0


def test_the_sessions_own_list_is_enough_without_asking_the_register(
    flask_app, monkeypatch, register, saves
):
    fake = FakeSession(recognized={STUDENT_ID: "Present"})
    monkeypatch.setattr(recognize_face, "session", fake)

    body = upload(flask_app).get_json()

    assert body["already"] is True
    assert register.asked == 0
    assert fake.detector.calls == 0


def test_an_unreadable_register_fails_closed(flask_app, session, register, saves):
    register.error = True

    response = upload(flask_app)

    assert response.status_code == 503
    assert session.detector.calls == 0
    assert saves == []


def test_a_student_not_yet_recorded_is_verified_once(flask_app, session, register, saves):
    register.status = None  # nothing today - or only an Absent, which is not a mark

    body = upload(flask_app).get_json()

    assert body["done"] is True
    assert "already" not in body
    assert saves == [STUDENT_ID]
    assert session.marked == {STUDENT_ID: "Present"}


def test_after_being_recorded_the_next_attempt_is_refused(
    flask_app, session, register, saves
):
    upload(flask_app)          # recorded - and now in the session's list
    second = upload(flask_app).get_json()

    assert second["already"] is True
    assert saves == [STUDENT_ID]  # one write, not two


def test_a_frame_that_records_nothing_does_not_lock_the_student_out(
    flask_app, session, register, saves
):
    """
    A failed save (NotRecorded.ERROR) wrote nothing, and the pipeline retries
    it on a later frame rather than ending. Marking the student would make the
    once-a-day rule refuse them for the rest of the day with no attendance at
    all - so nothing is marked until a frame comes back done.
    """
    saves.result = PhoneResult(False, "Not recorded - system error")

    body = upload(flask_app).get_json()

    assert body["done"] is False
    assert session.marked == {}

    saves.result = PhoneResult(True, "Verified! Marked as Present.")
    retry = upload(flask_app).get_json()
    assert retry["done"] is True and "already" not in retry


def test_live_tells_the_app_the_student_is_already_recorded(
    flask_app, session, register, monkeypatch
):
    register.status = "Late"
    monkeypatch.setattr(api.attendance_repo, "recognised_today", lambda *args: [])
    monkeypatch.setattr(api, "db_cursor", _null_cursor)

    body = flask_app.test_client().get("/api/attendance/live", headers=headers()).get_json()

    assert body["your_status"] == "Late"


class _null_cursor:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return object()

    def __exit__(self, *exc):
        return False


# ---------------------------------------------------------------------------
# The register query
# ---------------------------------------------------------------------------


class ScriptedCursor:
    """Records the SQL and answers `fetchone()` from a fixed row."""

    def __init__(self, row):
        self.row = row
        self.queries = []

    def execute(self, sql, params=None):
        self.queries.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.row


def test_the_query_is_keyed_like_the_unique_index_and_ignores_absent():
    """
    Same definition as `recorded_today()` (tests/test_session_seeding.py), one
    student at a time: student, subject and today - the UNIQUE index - and
    only the statuses that mean "recorded". An Absent from an earlier session
    must not block a real verification.
    """
    from repositories import attendance as attendance_repo

    cursor = ScriptedCursor({"status": "Late"})

    assert attendance_repo.recorded_status_today(cursor, STUDENT_ID, 7) == "Late"

    sql, params = cursor.queries[0]
    assert "FROM attendance" in sql
    assert "student_id = %s" in sql
    assert "subject_id = %s" in sql
    assert "attendance_date = CURDATE()" in sql
    assert "status IN ('Present', 'Late')" in sql
    assert params == (STUDENT_ID, 7)


def test_no_row_is_not_recorded():
    from repositories import attendance as attendance_repo

    assert attendance_repo.recorded_status_today(ScriptedCursor(None), STUDENT_ID, 7) is None
