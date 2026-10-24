"""
Phone verification through the real pipeline, on real enrolled faces.

The same stimulus as tests/test_recognition_loop_smoke.py - real dataset crops
composited onto 1280x720 grey frames, scripted as "hold still, turn left, come
back" - but fed to `PhoneVerifications` frame by frame, the way phone uploads
arrive, with the real MediaPipe detector, the real model and the real
`recognise_frame()`. Four questions:

1. Does a student's own face, signed in as themselves, get recorded - through
   the window, the liveness challenge and the re-check?
2. Signed in as **another enrolled student**, does the same face record
   nobody? This is the friend-signed-in-as-Jeff case.
3. With frames 2 s apart - a slow phone - does it still confirm?
4. ...and would the classroom's 1.5 s track timeout have stopped it? (Why
   phones get a longer one.)

⚠️ **lessons.md L25: this is a synthetic stimulus.** Composited, perfectly lit
dataset crops say nothing about how a real phone in real light performs. What
these show is that the phone path applies the classroom's rules and the
identity restriction; a real phone has to be measured on a real phone.

⚠️ `save_attendance()` is stubbed first and the database is tripwired, as in
the smoke test (L6): the identifiers here are real students.

Skipped without the gitignored dataset/ and trainer/. Marked slow. Note L36:
this loads trainer/, so do not run it during a retrain.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

import infra.db
from vision.liveness import CENTER, LEFT, LivenessChallenge

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATASET_DIR = PROJECT_ROOT / "dataset"
TRAINER_FILE = PROJECT_ROOT / "trainer" / "trainer.yml"
LABELS_FILE = PROJECT_ROOT / "trainer" / "labels.txt"

FRAME_WIDTH = 1280
FRAME_HEIGHT = 720
COMPOSITE_SIZE = 520  # see the smoke test's table
BACKGROUND_GREY = 60
FRONTAL_IMAGES = range(1, 26)
LEFT_TURN_IMAGES = range(32, 41)


def _requirements_present() -> bool:
    return (
        DATASET_DIR.is_dir()
        and TRAINER_FILE.exists()
        and LABELS_FILE.exists()
    )


pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not _requirements_present(),
        reason="needs the gitignored dataset/ and trainer/ (biometric data)",
    ),
]


class SteppedClock:
    """A tracker clock that a test moves forward, to play a slow phone."""

    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append(args)
        return "Present"


@pytest.fixture(scope="module")
def module():
    import recognize_face

    return recognize_face


@pytest.fixture(scope="module")
def students():
    """The first two distinct students in labels.txt that have a dataset folder."""
    seen = []

    for line in LABELS_FILE.read_text(encoding="utf-8").splitlines():
        if "," not in line:
            continue

        student_id = line.split(",", 1)[1].strip()

        if student_id not in seen and (DATASET_DIR / student_id).is_dir():
            seen.append(student_id)

        if len(seen) == 2:
            return seen

    pytest.skip("needs two enrolled students with dataset folders")


@pytest.fixture(scope="module")
def frames(students):
    """The first student's face: 30 frontal, 10 turned left, 20 frontal."""
    import cv2
    import numpy as np

    folder = DATASET_DIR / students[0]

    def composite(number):
        crop = cv2.imread(str(folder / f"{number}.jpg"))

        if crop is None:
            pytest.skip(f"dataset image {number}.jpg missing")

        frame = np.full((FRAME_HEIGHT, FRAME_WIDTH, 3), BACKGROUND_GREY, np.uint8)
        face = cv2.resize(crop, (COMPOSITE_SIZE, COMPOSITE_SIZE), interpolation=cv2.INTER_CUBIC)
        x = (FRAME_WIDTH - COMPOSITE_SIZE) // 2
        y = (FRAME_HEIGHT - COMPOSITE_SIZE) // 2
        frame[y:y + COMPOSITE_SIZE, x:x + COMPOSITE_SIZE] = face
        return frame

    frontal = [composite(n) for n in FRONTAL_IMAGES]
    turned = [composite(n) for n in LEFT_TURN_IMAGES]

    def cycle(source, count):
        return [source[i % len(source)] for i in range(count)]

    return cycle(frontal, 30) + cycle(turned, 10) + cycle(frontal, 20)


