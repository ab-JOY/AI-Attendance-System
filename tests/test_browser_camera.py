"""
Attendance from the operator's browser webcam.

A session can take its frames from uploads instead of the classroom camera, so
an instructor on any laptop - over the Cloudflare deployment - can use that
laptop's webcam. `BrowserCamera` stands in for `cv2.VideoCapture`, so the
reader, the recognition thread and everything after them are unchanged.

What matters, and what is tested here:

- an uploaded frame reaches recognition **through the real CameraReader**,
  once, at the recognition resolution (1280x720, not enrolment's 1920x1080 -
  the face-size gates are pixels tuned at 1280x720);
- an upload can never be mixed into a classroom-camera session;
- only the login that started a browser session can feed it.
"""

from __future__ import annotations

import contextlib
import threading
import time

import cv2
import numpy as np
import pytest

from infra.camera import BrowserCamera, CameraReader
from recognize_face import ActiveSubject
from vision.pose import RECOGNITION_FRAME_HEIGHT, RECOGNITION_FRAME_WIDTH
from vision.session import RecognitionSession, SessionHooks

FRAME = np.zeros((RECOGNITION_FRAME_HEIGHT, RECOGNITION_FRAME_WIDTH, 3), np.uint8)
SUBJECT = ActiveSubject(subject_id=1, session_id=7, subject_code="CS401")


# ---------------------------------------------------------------------------
# BrowserCamera
# ---------------------------------------------------------------------------


def test_a_pushed_frame_is_read_once():
    camera = BrowserCamera(wait_seconds=0.05)
    camera.push(FRAME)

    ok, frame = camera.read()
    assert ok and frame is FRAME

    # The same frame twice would be recognised twice - identity votes from one
    # picture counted as two.
    assert camera.read() == (False, None)


def test_read_waits_for_the_next_upload():
    camera = BrowserCamera(wait_seconds=2.0)
    threading.Timer(0.1, camera.push, args=(FRAME,)).start()

    started = time.monotonic()
    ok, _frame = camera.read()

    assert ok
    assert time.monotonic() - started < 1.0


def test_release_wakes_a_waiting_read_and_refuses_further_uploads():
    camera = BrowserCamera(wait_seconds=5.0)
    threading.Timer(0.1, camera.release).start()

    started = time.monotonic()
    assert camera.read() == (False, None)
    assert time.monotonic() - started < 1.0

    assert not camera.isOpened()
    assert camera.push(FRAME) is False


# ---------------------------------------------------------------------------
# The session, with the real CameraReader
# ---------------------------------------------------------------------------


class Hooks:
    def __init__(self):
        self.open_camera_calls = 0
        self.configure_calls = 0

    def open_camera(self):
        self.open_camera_calls += 1
        return FakeDevice()

    def configure_camera(self, _capture):
        self.configure_calls += 1

    def build(self):
        return SessionHooks(
            open_camera=self.open_camera,
            load_model=lambda: (object(), {0: {"student_id": "S1", "name": "One"}}),
            model_signature=lambda: ("sig", 1),
            make_detector=object,
            make_reader=CameraReader,
            configure_camera=self.configure_camera,
        )


class FakeDevice:
    """A classroom camera that opens and never produces a frame."""

    def __init__(self):
        self.released = False

    def isOpened(self):
        return not self.released

    def read(self):
        time.sleep(0.01)
        return False, None

    def release(self):
        self.released = True


@pytest.fixture
def hooks():
    return Hooks()


@pytest.fixture
def session(hooks):
    recognition = RecognitionSession(hooks.build(), register_exit_hook=lambda _fn: None)
    yield recognition
    recognition.stop()


def test_an_uploaded_frame_reaches_recognition(session):
    camera = BrowserCamera()
    assert session.start(SUBJECT, camera=camera)

    assert session.submit_frame(FRAME)

    ok, frame = session.snapshot().reader.read(timeout=2.0)
    assert ok
    assert frame.shape[:2] == (RECOGNITION_FRAME_HEIGHT, RECOGNITION_FRAME_WIDTH)


