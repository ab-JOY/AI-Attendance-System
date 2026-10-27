"""
The classroom code - proof that a phone verifying attendance is in the room.

services/classroom_code.py makes a code from the running session and the
current PERIOD_SECONDS step; the instructor's attendance page shows it; the app
sends it to `/api/attendance/checkin` for a pass; `/api/attendance/frame`
refuses without a pass **before any face is judged**.

What these pin: a code is only good for its own session and for two steps;
wrong codes run out; the frame route judges no face without a pass; only the
instructor whose class is running (or an admin) is shown the code. No
database is touched.
"""

from __future__ import annotations

from types import SimpleNamespace

import cv2
import numpy as np
import pytest

import recognize_face
import web.api as api
import web.sessions as sessions_module
from services import classroom_code
from services.classroom_code import (
    MAX_WRONG_CODES,
    PASS_SECONDS,
    PERIOD_SECONDS,
    CheckIn,
    CheckIns,
    accepts,
    code_at,
    current_code,
    parse_code,
)
from web.api import _make_token

STUDENT_ID = "23-1-1-0001"
SUBJECT = SimpleNamespace(subject_id=3, session_id=40, subject_code="CS101")
OTHER_SESSION = SimpleNamespace(subject_id=3, session_id=41, subject_code="CS101")
SECRET = b"k" * 32
KEY = classroom_code.session_key(SUBJECT)


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


# ---------------------------------------------------------------------------
# The code
# ---------------------------------------------------------------------------


def test_a_code_is_six_digits_and_the_same_within_its_step():
    start = 1_000_000.0 - (1_000_000.0 % PERIOD_SECONDS)
    code, left = current_code(SECRET, KEY, start)

    assert len(code) == 6 and code.isdigit()
    assert left == PERIOD_SECONDS
    assert current_code(SECRET, KEY, start + PERIOD_SECONDS - 1)[0] == code
    assert current_code(SECRET, KEY, start + PERIOD_SECONDS)[0] != code


