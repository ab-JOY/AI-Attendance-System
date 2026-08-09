"""
Drives `generate_frames()` end to end, headless, with no camera.

**Why this file exists.** Until Phase 3 nothing could reach the recognition
loop. It needs a camera, a trained model and a face, so the whole path from
geometry gate -> tracker -> LBPH predict -> identity confirmation -> liveness
-> `save_attendance()` was only ever exercised by hand, by pointing a webcam at
someone. That is the largest and most consequential block in the codebase
(MA-12) and it had no test at all.

The Phase 3a sprint built this as a throwaway script and its handover asked for
it to be committed, because RE-2, PE-4 and SE-12 all rewrite this exact path
and this is the only way to watch it work without hardware.

**How it fakes a camera.** Real dataset crops are 200x200. They are upscaled
and composited onto grey 1280x720 frames - the resolution `start_camera()`
requests - and fed through a stand-in for `CameraReader`. Measured while
building this, on all 100 images of one student:

| composite size | mesh found | full path (gate + quality + predict) |
|---:|---:|---:|
| 240 | 1/100 | - |
| 320 | 48/100 | - |
| 420 | 99/100 | 99/100 |
| **520** | **100/100** | **100/100**, all correct, distance 17.7-28.6 |

520 it is. Smaller faces are not a MediaPipe failure to worry about - they are
simply further from the lens than this test wants to simulate.

**The capture stages are visible in the file numbering**, which is what makes a
liveness challenge scriptable: images 1-25 are the frontal stage, **26-40 turn
left** and **41-55 turn right** (matching `LEFT_IMAGES = 15` and
`RIGHT_IMAGES = 15` in `capture_dataset.py`). So a scripted turn is a slice.

⚠️ **`save_attendance()` is stubbed before the loop is ever started**, not
after. That function reaches the live MySQL database, and this test drives a
real enrolled student ID all the way to it. `tasks/lessons.md` L6 is the story
of two live rows deleted by a test run; the rule that came out of it is that a
test never gets a real identifier to break. Here the identifier has to be real
- the model only knows three faces - so the stub is what stands in for the
rule, and it is installed first.

⚠️ **This is the second and last file allowed to import `recognize_face`.**
See `tests/conftest.py`. It is marked `slow` for that reason.

Skipped when `dataset/` or `trainer/` is absent, which is always the case in
CI - both are gitignored, holding face images of identifiable students and the
biometric templates derived from them.
"""

from __future__ import annotations

import glob
import itertools
import os
from pathlib import Path

import pytest

import infra.db
from vision.liveness import CENTER, LEFT, LivenessChallenge
from vision.session import RecognitionSession, SessionBusy, SessionHooks

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATASET_DIR = PROJECT_ROOT / "dataset"
TRAINER_FILE = PROJECT_ROOT / "trainer" / "trainer.yml"
LABELS_FILE = PROJECT_ROOT / "trainer" / "labels.txt"

FRAME_WIDTH = 1280
FRAME_HEIGHT = 720

# See the table in the module docstring. 520 is the smallest size at which all
# 100 images of a student clear mesh detection, the geometry gate and the
# quality gate.
COMPOSITE_SIZE = 520

# Grey rather than black: a flat black background is what a dead camera
# produces, and CAM-1 exists because that used to be indistinguishable from a
# working one.
BACKGROUND_GREY = 60

# Frontal stage, left-turn stage. Derived from the numbering above.
FRONTAL_IMAGES = range(1, 26)
LEFT_TURN_IMAGES = range(32, 41)


def _requirements_present() -> bool:
    return (
        DATASET_DIR.is_dir()
        and any(DATASET_DIR.iterdir())
        and TRAINER_FILE.exists()
        and LABELS_FILE.exists()
    )


pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not _requirements_present(),
        reason=(
            "needs the gitignored dataset/ and trainer/ - absent in CI by "
            "design, since both hold biometric data"
        ),
    ),
]


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class ScriptedCameraReader:
    """
    Stands in for `CameraReader`: hands out a fixed list of frames, once each.

    When the script runs out it clears `attendance_running`, which is how the
    real loop is asked to stop, so `generate_frames()` terminates the way it
    does in production rather than by having the test abandon the generator
    part way through.
    """

    def __init__(self, frames):
        self._frames = list(frames)
        self._index = 0
        self.reads = 0

        # Set after construction: what it has to call is the session, and the
        # session needs this reader to exist first.
        self.on_exhausted = lambda: None

    def read(self):
        self.reads += 1

        if self._index >= len(self._frames):
            self.on_exhausted()
            return False, None

        frame = self._frames[self._index]
        self._index += 1
        return True, frame.copy()

    def stop(self):
        pass


class FakeCapture:
    """A `cv2.VideoCapture` stand-in that never touches a device."""

    def __init__(self):
        self.released = False

    def isOpened(self):  # noqa: N802 - matches the cv2 API being faked
        return not self.released

    def release(self):
        self.released = True


class AttendanceRecorder:
    """Replaces `save_attendance()`. Records calls, writes nothing."""

    def __init__(self):
        self.calls = []

    def __call__(self, student_id, subject, status):
        self.calls.append((student_id, subject, status))
        return True


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def recognition_module():
    """
    `recognize_face`, imported once for the whole module.

    The import is the expensive thing this file is marked `slow` for.
    """
    import recognize_face

    return recognize_face


@pytest.fixture(scope="module")
def enrolled_student():
    """
    `(student_id, name, folder)` for the first student in `trainer/labels.txt`.

    Read from the labels file rather than hardcoded, so the test follows a
    retrain instead of breaking on one. The identifier is necessarily real -
    the model knows three faces - which is exactly why `save_attendance` is
    stubbed.
    """
    first_line = LABELS_FILE.read_text(encoding="utf-8").strip().splitlines()[0]
    _label, folder_name = first_line.split(",", 1)
    student_id, name = folder_name.strip().split("_", 1)

    folder = DATASET_DIR / folder_name.strip()

    if not folder.is_dir():
        pytest.skip(f"dataset folder missing for {student_id}")

    return student_id, name, folder


@pytest.fixture(scope="module")
def scripted_frames(enrolled_student):
    """
    60 frames: hold still, turn left, come back.

    That sequence is what the current liveness challenge asks for, so the
    frames are the answer to it:

    * 30 frontal - five to clear the provisional window, twenty to fill the
      prediction history, and the confirmation lands in the rest
    * 10 turned left - the challenge movement
    * 20 frontal - back to centre, then the eight post-liveness frames on
      which the identity has to still agree before attendance is written
    """
    import cv2
    import numpy as np

    _student_id, _name, folder = enrolled_student

    def load(number):
        path = folder / f"{number}.jpg"

        if not path.exists():
            pytest.skip(f"dataset image {path.name} missing")

        crop = cv2.imread(str(path))

        if crop is None:
            pytest.skip(f"dataset image {path.name} unreadable")

        return crop

    def composite(crop):
        frame = np.full(
            (FRAME_HEIGHT, FRAME_WIDTH, 3),
            BACKGROUND_GREY,
            dtype=np.uint8,
        )

        face = cv2.resize(
            crop,
            (COMPOSITE_SIZE, COMPOSITE_SIZE),
            interpolation=cv2.INTER_CUBIC,
        )

        x = (FRAME_WIDTH - COMPOSITE_SIZE) // 2
        y = (FRAME_HEIGHT - COMPOSITE_SIZE) // 2
        frame[y:y + COMPOSITE_SIZE, x:x + COMPOSITE_SIZE] = face

        return frame

    frontal = [composite(load(n)) for n in FRONTAL_IMAGES]
    turned = [composite(load(n)) for n in LEFT_TURN_IMAGES]

    if not frontal or not turned:
        pytest.skip("not enough dataset images to script a session")

    def cycle(source, count):
        return [source[i % len(source)] for i in range(count)]

    return cycle(frontal, 30) + cycle(turned, 10) + cycle(frontal, 20)