@pytest.fixture(scope="module")
def model(module):
    recognizer, label_map = module.load_model_and_labels()

    if recognizer is None or not label_map:
        pytest.skip("no LBPH model loaded - run train_model.py first")

    return recognizer, label_map


@pytest.fixture
def harness(module, model, monkeypatch):
    """Everything wired as web/api.py wires it, with the write stubbed."""
    from services.phone_verification import PhoneVerifications, phone_track_config
    from vision.session import SessionSnapshot
    from vision.tracking import FaceTracker

    recorder = Recorder()
    monkeypatch.setattr(module, "save_attendance", recorder)

    def refuse_to_connect(*args, **kwargs):
        raise AssertionError("phone verification reached the database - lessons.md L6")

    monkeypatch.setattr(module.mysql.connector, "connect", refuse_to_connect)
    monkeypatch.setattr(infra.db, "get_pool", refuse_to_connect)

    # The scripted frames answer one sequence; the draw is tested elsewhere.
    monkeypatch.setattr(
        module,
        "create_liveness_state",
        lambda: LivenessChallenge(config=module.LIVENESS_CONFIG, sequence=(LEFT, CENTER)),
    )

    recognizer, label_map = model
    subject = module.ActiveSubject(subject_id=-1, session_id=-1, subject_code="PHONE-TEST")
    snapshot = SessionSnapshot(
        running=True,
        subject=subject,
        reader=None,
        detector=None,
        recognizer=recognizer,
        label_map=label_map,
        tracker=None,
        recognized={},
    )

    def build(timeout_seconds=None, clock=None, classroom_rules=False):
        # The phone's rules as web/api.py builds them - or, for a
        # differential, the classroom's with only the phone's timeout.
        if classroom_rules:
            config = dataclasses.replace(
                module.TRACK_CONFIG,
                timeout_seconds=phone_track_config(module.TRACK_CONFIG).timeout_seconds,
            )
        else:
            config = phone_track_config(module.TRACK_CONFIG)

        if timeout_seconds is not None:
            config = dataclasses.replace(config, timeout_seconds=timeout_seconds)

        tracker_kwargs = {} if clock is None else {"clock": clock}

        return PhoneVerifications(
            make_detector=module.make_detector,
            make_tracker=lambda: FaceTracker(config, **tracker_kwargs),
            recognise=module.recognise_frame,
        )

    phones = []

    def make(**kwargs):
        verifications = build(**kwargs)
        phones.append(verifications)
        return verifications

    yield make, snapshot, recorder

    for verifications in phones:
        verifications.discard_all()


def drive(phones, student_id, snapshot, frames, clock=None, step=0.0):
    """Feed frames one at a time until done. Returns (result, frames used, messages)."""
    marked = []
    messages = []

    for count, frame in enumerate(frames, start=1):
        if clock is not None:
            clock.now += step

        result = phones.verify(
            student_id, snapshot, frame, lambda sid, status: marked.append((sid, status))
        )
        messages.append(result.message)

        if result.done or result.refused:
            return result, count, messages, marked

    return result, len(frames), messages, marked


def test_a_student_is_recorded_through_window_liveness_and_recheck(harness, students, frames):
    make, snapshot, recorder = harness
    student_id = students[0]

    result, used, messages, marked = drive(make(), student_id, snapshot, frames)

    assert result.done, f"not recorded after {used} frames; last: {messages[-5:]}"
    assert [args[0] for args in recorder.calls] == [student_id]
    assert marked == [(student_id, "Present")]
    # It passed through the challenge, not around it.
    assert any("Liveness" in message or "Turn" in message for message in messages)
    # And not on the first frames: the phone's window alone is 1 + 12.
    assert used > 13