def test_a_browser_session_does_not_open_or_configure_the_classroom_camera(
    session, hooks
):
    # configure_camera reads three frames to discard them; on a browser that
    # has sent nothing yet that would only block the start.
    assert session.start(SUBJECT, camera=BrowserCamera())

    assert hooks.open_camera_calls == 0
    assert hooks.configure_calls == 0
    assert session.uses_browser_camera


def test_uploads_are_refused_by_a_classroom_camera_session(session):
    assert session.start(SUBJECT)

    assert not session.uses_browser_camera
    assert session.submit_frame(FRAME) is False


def test_uploads_are_refused_when_no_session_is_running(session):
    assert session.submit_frame(FRAME) is False


def test_ending_the_session_releases_the_browser_camera(session):
    camera = BrowserCamera()
    session.start(SUBJECT, camera=camera)

    session.stop()

    assert not camera.isOpened()
    assert session.submit_frame(FRAME) is False


# ---------------------------------------------------------------------------
# The routes
# ---------------------------------------------------------------------------


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


def sign_in(client, role="admin", browser_session_id=None):
    from web.sessions import BROWSER_CAMERA_KEY

    with client.session_transaction() as flask_session:
        flask_session["user"] = "test-" + role
        flask_session["role"] = role

        if browser_session_id is not None:
            flask_session[BROWSER_CAMERA_KEY] = browser_session_id


class FakeRecognition:
    """The recognition session as the upload route sees it."""

    def __init__(self, *, subject=SUBJECT, browser=True):
        self.subject = subject
        self.uses_browser_camera = browser
        self.frames = []

    def submit_frame(self, frame):
        self.frames.append(frame)
        return True


@pytest.fixture
def recognition(monkeypatch):
    import web.sessions as sessions_module

    fake = FakeRecognition()
    monkeypatch.setattr(sessions_module, "recognition_session", fake)
    return fake


def jpeg(width, height):
    ok, buffer = cv2.imencode(".jpg", np.full((height, width, 3), 128, np.uint8))
    assert ok
    return buffer.tobytes()


def upload(client, body, content_type="image/jpeg"):
    return client.post("/attendance/frame", data=body, content_type=content_type)


def test_the_starting_browser_feeds_its_session(client, recognition):
    sign_in(client, browser_session_id=SUBJECT.session_id)

    response = upload(client, jpeg(1920, 1080))

    assert response.status_code == 200
    # Normalised to the recognition resolution, not enrolment's canonical one.
    assert recognition.frames[0].shape[:2] == (
        RECOGNITION_FRAME_HEIGHT,
        RECOGNITION_FRAME_WIDTH,
    )


def test_another_login_cannot_feed_the_session(client, recognition):
    sign_in(client)  # signed in, but did not start this session

    assert upload(client, jpeg(1280, 720)).status_code == 409
    assert recognition.frames == []


def test_a_stale_key_from_an_earlier_session_cannot_feed_this_one(client, recognition):
    sign_in(client, browser_session_id=SUBJECT.session_id - 1)

    assert upload(client, jpeg(1280, 720)).status_code == 409


def test_a_classroom_camera_session_refuses_uploads(client, monkeypatch):
    import web.sessions as sessions_module

    fake = FakeRecognition(browser=False)
    monkeypatch.setattr(sessions_module, "recognition_session", fake)
    sign_in(client, browser_session_id=SUBJECT.session_id)

    assert upload(client, jpeg(1280, 720)).status_code == 409
    assert fake.frames == []


def test_no_running_session_refuses_uploads(client, monkeypatch):
    import web.sessions as sessions_module

    monkeypatch.setattr(
        sessions_module, "recognition_session", FakeRecognition(subject=None)
    )
    sign_in(client, browser_session_id=SUBJECT.session_id)

    assert upload(client, jpeg(1280, 720)).status_code == 409


