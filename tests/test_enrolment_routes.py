"""
The browser enrolment endpoints, driven the way the capture page drives them.

Two halves, for two different reasons.

**Route behaviour, with fakes** - the ordering guarantee that is FS-9 (images
promoted before the student row is written), the single-session rule, the
upload refusals, and the 413. None of it needs a camera, a face or a database.

**A smoke run over real frames** - real dataset crops composited into canonical
frames and POSTed to /enrol/frame, so the whole chain from an HTTP body through
decoding, MediaPipe, the geometry and quality gates and back out as JSON is
exercised once against actual faces. Skipped when `dataset/` is absent, which
is always in CI.

Every student identifier here is fake (lessons.md L6), and nothing is ever
pointed at the configured dataset directory - the staging and dataset roots are
redirected to tmp_path in every test that writes.
"""

from __future__ import annotations

import io
import os

import cv2
import numpy as np
import pytest

from config.settings import settings
from tests.conftest import PROJECT_ROOT
from vision.pose import CANONICAL_FRAME_HEIGHT, CANONICAL_FRAME_WIDTH

STUDENT = "SEC-TEST-NOBODY"
NAME = "Test Student"

DATASET_DIR = PROJECT_ROOT / "dataset"

BACKGROUND_GREY = 96
COMPOSITE_SIZE = 620


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


@pytest.fixture(autouse=True)
def scratch_dirs(tmp_path, monkeypatch):
    """
    Point staging and dataset at a scratch tree.

    Not optional and not per-test: a bug in a route under test writing into
    the real dataset/ would be the Phase 2 incident again, and there is no
    reason any test here should be able to.
    """
    dataset = tmp_path / "dataset"
    staging = tmp_path / "dataset_staging"
    dataset.mkdir()
    staging.mkdir()

    monkeypatch.setattr(settings, "dataset_dir", dataset)
    monkeypatch.setattr(settings, "dataset_staging_dir", staging)

    return dataset, staging


@pytest.fixture(autouse=True)
def no_running_enrolment(flask_app):
    """
    Leave the slot empty however a test ends.

    Phase 5 moved the registry and the two release helpers out of app.py into
    `services/enrolment.py`'s `EnrolmentSlot`, which owns the detector and the
    staging folder alongside the session. `abandon()` is what `_abandon()` was.
    """
    import web.enrolment as enrolment_module

    yield

    enrolment_module.enrolment_slot.abandon()


def sign_in(client, role="admin"):
    with client.session_transaction() as session:
        session["user"] = f"test-{role}"
        session["role"] = role


def jpeg_bytes(frame):
    ok, buffer = cv2.imencode(".jpg", frame)
    assert ok
    return buffer.tobytes()


def blank_frame():
    return np.full(
        (CANONICAL_FRAME_HEIGHT, CANONICAL_FRAME_WIDTH, 3),
        BACKGROUND_GREY,
        dtype=np.uint8,
    )


def start(client, student_id=STUDENT, name=NAME):
    return client.post(
        "/enrol/start",
        json={"student_id": student_id, "student_name": name, "record": {}},
    )


def send_frame(client, frame=None, content_type="image/jpeg"):
    body = jpeg_bytes(frame if frame is not None else blank_frame())

    return client.post("/enrol/frame", data=body, content_type=content_type)


# ==============================
# Starting and stopping
# ==============================


def test_a_capture_starts_and_reports_its_first_stage(client, scratch_dirs):
    _, staging = scratch_dirs
    sign_in(client)

    response = start(client)

    assert response.status_code == 201

    body = response.get_json()
    assert body["success"]
    assert body["progress"]["stage"] == "STRAIGHT"
    assert body["progress"]["captured"] == 0
    assert body["progress"]["total"] == 100

    # The staging folder is the student ID alone (todo.md §7.5). It was
    # `{id}_{name}` until Phase 5.
    assert (staging / STUDENT).is_dir()


def test_a_second_capture_is_refused_while_one_is_running(client):
    sign_in(client)
    assert start(client).status_code == 201

    response = start(client, student_id="SEC-TEST-NOONE", name="Other Student")

    assert response.status_code == 409
    assert not response.get_json()["success"]