@pytest.fixture(scope="module")
def driven_session(
    recognition_module,
    enrolled_student,
    scripted_frames,
):
    """
    Runs the real `generate_frames()` over the scripted frames, **once**.

    Module-scoped on purpose: driving 60 frames through MediaPipe and LBPH
    takes about ten seconds, and every assertion below is a different question
    about the same single run. Function scope re-ran the whole session per test
    and cost 72 s for one session's worth of information.

    That means `monkeypatch` (function-scoped) cannot be used, so the patches
    are managed explicitly. They stay active for the duration of the module,
    which is what lets the tripwire below keep watching.
    """
    module = recognition_module
    student_id, _name, _folder = enrolled_student

    with pytest.MonkeyPatch.context() as patch:
        recorder = AttendanceRecorder()

        # First, before anything else can run. See lessons.md L6: this path
        # reaches the live MySQL database with a real enrolled student ID.
        patch.setattr(module, "save_attendance", recorder)

        # And a tripwire behind the stub. If any code in this loop reaches the
        # database - now, or after a future refactor moves the write somewhere
        # else - the test fails loudly instead of quietly writing an attendance
        # row for a real student against a fake subject code.
        #
        # Both seams are armed. PE-7 moved the write from a direct
        # `mysql.connector.connect()` onto the pool, and a tripwire on the seam
        # a refactor has just abandoned protects nothing.
        def refuse_to_connect(*args, **kwargs):
            raise AssertionError(
                "the recognition loop reached the database during a test - "
                "see tasks/lessons.md L6"
            )

        patch.setattr(module.mysql.connector, "connect", refuse_to_connect)
        patch.setattr(infra.db, "get_pool", refuse_to_connect)

        # A fixed sequence, not a random one: this test is about the loop, not
        # about the draw, and the scripted frames can only answer one sequence.
        # The randomness has its own tests in tests/test_vision_liveness.py.
        patch.setattr(
            module,
            "create_liveness_state",
            lambda: LivenessChallenge(
                config=module.LIVENESS_CONFIG,
                sequence=(LEFT, CENTER),
            ),
        )

        # A real session with the real model, the real MediaPipe detector and
        # the real tracker - only the camera is faked. This is what
        # `SessionHooks` is for, and it is why the session lives in `vision/`
        # rather than being tangled into the module: the two things a test
        # cannot supply are exactly the two things injected here.
        capture = FakeCapture()
        reader = ScriptedCameraReader(scripted_frames)

        session = RecognitionSession(
            hooks=SessionHooks(
                open_camera=lambda: capture,
                load_model=module.load_model_and_labels,
                model_signature=module.model_signature,
                make_detector=module.make_detector,
                make_reader=lambda _capture: reader,
                configure_camera=None,
            ),
            config=module.TRACK_CONFIG,
        )

        patch.setattr(module, "session", session)

        # Started through the real `start()`, so the model load, the tracker
        # reset and the lifecycle guards are exercised rather than bypassed.
        if not session.start("SMOKE-TEST-SUBJECT"):
            pytest.skip("no LBPH model loaded - run train_model.py first")

        token = session.acquire_viewer()
        stream = module.generate_frames(token)

        # Consume exactly the script, then close the stream - which is what a
        # browser tab closing does to the generator. Deliberately *not*
        # stop()ping the session to end the loop: stop() resets the tracker,
        # and the tracker is what most of the assertions below read. Closing
        # the stream instead is also the RE-10 case, so the camera state after
        # this line is evidence rather than incidental.
        parts = list(itertools.islice(stream, len(scripted_frames)))
        stream.close()

        yield {
            "module": module,
            "session": session,
            "student_id": student_id,
            "parts": parts,
            "recorder": recorder,
            "capture": capture,
            "reader": reader,
            "token": token,
            "tracker": session.tracker,
        }

        session.stop()


