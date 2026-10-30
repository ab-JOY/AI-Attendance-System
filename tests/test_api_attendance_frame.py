"""
POST /api/attendance/frame - one frame of a phone's verification stream.

The decision itself is services/phone_verification.py's and the classroom
pipeline's; these tests are about the route around it: what it refuses before
any recognition, the size it decodes at, and how each outcome is answered.

Every test here has nothing in today's register, unless it says otherwise:
that refusal has its own file (tests/test_api_once_a_day.py).
"""

from __future__ import annotations

import io
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

import recognize_face
import web.api as api
from services.phone_verification import PhoneResult, PhonesBusy
from vision.pose import RECOGNITION_FRAME_HEIGHT, RECOGNITION_FRAME_WIDTH
from web.api import _make_token

STUDENT_ID = "23-1-1-0001"
SUBJECT = SimpleNamespace(subject_id=3, session_id=40, subject_code="CS101")


class FakeSession:
    def __init__(self, *, running=True, recognized=None):
        self.marked = {}
        self._snapshot = SimpleNamespace(
            running=running,
            subject=SUBJECT if running else None,
            recognizer="the model" if running else None,
            recognized=recognized or {},
        )

    def snapshot(self):
        return self._snapshot

    def mark_recognized(self, student_id, status=None):
        self.marked[student_id] = status


class FakePhones:
    def __init__(self, result=None, error=None):
        self.result = result or PhoneResult(False, "Verifying 3/20")
        self.error = error
        self.frames = []
        self.discarded = 0

    def verify(self, student_id, snapshot, frame, mark_recognized, **timing):
        self.frames.append((student_id, frame))
        if self.error is not None:
            raise self.error
        if self.result.done:
            mark_recognized(student_id, "Present")
        return self.result

    def discard(self, _student_id):
        self.discarded += 1


@pytest.fixture(scope="module")
def flask_app():
    import app as app_module

    app_module.app.config["TESTING"] = True
    return app_module.app


@pytest.fixture
def session(monkeypatch):
    fake = FakeSession()
    # The running class this student's phone belongs to (todo.md §19).
    monkeypatch.setattr(api, "_session_for_student", lambda _student_id: fake)
    return fake


@pytest.fixture(autouse=True)
def not_yet_recorded(monkeypatch):
    monkeypatch.setattr(api, "_recorded_status_today", lambda *args: None)


@pytest.fixture
def phones(monkeypatch):
    fake = FakePhones()
    monkeypatch.setattr(api, "_phones", fake)
    return fake


def upload(flask_app, width=1920, height=1080, image=None):
    if image is None:
        image = np.full((height, width, 3), 128, np.uint8)

    ok, buffer = cv2.imencode(".jpg", image)
    assert ok
    token = _make_token({"role": "student", "user_id": STUDENT_ID})

    return flask_app.test_client().post(
        "/api/attendance/frame",
        data=buffer.tobytes(),
        content_type="image/jpeg",
        headers={"Authorization": f"Bearer {token}"},
    )


def test_a_frame_is_decoded_at_the_recognition_resolution(flask_app, session, phones):
    response = upload(flask_app, 1920, 1080)

    assert response.status_code == 200
    (student_id, frame), = phones.frames
    assert student_id == STUDENT_ID
    assert frame.shape[:2] == (RECOGNITION_FRAME_HEIGHT, RECOGNITION_FRAME_WIDTH)