def test_cancelling_removes_the_staging_folder(client, scratch_dirs):
    dataset, staging = scratch_dirs
    sign_in(client)
    start(client)

    send_frame(client)

    response = client.post("/enrol/cancel")

    assert response.status_code == 200
    assert response.get_json()["success"]
    assert list(staging.iterdir()) == []
    assert list(dataset.iterdir()) == [], "Nothing may reach dataset/"


def test_cancelling_when_nothing_is_running_is_harmless(client):
    sign_in(client)

    assert client.post("/enrol/cancel").get_json()["success"]


def test_a_frame_without_a_session_is_refused(client):
    sign_in(client)

    response = send_frame(client)

    assert response.status_code == 409
    assert "No capture is in progress" in response.get_json()["message"]


def test_status_reports_whether_a_capture_is_running(client):
    sign_in(client)

    assert client.get("/enrol/status").get_json() == {"running": False}

    start(client)
    body = client.get("/enrol/status").get_json()

    assert body["running"]
    assert body["student_id"] == STUDENT


def test_an_unsafe_student_id_never_opens_a_session(client, scratch_dirs):
    _, staging = scratch_dirs
    sign_in(client)

    response = start(client, student_id="../escape")

    assert response.status_code == 400
    assert list(staging.iterdir()) == []


# ==============================
# Uploads
# ==============================


def test_a_frame_that_is_not_a_jpeg_is_refused(client):
    sign_in(client)
    start(client)

    response = client.post(
        "/enrol/frame", data=b"\x89PNG\r\n\x1a\n", content_type="image/jpeg"
    )

    assert response.status_code == 400
    assert "not a JPEG" in response.get_json()["message"]


def test_the_wrong_content_type_is_refused(client):
    sign_in(client)
    start(client)

    response = send_frame(client, content_type="text/plain")

    assert response.status_code == 400
    assert "image/jpeg" in response.get_json()["message"]


def test_an_oversized_body_is_answered_as_json_not_html(client, flask_app):
    """
    ⚠️ Werkzeug refuses a body over MAX_CONTENT_LENGTH *before* the view runs,
    so @json_api never gets a say. Without the handler this test guards, the
    capture page would be handed an HTML error page to JSON.parse and would
    report a syntax error rather than "that frame was too big".
    """
    sign_in(client)
    start(client)

    body = b"\xff\xd8\xff" + os.urandom(settings.max_upload_bytes + 1024)

    response = client.post("/enrol/frame", data=body, content_type="image/jpeg")

    assert response.status_code == 413
    assert response.is_json, "A 413 reached the uploader as HTML"
    assert not response.get_json()["success"]


def test_the_upload_cap_is_configured_on_the_application(flask_app):
    assert flask_app.config["MAX_CONTENT_LENGTH"] == settings.max_upload_bytes


# ==============================
# Finishing - the FS-9 ordering
# ==============================


def test_finishing_an_incomplete_capture_is_refused(client, scratch_dirs):
    dataset, _ = scratch_dirs
    sign_in(client)
    start(client)

    response = client.post("/enrol/finish")

    assert response.status_code == 409
    assert "of 100 images" in response.get_json()["message"]
    assert list(dataset.iterdir()) == []


def test_no_student_row_is_written_before_the_images_exist(client, monkeypatch):
    """
    FS-9, stated as an ordering property rather than a hope.

    /capture_face inserted the row and *then* captured, so a cancelled capture
    left a student with no dataset - which is how the Phase 0 outage was made.
    Here the database is armed with a tripwire: any INSERT during a start, a
    frame or a cancel fails the test.
    """
    import web.enrolment as enrolment_module

    def tripwire(*args, **kwargs):
        raise AssertionError(
            "The database was touched during an in-progress capture"
        )

    # The blueprint's own reference, not infra.db's: `from infra.db import
    # db_cursor` binds the name in the module that imported it, so patching
    # the source module would leave the route holding the original.
    monkeypatch.setattr(enrolment_module, "db_cursor", tripwire)

    sign_in(client)
    assert start(client).status_code == 201

    send_frame(client)

    assert client.post("/enrol/cancel").get_json()["success"]