# ---------------------------------------------------------------------------
# The loop produces a stream
# ---------------------------------------------------------------------------


def test_every_frame_is_a_well_formed_mjpeg_part(driven_session):
    """
    The generator's output is what an `<img src>` consumes, so its shape is
    part of the contract - a truncated part shows the operator a broken image
    with nothing in any log.
    """
    parts = driven_session["parts"]

    assert len(parts) == 60, (
        f"expected one part per scripted frame, got {len(parts)}"
    )

    for index, part in enumerate(parts):
        assert part.startswith(b"--frame\r\n"), f"part {index} lacks a boundary"
        assert b"Content-Type: image/jpeg\r\n\r\n" in part, (
            f"part {index} lacks a JPEG content type"
        )
        assert part.endswith(b"\r\n"), f"part {index} is not terminated"

        payload = part.split(b"\r\n\r\n", 1)[1]

        # SOI/EOI. A JPEG that decodes is the only useful assertion here, and
        # the markers are what a decoder looks for first.
        assert payload.startswith(b"\xff\xd8"), f"part {index} is not a JPEG"
        assert payload.rstrip(b"\r\n").endswith(b"\xff\xd9"), (
            f"part {index} JPEG is truncated"
        )


def test_the_loop_consumed_every_scripted_frame(driven_session):
    """One read per frame, no frame silently dropped or re-read."""
    reader = driven_session["reader"]

    assert reader.reads == 60


# ---------------------------------------------------------------------------
# The loop tracks, recognises, confirms and marks attendance
# ---------------------------------------------------------------------------


def test_one_track_is_held_across_the_whole_session(driven_session):
    """
    A single face in front of the camera must stay one track.

    If this reports more than one, tracking is dropping the face and
    re-acquiring it - which resets the prediction history and means the
    identity can never accumulate enough votes to confirm.
    """
    tracker = driven_session["tracker"]

    assert len(tracker.states) == 1, (
        f"expected one track for one face, got {len(tracker.states)}"
    )


def test_the_correct_identity_is_confirmed(driven_session):
    """The end of the pipeline: the right student, locked to the track."""
    tracker = driven_session["tracker"]
    expected_id = driven_session["student_id"]

    state = next(iter(tracker.states.values()))

    assert state.confirmed, "the track never confirmed an identity"
    assert state.student_id == expected_id, (
        f"confirmed {state.student_id!r}, expected {expected_id!r}"
    )


def test_liveness_passes_on_the_scripted_turn(driven_session):
    """
    The scripted left turn and return to centre satisfies the challenge.

    This is the part no other test can reach: liveness reads yaw off real
    MediaPipe landmarks, so it needs real face pixels moving over time.
    """
    tracker = driven_session["tracker"]

    state = next(iter(tracker.states.values()))

    assert state.liveness is not None, "liveness never started"
    assert state.liveness.sequence == (LEFT, CENTER)
    assert state.liveness.passed is True, (
        "the scripted turn and return to centre did not complete the sequence"
    )
    assert state.liveness.restarts == 0, (
        "the challenge timed out and restarted part way through"
    )


def test_attendance_is_saved_exactly_once(driven_session):
    """
    One person, one session, one row - and the duplicate suppression that
    protects it is inside the loop, not the database (RE-3).
    """
    recorder = driven_session["recorder"]
    expected_id = driven_session["student_id"]

    assert len(recorder.calls) == 1, (
        f"expected exactly one save_attendance call, got {recorder.calls}"
    )

    student_id, subject, status = recorder.calls[0]

    assert student_id == expected_id
    assert subject == "SMOKE-TEST-SUBJECT"
    assert status == "Present"


def test_the_student_is_added_to_the_recognised_set(driven_session):
    """Who the session believes was present. Was a module global; now session state."""
    session = driven_session["session"]

    assert driven_session["student_id"] in session.recognized_ids()