def test_signed_in_as_another_student_the_same_face_records_nobody(
    harness, students, frames
):
    make, snapshot, recorder = harness
    other_student = students[1]

    result, used, messages, marked = drive(make(), other_student, snapshot, frames)

    assert not result.done
    assert recorder.calls == []
    assert marked == []
    # It never reached a challenge: no identity was ever confirmed.
    assert not any("Turn" in message for message in messages)


def test_a_slow_phone_still_confirms(harness, students, frames):
    make, snapshot, recorder = harness
    clock = SteppedClock()

    result, used, messages, _marked = drive(
        make(clock=clock), students[0], snapshot, frames, clock=clock, step=2.0
    )

    assert result.done, f"slow phone not recorded; last: {messages[-5:]}"


def test_the_classroom_timeout_would_not_have_confirmed_a_slow_phone(
    harness, students, frames
):
    """The differential that justifies PHONE_TRACK_TIMEOUT_SECONDS."""
    make, snapshot, recorder = harness
    clock = SteppedClock()

    result, used, messages, _marked = drive(
        make(timeout_seconds=1.5, clock=clock),
        students[0],
        snapshot,
        frames,
        clock=clock,
        step=2.0,
    )

    assert not result.done
    assert recorder.calls == []


def _drive_in_batches(phones, student_id, snapshot, frames, clock, gap):
    """
    Frames as the app now sends them over 4G (2026-10-25): batches of four,
    judged 0.4 s apart, with `gap` seconds between one batch and the next.
    """
    result = None

    for count, frame in enumerate(frames):
        clock.now += gap if count % 4 == 0 else 0.4
        result = phones.verify(student_id, snapshot, frame, lambda *args: None)

        if result.done or result.refused:
            break

    return result


def test_batches_six_seconds_apart_still_confirm(harness, students, frames):
    """
    The user's phone sent a batch every ~5 s, and the count fell from 10/12
    back to 2 whenever a gap crossed the old 5 s timeout.
    """
    make, snapshot, recorder = harness
    clock = SteppedClock()

    result = _drive_in_batches(
        make(clock=clock), students[0], snapshot, frames, clock, gap=6.0
    )

    assert result.done, f"not recorded; last: {result.message}"


def test_the_old_five_second_timeout_restarted_the_count(harness, students, frames):
    """The differential behind PHONE_TRACK_TIMEOUT_SECONDS = 15."""
    make, snapshot, recorder = harness
    clock = SteppedClock()

    result = _drive_in_batches(
        make(timeout_seconds=5.0, clock=clock), students[0], snapshot, frames, clock, gap=6.0
    )

    assert not result.done
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# The square preview (the user found arm's length too hard, 2026-10-22)
# ---------------------------------------------------------------------------

# The app sends a 1080 square; the face fills its 72% guide. Letterboxed to
# 720 high that is 520 px - the size the smoke test's table shows is detected.
SQUARE = 1080
FACE_IN_SQUARE = 780


@pytest.fixture(scope="module")
def square_frames(students):
    """The same script, as the phone now sends it: a close-up in a square."""
    import cv2
    import numpy as np

    folder = DATASET_DIR / students[0]

    def square(number):
        crop = cv2.imread(str(folder / f"{number}.jpg"))

        if crop is None:
            pytest.skip(f"dataset image {number}.jpg missing")

        frame = np.full((SQUARE, SQUARE, 3), BACKGROUND_GREY, np.uint8)
        face = cv2.resize(crop, (FACE_IN_SQUARE, FACE_IN_SQUARE), interpolation=cv2.INTER_CUBIC)
        offset = (SQUARE - FACE_IN_SQUARE) // 2
        frame[offset:offset + FACE_IN_SQUARE, offset:offset + FACE_IN_SQUARE] = face
        return frame

    frontal = [square(n) for n in FRONTAL_IMAGES]
    turned = [square(n) for n in LEFT_TURN_IMAGES]

    def cycle(source, count):
        return [source[i % len(source)] for i in range(count)]

    return cycle(frontal, 30) + cycle(turned, 10) + cycle(frontal, 20)