@pytest.mark.parametrize(
    ("body", "content_type"),
    [
        (b"not a jpeg", "image/jpeg"),
        (jpeg(1280, 720), "image/png"),
        (jpeg(640, 480), "image/jpeg"),  # below the minimum width
    ],
    # Without ids pytest names each case after the JPEG's bytes, which
    # overflows the PYTEST_CURRENT_TEST environment variable on Windows.
    ids=["not-a-jpeg", "wrong-content-type", "too-narrow"],
)
def test_an_unusable_upload_is_refused_with_a_reason(
    client, recognition, body, content_type
):
    sign_in(client, browser_session_id=SUBJECT.session_id)

    response = upload(client, body, content_type)

    assert response.status_code == 400
    assert response.get_json()["message"]
    assert recognition.frames == []


def test_uploading_requires_signing_in(client, recognition):
    response = upload(client, jpeg(1280, 720))

    assert response.status_code in (302, 401)
    assert recognition.frames == []


# --- starting a browser session ---------------------------------------------


@pytest.fixture
def startable(monkeypatch):
    """/start-attendance with everything but the camera choice stubbed."""
    import web.sessions as sessions_module

    calls = {"cameras": [], "closed": []}

    @contextlib.contextmanager
    def fake_cursor(*_args, **_kwargs):
        yield object()

    def start_camera(_active, camera=None):
        calls["cameras"].append(camera)
        return True

    monkeypatch.setattr(sessions_module, "db_cursor", fake_cursor)
    monkeypatch.setattr(sessions_module.subjects_repo, "meeting_day", lambda *_a: None)
    monkeypatch.setattr(
        sessions_module.enrolments_repo, "count_for_subject", lambda *_a: 3
    )
    monkeypatch.setattr(
        sessions_module.attendance_service,
        "open_session",
        lambda subject_id, _user: ({"subject_code": "CS401", "id": subject_id}, 7),
    )
    monkeypatch.setattr(
        sessions_module.attendance_service,
        "close_session",
        lambda row_id: calls["closed"].append(row_id),
    )
    def start_upload_recognition():
        calls["recognition_started"] = calls.get("recognition_started", 0) + 1
        return True

    monkeypatch.setattr(sessions_module, "start_camera", start_camera)
    monkeypatch.setattr(
        sessions_module, "start_upload_recognition", start_upload_recognition
    )

    return calls


def start(client, camera):
    return client.post(
        "/start-attendance", data={"subject_id": "1", "camera": camera}
    ).get_json()


def test_starting_with_the_browser_camera_supplies_one_and_remembers_the_login(
    client, monkeypatch, startable
):
    import web.sessions as sessions_module

    monkeypatch.setattr(
        sessions_module, "recognition_session", FakeRecognition(browser=True)
    )
    sign_in(client)

    body = start(client, "browser")

    assert body["success"] and body["camera"] == "browser"
    assert isinstance(startable["cameras"][0], BrowserCamera)
    # The page opens no stream in this mode, so the server runs recognition.
    assert startable["recognition_started"] == 1

    with client.session_transaction() as flask_session:
        assert flask_session[sessions_module.BROWSER_CAMERA_KEY] == 7


def test_starting_with_the_classroom_camera_supplies_none(client, monkeypatch, startable):
    import web.sessions as sessions_module

    monkeypatch.setattr(
        sessions_module, "recognition_session", FakeRecognition(browser=False)
    )
    sign_in(client)

    assert start(client, "server")["success"]
    assert startable["cameras"] == [None]
    # The classroom camera's recognition belongs to its /video_feed viewer.
    assert "recognition_started" not in startable


def test_asking_for_the_browser_camera_on_a_classroom_session_is_refused(
    client, monkeypatch, startable
):
    # start() answers True when the subject is already running - on whichever
    # camera it started with. That must not read as success to a browser that
    # would then upload into a session refusing its frames.
    import web.sessions as sessions_module

    monkeypatch.setattr(
        sessions_module, "recognition_session", FakeRecognition(browser=False)
    )
    sign_in(client)

    body = start(client, "browser")

    assert not body["success"]
    assert "classroom camera" in body["message"]
    assert startable["closed"] == [7]