def test_no_database_connection_was_opened(driven_session):
    """
    The guard rail, asserted rather than assumed.

    The whole 60-frame session ran with both database seams replaced by
    something that raises, so reaching this assertion at all is the evidence:
    nothing in the loop touched the database. The tripwires are still armed,
    and the checks below prove it, so they are not silently uninstalled by a
    future edit to the fixture.
    """
    module = driven_session["module"]

    assert module.save_attendance is driven_session["recorder"], (
        "save_attendance was not the stub for the whole run"
    )

    with pytest.raises(AssertionError, match="reached the database"):
        module.mysql.connector.connect()

    with pytest.raises(AssertionError, match="reached the database"):
        infra.db.get_pool()


# ---------------------------------------------------------------------------
# A viewer going away does not end the session (RE-10)
# ---------------------------------------------------------------------------


def test_closing_the_stream_does_not_release_the_camera(driven_session):
    """
    RE-10, at the layer it actually broke.

    The old generator ended with `if cap is not None: cap.release()`. A
    generator ends whenever its client goes away, so one operator closing one
    browser tab released the shared camera and ended the session for the whole
    room. The stream above was closed the same way a tab closing closes it -
    and the camera is still open.
    """
    session = driven_session["session"]
    capture = driven_session["capture"]

    assert not capture.released, (
        "closing the video stream released the camera - RE-10 is back"
    )
    assert session.is_running, (
        "closing the video stream ended the attendance session - RE-10 is back"
    )


def test_closing_the_stream_frees_the_viewer_slot(driven_session):
    """
    The other half: the slot has to come back, or the session is unwatchable
    until it is restarted. A `finally` in `generate_frames()` is what does it.
    """
    session = driven_session["session"]

    assert not session.has_viewer

    # And it can be claimed again.
    token = session.acquire_viewer()
    session.release_viewer(token)


def test_a_second_viewer_is_refused_while_one_is_open(driven_session):
    """
    The single-viewer invariant that makes the unlocked per-frame path safe.

    Two generators walking one tracker would double-count identity votes
    toward an attendance decision. `/video_feed` turns this into a 409.
    """
    session = driven_session["session"]

    first = session.acquire_viewer()

    try:
        with pytest.raises(SessionBusy):
            session.acquire_viewer()
    finally:
        session.release_viewer(first)


# The counterpart - that `stop()` *does* release the camera - is in
# tests/test_recognition_session.py. It belongs there rather than here: a test
# that tears down this module-scoped session would leave every assertion after
# it depending on file order.


# ---------------------------------------------------------------------------
# The fixture's own assumptions
# ---------------------------------------------------------------------------


def test_the_scripted_frames_are_what_the_camera_would_produce(scripted_frames):
    """A fake that drifts from the real frame shape tests the wrong thing."""
    assert len(scripted_frames) == 60

    for frame in scripted_frames:
        assert frame.shape == (FRAME_HEIGHT, FRAME_WIDTH, 3)
        assert frame.dtype.name == "uint8"


def test_the_dataset_folder_numbering_still_matches_the_capture_stages(
    enrolled_student,
):
    """
    The scripted turn is a slice of file numbers, so it depends on
    `capture_dataset.py` still collecting 25 frontal images and then 15 left.

    If enrolment is ever re-staged - and Phase 5 replaces it with a browser
    flow - this test says so, instead of the liveness assertions failing for a
    reason that looks like a liveness bug.
    """
    _student_id, _name, folder = enrolled_student

    numbers = {
        int(os.path.basename(path)[:-4])
        for path in glob.glob(str(folder / "*.jpg"))
    }

    missing_frontal = set(FRONTAL_IMAGES) - numbers
    missing_turn = set(LEFT_TURN_IMAGES) - numbers

    assert not missing_frontal, f"frontal images missing: {sorted(missing_frontal)}"
    assert not missing_turn, f"left-turn images missing: {sorted(missing_turn)}"