def test_the_phones_square_reaches_recognition_whole(flask_app, session, phones):
    """
    Letterboxed, not centre-cropped: the top of the square the student framed
    is still in the frame recognition judges (lessons.md L33).
    """
    square = np.full((1080, 1080, 3), 128, np.uint8)
    square[:60, :] = (0, 0, 255)  # a red band across the top

    upload(flask_app, image=square)

    (_student_id, frame), = phones.frames
    assert frame.shape[:2] == (RECOGNITION_FRAME_HEIGHT, RECOGNITION_FRAME_WIDTH)
    top_centre = frame[5, RECOGNITION_FRAME_WIDTH // 2]
    assert top_centre[2] > 180 and top_centre[0] < 80


def test_progress_is_relayed_and_not_done(flask_app, session, phones):
    phones.result = PhoneResult(
        False, "Liveness 1/2: Turn LEFT", stage="liveness", step="LEFT", progress=0.6
    )

    body = upload(flask_app).get_json()

    assert body == {
        "success": True,
        "done": False,
        "message": "Liveness 1/2: Turn LEFT",
        "stage": "liveness",
        "step": "LEFT",
        "progress": 0.6,
    }
    assert session.marked == {}


def test_a_verified_student_is_done_and_marked(flask_app, session, phones):
    phones.result = PhoneResult(True, "Verified! Marked as Present.")

    body = upload(flask_app).get_json()

    assert body["done"] is True
    assert session.marked == {STUDENT_ID: "Present"}


def test_a_permanent_refusal_is_a_403(flask_app, session, phones):
    phones.result = PhoneResult(False, "Not in this class", refused=True)

    response = upload(flask_app)

    assert response.status_code == 403
    assert response.get_json()["message"] == "Not in this class"


def test_too_many_phones_is_a_429_to_retry(flask_app, session, phones):
    phones.error = PhonesBusy()

    assert upload(flask_app).status_code == 429


def test_a_pipeline_fault_is_a_500_not_a_verdict(flask_app, session, phones):
    phones.error = RuntimeError("boom")

    response = upload(flask_app)

    assert response.status_code == 500
    assert response.get_json()["success"] is False


def test_no_running_session_is_refused_and_releases_phones(
    flask_app, monkeypatch, phones
):
    # The running class this student's phone belongs to (todo.md §19).
    ended = FakeSession(running=False)
    monkeypatch.setattr(api, "_session_for_student", lambda _student_id: ended)

    response = upload(flask_app)

    assert response.status_code == 409
    assert phones.frames == []
    assert phones.discarded == 1


def test_an_already_recorded_student_is_done_without_recognition(
    flask_app, monkeypatch, phones
):
    # The running class this student's phone belongs to (todo.md §19).
    recorded = FakeSession(recognized={STUDENT_ID: "Present"})
    monkeypatch.setattr(api, "_session_for_student", lambda _student_id: recorded)

    body = upload(flask_app).get_json()

    assert body["done"] is True
    assert phones.frames == []


def test_the_one_photo_70_threshold_is_gone():
    """The old route's own threshold must not come back beside the pipeline's."""
    import inspect

    source = inspect.getsource(api.api_attendance_frame)

    assert "70.0" not in source.split('"""', 2)[2]
    assert ".predict(" not in source


# ---------------------------------------------------------------------------
# Several frames per request - one tunnel round trip, several frames judged
# ---------------------------------------------------------------------------


def upload_many(flask_app, count, shades=None):
    token = _make_token({"role": "student", "user_id": STUDENT_ID})
    data = {"frame": []}

    for index in range(count):
        shade = shades[index] if shades else 128
        ok, buffer = cv2.imencode(".jpg", np.full((720, 720, 3), shade, np.uint8))
        assert ok
        data["frame"].append((io.BytesIO(buffer.tobytes()), f"frame{index}.jpg", "image/jpeg"))

    return flask_app.test_client().post(
        "/api/attendance/frame",
        data=data,
        content_type="multipart/form-data",
        headers={"Authorization": f"Bearer {token}"},
    )


def test_several_frames_are_judged_in_the_order_they_were_sent(flask_app, session, phones):
    upload_many(flask_app, 3, shades=[20, 120, 220])

    centres = [int(frame[360, 640, 0]) for _student, frame in phones.frames]
    assert len(centres) == 3
    assert centres == sorted(centres)  # oldest first, as the app sends them


def test_judging_stops_at_the_frame_that_records(flask_app, session, phones):
    phones.result = PhoneResult(True, "Verified! Marked as Present.")

    body = upload_many(flask_app, 3).get_json()

    assert body["done"] is True
    assert len(phones.frames) == 1
    assert session.marked == {STUDENT_ID: "Present"}


def test_frames_past_the_cap_are_ignored(flask_app, session, phones):
    upload_many(flask_app, api.MAX_FRAMES_PER_REQUEST + 2)

    assert len(phones.frames) == api.MAX_FRAMES_PER_REQUEST


def test_one_unusable_frame_refuses_the_request_before_any_is_judged(
    flask_app, session, phones
):
    token = _make_token({"role": "student", "user_id": STUDENT_ID})
    ok, good = cv2.imencode(".jpg", np.full((720, 720, 3), 128, np.uint8))
    response = flask_app.test_client().post(
        "/api/attendance/frame",
        data={"frame": [
            (io.BytesIO(good.tobytes()), "a.jpg", "image/jpeg"),
            (io.BytesIO(b"not a picture"), "b.jpg", "image/jpeg"),
        ]},
        content_type="multipart/form-data",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 400
    assert phones.frames == []


# ---------------------------------------------------------------------------
# Which class a phone belongs to, with several running (todo.md §19 step 9)
# ---------------------------------------------------------------------------


@pytest.fixture
def classes_running(monkeypatch):
    import contextlib

    from tests.fake_sessions import FakeRegistry

    def running_class(subject_id, session_id):
        return SimpleNamespace(
            is_running=True,
            subject=SimpleNamespace(subject_id=subject_id, session_id=session_id),
        )

    registry = FakeRegistry(running_class(1, 10), running_class(2, 11), running_class(3, 12))
    monkeypatch.setattr(recognize_face, "sessions", registry)

    enrolled = {"subjects": set()}

    @contextlib.contextmanager
    def fake_cursor(*_args, **_kwargs):
        yield object()

    monkeypatch.setattr(api, "db_cursor", fake_cursor)
    monkeypatch.setattr(
        api.enrolments_repo, "subject_ids_for",
        lambda _cursor, _student_id: enrolled["subjects"],
    )
    return enrolled


def test_a_phone_joins_the_running_class_its_student_is_in(classes_running):
    classes_running["subjects"] = {2, 7}

    assert api._session_for_student(STUDENT_ID).subject.subject_id == 2


def test_a_phone_never_joins_a_class_its_student_is_not_in(classes_running):
    """Otherwise a student could be recorded in someone else's class."""
    classes_running["subjects"] = {7, 8}

    assert api._session_for_student(STUDENT_ID) is None


def test_two_of_a_students_classes_running_picks_the_newest(classes_running):
    classes_running["subjects"] = {1, 3}

    assert api._session_for_student(STUDENT_ID).subject.session_id == 12


def test_nothing_running_asks_no_database(monkeypatch):
    from tests.fake_sessions import FakeRegistry

    monkeypatch.setattr(recognize_face, "sessions", FakeRegistry())
    monkeypatch.setattr(api, "db_cursor", lambda *a, **k: pytest.fail("queried"))

    assert api._session_for_student(STUDENT_ID) is None