def test_an_unknown_camera_choice_is_refused(client, startable):
    sign_in(client)

    assert not start(client, "the-projector")["success"]
    assert startable["cameras"] == []


# --- results returned to the page -------------------------------------------


def test_each_upload_returns_the_latest_results_to_draw(client, recognition, monkeypatch):
    import web.sessions as sessions_module

    faces = [{"box": [1, 2, 3, 4], "name": "One", "status": "Present",
              "score": "30.0", "color": "#00ff00"}]
    monkeypatch.setattr(sessions_module, "latest_upload_overlays", lambda: faces)
    sign_in(client, browser_session_id=SUBJECT.session_id)

    assert upload(client, jpeg(1280, 720)).get_json()["faces"] == faces


def test_a_result_is_drawn_as_the_server_would_draw_it():
    from recognize_face import FaceOverlay, overlay_as_json
    from vision.geometry import FaceBox

    drawn = overlay_as_json(
        FaceOverlay(FaceBox(10, 20, 110, 140), "Jeff", "Present", 31.25, 1)
    )

    assert drawn == {
        "box": [10, 20, 110, 140],
        "name": "Jeff",
        "status": "Present",
        "score": "31.2",
        # status_color() is BGR (0, 255, 0); the page wants RGB hex.
        "color": "#00ff00",
    }


def test_an_unmatched_face_shows_no_score():
    from recognize_face import FaceOverlay, overlay_as_json
    from vision.geometry import FaceBox

    drawn = overlay_as_json(FaceOverlay(FaceBox(0, 0, 1, 1), "Unknown", "Unknown", 999.0, 1))

    assert drawn["score"] == "--"


def test_uploads_keep_the_recognition_slot_alive(hooks):
    # The upload-driven recognition thread has no stream to heartbeat with.
    # Without uploads counting, any /video_feed could take its slot over as
    # "abandoned" ten seconds after Start.
    from vision.session import VIEWER_STALE_SECONDS, SessionBusy

    now = [0.0]
    recognition = RecognitionSession(
        hooks.build(), clock=lambda: now[0], register_exit_hook=lambda _fn: None
    )
    recognition.start(SUBJECT, camera=BrowserCamera())
    recognition.acquire_viewer()

    now[0] += VIEWER_STALE_SECONDS + 1
    recognition.submit_frame(FRAME)

    with pytest.raises(SessionBusy):
        recognition.acquire_viewer()

    recognition.stop()


# --- the page always carries the camera settings ----------------------------


def test_attendance_html_is_rendered_only_through_the_helper():
    """
    /end-attendance rendered the page itself and left the camera settings
    out, so after End the webcam opened at its smallest mode (640x480) and
    every frame of the next session was refused. Two render calls for one page
    is how that happened; one is the fix.
    """
    import ast
    from pathlib import Path

    offenders = []

    for path in Path(__file__).resolve().parent.parent.glob("web/*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))

        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "render_template"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "attendance.html"
            ):
                offenders.append(f"{path.name}:{node.lineno}")

    # Exactly one: the call inside _render_attendance_page().
    assert len(offenders) == 1, offenders


def test_the_rendered_page_carries_real_camera_settings(flask_app):
    import re

    from web.sessions import _render_attendance_page

    with flask_app.test_request_context("/attendance"):
        html = _render_attendance_page(subjects=[], running_subject_id=None)

    for name, expected in (
        ("data-recognition-width", RECOGNITION_FRAME_WIDTH),
        ("data-recognition-height", RECOGNITION_FRAME_HEIGHT),
        ("data-min-frame-width", 960),
    ):
        match = re.search(name + r'="([^"]*)"', html)
        assert match, f"{name} missing"
        assert match.group(1) == str(expected), f"{name}={match.group(1)!r}"