# ==============================
# Smoke: real frames, real gates
# ==============================


pytestmark_dataset = pytest.mark.skipif(
    not DATASET_DIR.is_dir() or not any(DATASET_DIR.iterdir()),
    reason="needs the gitignored dataset/ - never present in CI",
)


def real_crops(limit=12):
    """A handful of real aligned crops from the first populated student."""
    for folder in sorted(DATASET_DIR.iterdir()):
        if not folder.is_dir():
            continue

        images = sorted(
            (p for p in folder.iterdir() if p.suffix.lower() == ".jpg"),
            key=lambda p: int(p.stem) if p.stem.isdigit() else 0,
        )

        if len(images) < limit:
            continue

        return [cv2.imread(str(p), cv2.IMREAD_GRAYSCALE) for p in images[:limit]]

    return []


def composite(crop):
    """A real face centred in a canonical frame, the way a webcam would see it."""
    frame = blank_frame()

    face = cv2.resize(
        crop, (COMPOSITE_SIZE, COMPOSITE_SIZE), interpolation=cv2.INTER_CUBIC
    )
    face = cv2.cvtColor(face, cv2.COLOR_GRAY2BGR)

    x = (CANONICAL_FRAME_WIDTH - COMPOSITE_SIZE) // 2
    y = (CANONICAL_FRAME_HEIGHT - COMPOSITE_SIZE) // 2
    frame[y : y + COMPOSITE_SIZE, x : x + COMPOSITE_SIZE] = face

    return frame


@pytest.mark.slow
@pytestmark_dataset
def test_real_frames_reach_the_gates_and_are_judged(client):
    """
    The whole chain once, against real faces: HTTP body -> decode ->
    canonicalise -> MediaPipe -> geometry -> framing -> align -> quality ->
    stage gate -> JSON.

    It asserts the pipeline *runs and judges*, not that these particular
    composited stills satisfy the STRAIGHT pose - they are 200x200 aligned
    crops scaled up and pasted onto flat grey, which is not what a webcam
    sees. Whether a real face at a real distance passes is the live run that
    is handed to the user, exactly as it was for recognition in Phase 3.
    """
    crops = real_crops()

    if not crops:
        pytest.skip("no populated dataset folder with enough images")

    sign_in(client)
    assert start(client).status_code == 201

    messages = set()

    for crop in crops:
        response = send_frame(client, composite(crop))

        assert response.status_code == 200, response.get_data(as_text=True)

        body = response.get_json()
        assert body["success"]

        progress = body["progress"]
        assert progress["total"] == 100
        assert progress["stage"] in {
            "STRAIGHT", "LEFT", "RIGHT", "UP", "DOWN",
            "SMILE", "CLOSE", "MEDIUM", "FAR",
        }
        assert isinstance(progress["message"], str) and progress["message"]

        messages.add(progress["message"])

    assert messages, "No verdict was produced for any frame"

    # A face was found in at least one frame: every message being "No face
    # detected." would mean the chain ran without ever reaching a gate, and
    # this test would be asserting nothing about the gates at all.
    assert messages != {"No face detected."}, (
        "MediaPipe found no face in any composited frame, so nothing past "
        "detection was exercised"
    )


@pytest.mark.slow
@pytestmark_dataset
def test_a_real_frame_is_accepted_as_a_jpeg_upload(client):
    """The decode path specifically, over a real photograph rather than noise."""
    crops = real_crops(limit=1)

    if not crops:
        pytest.skip("no populated dataset folder")

    from infra.uploads import decode_frame

    body = jpeg_bytes(composite(crops[0]))
    frame = decode_frame(io.BytesIO(body).getvalue(), "image/jpeg")

    assert frame.shape[:2] == (CANONICAL_FRAME_HEIGHT, CANONICAL_FRAME_WIDTH)