def test_the_current_and_previous_code_are_accepted_and_no_older():
    now = 1_000_000.0
    step = int(now // PERIOD_SECONDS)

    assert accepts(SECRET, KEY, code_at(SECRET, KEY, step), now)
    assert accepts(SECRET, KEY, code_at(SECRET, KEY, step - 1), now)
    assert not accepts(SECRET, KEY, code_at(SECRET, KEY, step - 2), now)
    # Nor a code from the future: the page cannot show it yet.
    assert not accepts(SECRET, KEY, code_at(SECRET, KEY, step + 1), now)


def test_another_sessions_code_is_not_this_ones():
    """Same subject, next session: yesterday's photo of the code is useless."""
    now = 1_000_000.0
    other, _ = current_code(SECRET, classroom_code.session_key(OTHER_SESSION), now)

    assert not accepts(SECRET, KEY, other, now)


def test_another_servers_secret_makes_other_codes():
    now = 1_000_000.0

    assert current_code(SECRET, KEY, now)[0] != current_code(b"x" * 32, KEY, now)[0]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("FACEATTEND:012345", None),
        ("012 345", "012345"),
        ("  012345 ", "012345"),
        ("12345", None),
        ("0123456", None),
        ("abcdef", None),
        ("https://example.com/012345", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_code(text, expected):
    assert parse_code(text) == expected


def right_code(clock):
    return current_code(SECRET, KEY, clock())[0]


def test_a_right_code_gives_a_pass_for_this_session_only():
    clock = Clock()
    check_ins = CheckIns(clock)

    outcome, _ = check_ins.check_in(STUDENT_ID, KEY, right_code(clock), SECRET)

    assert outcome is CheckIn.ACCEPTED
    assert check_ins.seconds_left(STUDENT_ID, KEY) == PASS_SECONDS
    assert check_ins.seconds_left("someone-else", KEY) == 0
    assert check_ins.seconds_left(STUDENT_ID, classroom_code.session_key(OTHER_SESSION)) == 0


def test_a_pass_runs_out():
    clock = Clock()
    check_ins = CheckIns(clock)
    check_ins.check_in(STUDENT_ID, KEY, right_code(clock), SECRET)

    clock.now += PASS_SECONDS + 1

    assert check_ins.seconds_left(STUDENT_ID, KEY) == 0


def test_wrong_codes_run_out_and_then_even_the_right_one_is_refused():
    clock = Clock()
    check_ins = CheckIns(clock)
    wrong = "000000" if right_code(clock) != "000000" else "111111"

    outcomes = [
        check_ins.check_in(STUDENT_ID, KEY, wrong, SECRET)
        for _ in range(MAX_WRONG_CODES)
    ]

    assert outcomes[0] == (CheckIn.WRONG, MAX_WRONG_CODES - 1)
    assert outcomes[-1] == (CheckIn.LOCKED, 0)
    assert check_ins.check_in(STUDENT_ID, KEY, right_code(clock), SECRET)[0] is CheckIn.LOCKED
    assert check_ins.seconds_left(STUDENT_ID, KEY) == 0


def test_an_unreadable_code_counts_as_a_wrong_try():
    check_ins = CheckIns(Clock())

    assert check_ins.check_in(STUDENT_ID, KEY, None, SECRET)[0] is CheckIn.WRONG


def test_a_new_session_starts_everyone_again():
    clock = Clock()
    check_ins = CheckIns(clock)
    check_ins.check_in(STUDENT_ID, KEY, right_code(clock), SECRET)
    for _ in range(MAX_WRONG_CODES):
        check_ins.check_in("locked-out", KEY, None, SECRET)

    other_key = classroom_code.session_key(OTHER_SESSION)
    other_code = current_code(SECRET, other_key, clock())[0]

    assert check_ins.check_in("locked-out", other_key, other_code, SECRET)[0] is CheckIn.ACCEPTED
    assert check_ins.seconds_left(STUDENT_ID, KEY) == 0


# ---------------------------------------------------------------------------
# The routes
# ---------------------------------------------------------------------------


class FakeSession:
    def __init__(self, running=True, subject=SUBJECT):
        self.subject = subject if running else None
        self.is_running = running
        self._snapshot = SimpleNamespace(
            running=running,
            subject=self.subject,
            recognizer="the model" if running else None,
            recognized={},
        )

    def snapshot(self):
        return self._snapshot

    def recognized_ids(self):
        return set()

    def mark_recognized(self, student_id, status=None):
        pass


class Phones(list):
    def verify(self, student_id, snapshot, frame, mark_recognized, **timing):
        self.append(student_id)
        from services.phone_verification import PhoneResult

        return PhoneResult(False, "Verifying 1/12")

    def discard_all(self):
        pass


@pytest.fixture(scope="module")
def flask_app():
    import app as app_module

    app_module.app.config["TESTING"] = True
    return app_module.app


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def running(monkeypatch, clock):
    fake = FakeSession()
    monkeypatch.setattr(recognize_face, "session", fake)
    monkeypatch.setattr(sessions_module, "recognition_session", fake)
    monkeypatch.setattr(api, "_check_ins", CheckIns(clock))
    monkeypatch.setattr(api, "_recorded_status_today", lambda *args: None)
    # The page's route reads the wall clock; pin it to the same instant as
    # `clock`. The module's own name for `time`, not the global function.
    monkeypatch.setattr(sessions_module, "time", SimpleNamespace(time=clock))
    return fake


@pytest.fixture
def phones(monkeypatch):
    fake = Phones()
    monkeypatch.setattr(api, "_phones", fake)
    return fake


def headers():
    token = _make_token({"role": "student", "user_id": STUDENT_ID})
    return {"Authorization": f"Bearer {token}"}


def the_rooms_code(clock):
    secret = classroom_code.secret_from(api.app_config.secret_key)
    return current_code(secret, KEY, clock())[0]


def check_in(flask_app, code):
    return flask_app.test_client().post(
        "/api/attendance/checkin", json={"code": code}, headers=headers()
    )


def upload(flask_app):
    ok, buffer = cv2.imencode(".jpg", np.full((1080, 1920, 3), 128, np.uint8))
    assert ok
    return flask_app.test_client().post(
        "/api/attendance/frame",
        data=buffer.tobytes(),
        content_type="image/jpeg",
        headers=headers(),
    )


def test_no_pass_means_no_face_is_judged(flask_app, running, phones):
    response = upload(flask_app)

    assert response.status_code == 403
    assert response.get_json()["need_code"] is True
    assert phones == []


def test_the_rooms_code_lets_the_phone_verify(flask_app, running, phones, clock):
    response = check_in(flask_app, the_rooms_code(clock))

    assert response.status_code == 200
    assert response.get_json()["pass_seconds"] == PASS_SECONDS

    assert upload(flask_app).status_code == 200
    assert phones == [STUDENT_ID]


def test_a_code_from_home_two_steps_old_is_refused(flask_app, running, phones, clock):
    """What a photo of the screen is worth once it has travelled ~40 s."""
    stale = the_rooms_code(clock)
    clock.now += 2 * PERIOD_SECONDS

    response = check_in(flask_app, stale)

    assert response.status_code == 400
    assert response.get_json()["tries_left"] == MAX_WRONG_CODES - 1
    assert upload(flask_app).status_code == 403
    assert phones == []


def test_too_many_wrong_codes_is_a_429(flask_app, running, clock):
    wrong = "000000" if the_rooms_code(clock) != "000000" else "111111"

    for _ in range(MAX_WRONG_CODES - 1):
        assert check_in(flask_app, wrong).status_code == 400

    response = check_in(flask_app, wrong)

    assert response.status_code == 429
    assert response.get_json()["locked"] is True


def test_check_in_needs_a_running_session(flask_app, monkeypatch):
    monkeypatch.setattr(recognize_face, "session", FakeSession(running=False))

    assert check_in(flask_app, "123456").status_code == 409


def test_check_in_needs_a_student_token(flask_app, running):
    response = flask_app.test_client().post(
        "/api/attendance/checkin", json={"code": "123456"}
    )

    assert response.status_code == 401


def test_live_tells_the_app_how_long_the_pass_has_left(
    flask_app, running, clock, monkeypatch
):
    monkeypatch.setattr(api.attendance_repo, "recognised_today", lambda *args: [])
    monkeypatch.setattr(api, "db_cursor", _null_cursor)
    client = flask_app.test_client()

    before = client.get("/api/attendance/live", headers=headers()).get_json()
    check_in(flask_app, the_rooms_code(clock))
    after = client.get("/api/attendance/live", headers=headers()).get_json()

    assert before["pass_seconds"] == 0
    assert after["pass_seconds"] == PASS_SECONDS


class _null_cursor:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return object()

    def __exit__(self, *exc):
        return False


# -- the instructor's page --------------------------------------------------


def sign_in(client, role):
    with client.session_transaction() as session:
        session["user"] = f"test-{role}"
        session["role"] = role
        if role == "admin":
            session["admin_id"] = 1
        else:
            session["instructor_id"] = "SEC-TEST-NOONE"
            session["instructor_pk"] = 999_999


def test_the_page_shows_the_code_the_phone_must_send(flask_app, running, clock, monkeypatch):
    monkeypatch.setattr(sessions_module, "_may_use_subject", lambda subject_id: True)
    client = flask_app.test_client()
    sign_in(client, "admin")

    body = client.get("/attendance/class-code").get_json()

    assert body["running"] is True and body["allowed"] is True
    assert body["code"] == the_rooms_code(clock)
    assert "qr" not in body
    assert 0 < body["seconds_left"] <= PERIOD_SECONDS
    assert check_in(flask_app, body["code"]).status_code == 200


def test_another_instructor_is_not_shown_the_code(flask_app, running, monkeypatch):
    """Holding the code is checking students in from anywhere."""
    asked = []

    def may_use(subject_id):
        asked.append(subject_id)
        return False

    monkeypatch.setattr(sessions_module, "_may_use_subject", may_use)
    client = flask_app.test_client()
    sign_in(client, "instructor")

    response = client.get("/attendance/class-code")

    assert response.status_code == 403
    assert "code" not in response.get_json()
    assert asked == [SUBJECT.subject_id]


def test_no_session_shows_no_code(flask_app, monkeypatch):
    fake = FakeSession(running=False)
    monkeypatch.setattr(sessions_module, "recognition_session", fake)
    client = flask_app.test_client()
    sign_in(client, "admin")

    assert client.get("/attendance/class-code").get_json() == {"running": False}


def test_the_code_page_is_closed_to_anonymous_users(flask_app, running):
    response = flask_app.test_client().get("/attendance/class-code")

    assert response.status_code == 401