def test_a_close_up_square_is_recorded_when_letterboxed(harness, students, square_frames):
    from infra.uploads import letterbox

    make, snapshot, recorder = harness
    frames = [letterbox(frame, (FRAME_WIDTH, FRAME_HEIGHT)) for frame in square_frames]

    result, used, messages, _marked = drive(make(), students[0], snapshot, frames)

    assert result.done, f"close-up square not recorded; last: {messages[-5:]}"
    assert [args[0] for args in recorder.calls] == [students[0]]


def test_the_same_close_up_cropped_to_16_9_is_not(harness, students, square_frames):
    """
    The differential behind the change: the old centre crop cuts the top and
    bottom off the close-up the student framed, so the same face, at the
    same distance, records nobody.
    """
    from infra.uploads import canonicalise

    make, snapshot, recorder = harness
    frames = [canonicalise(frame, (FRAME_WIDTH, FRAME_HEIGHT)) for frame in square_frames]

    result, used, messages, _marked = drive(make(), students[0], snapshot, frames)

    assert not result.done
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# A shaky hand (2026-10-22: "Unknown" mid-count, sudden movement)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def shaky_frames(students):
    """
    The same script, but held the way a hand holds a phone: the face jumps
    from one side of the frame to the other every four frames (no overlap,
    further than the classroom tracker will follow), and every ten frames
    three in a row are motion-blurred past the quality gate.
    """
    import cv2
    import numpy as np

    folder = DATASET_DIR / students[0]
    positions = (40, FRAME_WIDTH - COMPOSITE_SIZE - 40)

    def composite(number, index):
        crop = cv2.imread(str(folder / f"{number}.jpg"))

        if crop is None:
            pytest.skip(f"dataset image {number}.jpg missing")

        frame = np.full((FRAME_HEIGHT, FRAME_WIDTH, 3), BACKGROUND_GREY, np.uint8)
        face = cv2.resize(crop, (COMPOSITE_SIZE, COMPOSITE_SIZE), interpolation=cv2.INTER_CUBIC)

        if index % 10 in (7, 8, 9):
            face = cv2.GaussianBlur(face, (0, 0), 6)

        x = positions[(index // 4) % 2]
        y = (FRAME_HEIGHT - COMPOSITE_SIZE) // 2
        frame[y:y + COMPOSITE_SIZE, x:x + COMPOSITE_SIZE] = face
        return frame

    numbers = (
        [FRONTAL_IMAGES[i % len(FRONTAL_IMAGES)] for i in range(30)]
        + [LEFT_TURN_IMAGES[i % len(LEFT_TURN_IMAGES)] for i in range(10)]
        + [FRONTAL_IMAGES[i % len(FRONTAL_IMAGES)] for i in range(40)]
    )

    return [composite(number, index) for index, number in enumerate(numbers)]


def test_a_shaky_hand_is_still_recorded(harness, students, shaky_frames):
    make, snapshot, recorder = harness

    result, used, messages, _marked = drive(make(), students[0], snapshot, shaky_frames)

    assert result.done, f"shaky phone not recorded; last: {messages[-8:]}"
    assert [args[0] for args in recorder.calls] == [students[0]]
    # A blurred frame mid-count reads as progress, not as "Unknown".
    assert "Unknown" not in messages


def test_the_classroom_rules_would_not_have_recorded_the_shaky_hand(
    harness, students, shaky_frames
):
    """The differential behind PHONE_TRACK_OVERRIDES."""
    make, snapshot, recorder = harness

    result, used, messages, _marked = drive(
        make(classroom_rules=True), students[0], snapshot, shaky_frames
    )

    assert not result.done
    assert recorder.calls == []
