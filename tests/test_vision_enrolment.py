"""
The enrolment capture protocol: the plan, and the per-frame gate chain.

`capture_dataset.py` could not be tested at all. It ran at module scope, opened
a camera on import, and held its state in globals driven by a `while True:`.
Everything here runs with fake hooks and no hardware, which is the point of
moving the protocol into `vision/`.

Two kinds of test:

1. **Differential** - the stage plan is checked against the pre-rewrite
   `required_pose()` and `get_instruction()`, lifted out of the blob with
   `ast`. The distribution of poses across the hundred images decides what the
   model is trained on, so "same plan" is a claim worth testing rather than
   asserting (lessons.md L7).
2. **Behavioural** - each gate refuses what it should, the hold counter has to
   be satisfied continuously, and an image is written only when everything
   passes.
"""

from __future__ import annotations

import ast
import subprocess

import numpy as np
import pytest

from tests.conftest import PROJECT_ROOT
from vision import pose as pose_gates
from vision.enrolment import (
    ALIGN_FAILED,
    CAPTURE_DELAY,
    DEFAULT_PLAN,
    MANY_FACES,
    MAX_IMAGES,
    NO_FACE,
    NOT_STRAIGHT,
    OFF_CENTRE,
    REFUSAL_DIRECTIONS,
    REFUSAL_FALLBACK,
    TOO_CLOSE,
    TOO_FAR,
    EnrolmentComplete,
    EnrolmentHooks,
    EnrolmentPlan,
    EnrolmentRegistry,
    EnrolmentSession,
)
from vision.landmarks import (
    LEFT_EYE_INNER,
    LEFT_EYE_LOWER,
    LEFT_EYE_OUTER,
    LEFT_EYE_UPPER,
    LEFT_MOUTH,
    LEFT_UPPER_CHEEK,
    LOWER_LIP,
    NOSE_TIP,
    RIGHT_EYE_INNER,
    RIGHT_EYE_LOWER,
    RIGHT_EYE_OUTER,
    RIGHT_EYE_UPPER,
    RIGHT_MOUTH,
    RIGHT_UPPER_CHEEK,
    UPPER_LIP,
)

# See tests/test_vision_pose.py - the same pinned pre-rewrite blob.
ORIGINAL_BLOB = "9179e57e5f33563c55a429e376c7e5478ddee613"

WIDTH = pose_gates.CANONICAL_FRAME_WIDTH
HEIGHT = pose_gates.CANONICAL_FRAME_HEIGHT

LANDMARK_COUNT = 478

# Two indices used only to pin the bounding box, chosen because nothing in
# vision/ reads them. The obvious choice of 0 and 1 is wrong: NOSE_TIP is
# landmark 1, so pinning there silently moved the nose and every pose test
# then measured a face that was not the one it had asked for.
CORNER_TOP_LEFT = 400
CORNER_BOTTOM_RIGHT = 401


# ==============================
# Fakes
# ==============================


class FakeLandmark:
    __slots__ = ("x", "y")

    def __init__(self, x: float, y: float) -> None:
        self.x = x
        self.y = y


class FakeMesh:
    __slots__ = ("landmark",)

    def __init__(self, landmark) -> None:
        self.landmark = landmark


def build_face(
    *,
    box=(710, 290, 500, 500),
    yaw_px=0.0,
    pitch_px=None,
    smiling=False,
):
    """
    A synthetic mesh that the enrolment geometry gate accepts.

    Positions are pixels in a canonical frame, converted to the 0..1 landmark
    coordinates MediaPipe uses. Defaults describe a centred, frontal, neutral
    face at a face ratio inside the global distance band.

    `yaw_px` and `pitch_px` are the nose's offset from the eye centre **in
    pixels**, which is what the mesh actually carries. `get_head_pose()`
    divides them by the face box, so what they mean to the gate is
    `yaw_px / box_width` and `pitch_px / box_height`.

    ⚠️ `pitch_px` defaults to the middle of the STRAIGHT band, **expressed
    against the box rather than the frame**. It used to be
    `(PITCH_UP + PITCH_DOWN) / 2 * HEIGHT` — a fraction of the *frame* — and
    that is the defect this helper now has to avoid rebuilding: a nose placed
    relative to the frame is STRAIGHT at one face size and UP or DOWN at every
    other one.
    """
    x1, y1, box_width, box_height = box

    if pitch_px is None:
        # Midway between the two pitch boundaries, as a fraction of the FACE.
        pitch_px = (
            (pose_gates.PITCH_UP_OF_HEIGHT + pose_gates.PITCH_DOWN_OF_HEIGHT)
            / 2
            * box_height
        )

    def at(px, py):
        return FakeLandmark(px / WIDTH, py / HEIGHT)

    centre_x = x1 + box_width / 2

    landmarks = [
        at(centre_x, y1 + box_height / 2) for _ in range(LANDMARK_COUNT)
    ]

    # Pin the bounding box: get_face_box() is the extent of every landmark.
    landmarks[CORNER_TOP_LEFT] = at(x1, y1)
    landmarks[CORNER_BOTTOM_RIGHT] = at(x1 + box_width, y1 + box_height)

    eye_y = y1 + box_height * 0.32
    eye_half = box_width * 0.22
    jitter = box_width * 0.015

    for index, px, py in (
        (LEFT_EYE_OUTER, centre_x - eye_half - jitter, eye_y),
        (LEFT_EYE_INNER, centre_x - eye_half + jitter, eye_y),
        (LEFT_EYE_UPPER, centre_x - eye_half, eye_y - jitter),
        (LEFT_EYE_LOWER, centre_x - eye_half, eye_y + jitter),
        (RIGHT_EYE_INNER, centre_x + eye_half - jitter, eye_y),
        (RIGHT_EYE_OUTER, centre_x + eye_half + jitter, eye_y),
        (RIGHT_EYE_UPPER, centre_x + eye_half, eye_y - jitter),
        (RIGHT_EYE_LOWER, centre_x + eye_half, eye_y + jitter),
    ):
        landmarks[index] = at(px, py)

    # get_head_pose measures the nose against the midpoint of the two OUTER
    # eye corners, which the jitter above leaves at centre_x.
    landmarks[NOSE_TIP] = at(centre_x + yaw_px, eye_y + pitch_px)

    mouth_y = y1 + box_height * 0.75
    mouth_half = box_width * 0.16

    landmarks[LEFT_MOUTH] = at(centre_x - mouth_half, mouth_y)
    landmarks[RIGHT_MOUTH] = at(centre_x + mouth_half, mouth_y)
    landmarks[UPPER_LIP] = at(centre_x, mouth_y - box_height * 0.01)

    # A smile is a wide, closed mouth whose corners sit high against the
    # cheeks; a neutral face here has the corners hanging well below them.
    lower_offset = box_height * 0.005 if smiling else box_height * 0.06
    landmarks[LOWER_LIP] = at(centre_x, mouth_y + lower_offset)

    cheek_y = mouth_y - (box_height * 0.02 if smiling else -box_height * 0.40)
    landmarks[LEFT_UPPER_CHEEK] = at(centre_x - mouth_half, cheek_y)
    landmarks[RIGHT_UPPER_CHEEK] = at(centre_x + mouth_half, cheek_y)

    return FakeMesh(landmarks)


def canonical_frame():
    return np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)


class Clock:
    """A hand-wound monotonic clock, so CAPTURE_DELAY costs no wall time."""

    def __init__(self) -> None:
        self.seconds = 1000.0

    def __call__(self) -> float:
        return self.seconds

    def advance(self, amount=CAPTURE_DELAY + 0.01):
        """
        Default overshoots CAPTURE_DELAY slightly, on purpose.

        Advancing by exactly 0.80 twice does not leave 0.80 between the two
        readings - 1000.8 + 0.8 is 1001.5999999999999 - so the `>=` in
        _delay_elapsed() failed on the second capture and the test blamed the
        code. The margin is the test's problem to solve, not the session's.
        """
        self.seconds += amount


class Harness:
    """A session wired to fakes, with the saved images kept for assertions."""

    def __init__(self, faces=None, align=None, plan=DEFAULT_PLAN):
        self.clock = Clock()
        self.saved = []
        self.faces = faces if faces is not None else [build_face()]
        self._align = align
        # Frames the last capture_one() needed. See its docstring.
        self.frames_used = 0

        self.session = EnrolmentSession(
            student_id="SEC-TEST-NOBODY",
            student_name="Test Student",
            plan=plan,
            hooks=EnrolmentHooks(
                detect=lambda frame: self.faces,
                align=self._do_align,
                save=lambda index, image: self.saved.append((index, image)),
                now=self.clock,
            ),
        )

    def _do_align(self, frame, landmarks):
        if self._align is not None:
            return self._align(frame, landmarks)

        # A uniform mid-grey crop passes the enrolment quality gate on
        # brightness but not on contrast, so use noise with a fixed seed.
        rng = np.random.default_rng(1234)
        return rng.integers(40, 210, size=(200, 200), dtype=np.uint8)

    def offer(self, times=1):
        verdict = None
        for _ in range(times):
            verdict = self.session.offer_frame(canonical_frame())
        return verdict

    def capture_one(self, stage_hold=6):
        """
        Offer frames until exactly one more image is saved.

        ⚠️ **This used to offer a fixed `stage_hold` frames**, which was right
        only while `_save()` reset the hold counter - back then every image
        cost a fresh six frames. It does not any more (that was the whole of
        "capture is too slow"), so a second image in an already-settled stage
        costs **one** frame, and offering six would run past the end of a short
        plan and raise `EnrolmentComplete`.

        Stopping at the save rather than counting frames states what the helper
        was always for, and keeps it correct whichever way the pacing changes.
        `frames_used` is what `test_the_hold_is_paid_once_per_stage` reads.
        """
        self.clock.advance()

        before = self.session.captured
        verdict = None

        for attempt in range(1, stage_hold + 1):
            verdict = self.offer()

            if self.session.captured > before:
                self.frames_used = attempt
                return verdict

        raise AssertionError(
            f"no image was saved in {stage_hold} frames "
            f"(captured is still {before})"
        )


# ==============================
# Differential: the plan is the same one
# ==============================


@pytest.fixture(scope="module")
def original():
    """`required_pose` and `get_instruction` from the pre-rewrite blob."""
    try:
        source = subprocess.run(
            ["git", "cat-file", "blob", ORIGINAL_BLOB],
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=True,
        ).stdout.decode("utf-8")
    except (subprocess.CalledProcessError, FileNotFoundError) as error:
        pytest.skip(f"Cannot read the pre-rewrite blob: {error}")

    tree = ast.parse(source)

    wanted_functions = ("required_pose", "get_instruction")
    wanted_constants = (
        "STRAIGHT_IMAGES", "LEFT_IMAGES", "RIGHT_IMAGES", "UP_IMAGES",
        "DOWN_IMAGES", "SMILE_IMAGES", "CLOSE_IMAGES", "MEDIUM_IMAGES",
        "FAR_IMAGES", "MAX_IMAGES",
    )

    body = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted_functions:
            body.append(node)
        elif isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if any(name in wanted_constants for name in names):
                body.append(node)

    namespace: dict = {}
    exec(  # noqa: S102 - lifting the old implementation is the point
        compile(ast.Module(body=body, type_ignores=[]), "<original>", "exec"),
        namespace,
    )

    assert "required_pose" in namespace, "Nothing lifted; the test would be vacuous"

    return namespace


def test_the_total_image_count_is_unchanged(original):
    assert MAX_IMAGES == original["MAX_IMAGES"] == 100


def test_every_image_belongs_to_the_same_stage_as_before(original):
    """
    All hundred indices, not a sample. The stage mix decides what the model is
    trained on, so an off-by-one at a boundary changes the dataset.
    """
    mismatches = [
        (index, original["required_pose"](index), DEFAULT_PLAN.stage_for(index).name)
        for index in range(MAX_IMAGES)
        if original["required_pose"](index) != DEFAULT_PLAN.stage_for(index).name
    ]

    assert not mismatches, f"Stage plan changed at {mismatches[:5]}"


def test_every_instruction_is_unchanged(original):
    mismatches = [
        (index, original["get_instruction"](index), DEFAULT_PLAN.stage_for(index).instruction)
        for index in range(MAX_IMAGES)
        if original["get_instruction"](index) != DEFAULT_PLAN.stage_for(index).instruction
    ]

    assert not mismatches, f"Instructions changed at {mismatches[:5]}"


def test_the_plan_covers_every_index_exactly_once():
    counts = {}
    for index in range(MAX_IMAGES):
        stage = DEFAULT_PLAN.stage_for(index)
        counts[stage.name] = counts.get(stage.name, 0) + 1

    assert counts == {
        "STRAIGHT": 25, "LEFT": 15, "RIGHT": 15, "UP": 10,
        "DOWN": 10, "SMILE": 10, "CLOSE": 5, "MEDIUM": 5, "FAR": 5,
    }


def test_stage_for_clamps_past_the_end():
    assert DEFAULT_PLAN.stage_for(MAX_IMAGES).name == "FAR"
    assert DEFAULT_PLAN.stage_for(MAX_IMAGES * 10).name == "FAR"


def test_an_empty_plan_is_refused():
    with pytest.raises(ValueError, match="at least one stage"):
        EnrolmentPlan(stages=())


# ==============================
# The canonical frame contract
# ==============================


@pytest.mark.parametrize(
    "shape",
    [(720, 1280, 3), (480, 640, 3), (1080, 1921, 3), (1081, 1920, 3)],
)
def test_a_frame_that_is_not_canonical_is_refused(shape):
    """
    The thresholds are meaningless at any other size, and two of them are
    absolute pixels. Judging a 640x480 upload against them would silently
    apply a centring band three times wider, in proportion, than intended.

    The two off-by-one shapes are there because "close enough" is exactly the
    kind of resize a caller might think is harmless.
    """
    harness = Harness()

    with pytest.raises(ValueError, match="Frames must be 1920x1080"):
        harness.session.offer_frame(np.zeros(shape, dtype=np.uint8))


def test_a_canonical_greyscale_frame_is_accepted():
    """
    Only the first two axes are checked, so a single-channel frame at the right
    size is allowed through to the detector. Stated as a test rather than left
    implicit: the gates all work from landmarks and an aligned crop, neither of
    which cares how many channels arrived.
    """
    harness = Harness()

    verdict = harness.session.offer_frame(
        np.zeros((HEIGHT, WIDTH), dtype=np.uint8)
    )

    assert verdict.total == MAX_IMAGES


def test_a_missing_frame_is_refused():
    harness = Harness()

    with pytest.raises(ValueError, match="Frames must be"):
        harness.session.offer_frame(None)


# ==============================
# The gate chain
# ==============================


def test_no_face_is_refused():
    harness = Harness(faces=[])

    verdict = harness.offer()

    assert verdict.message == NO_FACE
    assert not verdict.accepted
    assert harness.saved == []


def test_two_faces_are_refused():
    harness = Harness(faces=[build_face(), build_face(box=(200, 200, 400, 400))])

    verdict = harness.offer()

    assert verdict.message == MANY_FACES
    assert harness.saved == []


def test_a_face_that_fails_the_geometry_gate_is_still_refused():
    """
    An object that is not a face at all must not become "one face".

    This is the surviving half of `test_a_face_that_fails_the_geometry_gate_
    counts_as_no_face`, which also asserted the message was NO_FACE. **That
    assertion pinned a defect as a feature**, the same way
    `test_straight_is_a_narrow_band` did for the pose thresholds: reporting a
    geometry refusal as "No face detected." is what sent three UAT rounds
    after a detector that was working perfectly. See
    tasks/audit-face-detector.md §1.3.

    What must not change is that the rubbish is refused and nothing is saved.
    """
    rubbish = FakeMesh([FakeLandmark(0.5, 0.5) for _ in range(LANDMARK_COUNT)])
    harness = Harness(faces=[rubbish])

    verdict = harness.offer()

    assert not verdict.accepted
    assert harness.saved == []
    assert verdict.message != NO_FACE, (
        "A face the detector found and the profile refused must not be "
        "reported as though the detector saw nothing."
    )


def test_no_face_is_reported_only_when_the_detector_returned_nothing():
    """The other half of the split, and the reason NO_FACE still exists."""
    harness = Harness(faces=[])

    assert harness.offer().message == NO_FACE


def test_a_geometry_refusal_names_a_direction_the_operator_can_act_on():
    """
    The reason itself ("eye separation out of range") is for the log; the
    screen gets something a person can do. A refusal that leaked an internal
    threshold name onto the capture page would be SE-10's shape.
    """
    rubbish = FakeMesh([FakeLandmark(0.5, 0.5) for _ in range(LANDMARK_COUNT)])
    harness = Harness(faces=[rubbish])

    message = harness.offer().message

    assert message in set(REFUSAL_DIRECTIONS.values()) | {REFUSAL_FALLBACK}


def test_a_geometry_refusal_is_logged_once_per_transition(caplog):
    """
    At a 120 ms upload tick, logging every refused frame is eight identical
    lines a second and the log stops being read. It must log on the change.
    """
    rubbish = FakeMesh([FakeLandmark(0.5, 0.5) for _ in range(LANDMARK_COUNT)])
    harness = Harness(faces=[rubbish])

    with caplog.at_level("WARNING", logger="vision.enrolment"):
        harness.offer(times=10)

    refusals = [r for r in caplog.records if "refusing frames" in r.message]

    assert len(refusals) == 1, (
        f"ten refused frames produced {len(refusals)} log lines, not one"
    )
    assert "face too small" in refusals[0].getMessage(), (
        "the log line must carry the precise reason, which is the half the "
        "operator's message deliberately does not"
    )


def test_a_refusal_that_returns_after_a_good_frame_is_logged_again(caplog):
    """
    Throttling must not swallow a recurrence. Clearing the state on a good
    frame is what separates "log on transition" from "log once ever".
    """
    rubbish = FakeMesh([FakeLandmark(0.5, 0.5) for _ in range(LANDMARK_COUNT)])
    harness = Harness(faces=[rubbish])

    with caplog.at_level("WARNING", logger="vision.enrolment"):
        harness.offer()
        harness.faces = [build_face()]
        harness.offer()
        harness.faces = [rubbish]
        harness.offer()

    refusals = [r for r in caplog.records if "refusing frames" in r.message]

    assert len(refusals) == 2


def test_an_off_centre_face_is_refused():
    harness = Harness(faces=[build_face(box=(20, 290, 500, 500))])

    verdict = harness.offer()

    assert verdict.message == OFF_CENTRE
    assert verdict.box is not None, "The operator still needs to see the box"


def test_a_face_filling_the_frame_is_refused():
    # 1100x1050 centred is a face ratio of 0.557, just past MAX_FACE_RATIO,
    # while keeping the aspect ratio inside the geometry gate's 0.55-1.15.
    harness = Harness(faces=[build_face(box=(410, 15, 1100, 1050))])

    assert harness.offer().message == TOO_FAR


def test_a_tiny_face_is_refused():
    # ✅ `pitch_px` no longer has to be given explicitly, and the comment that
    # used to be here is worth keeping as history. It said the default placed
    # the nose using a fraction of *frame* height - about 111 px - which on a
    # 100 px box put the nose outside the face's own bounding box, so the gate
    # refused it as "no face" before the distance rule was reached. It called
    # that "vision/pose.py's documented frame-normalised-units hazard, showing
    # up here rather than in a room".
    #
    # It showed up in a room on 2026-08-21, and the units are face-relative
    # now, so the default scales with the box and this works at any size.
    harness = Harness(faces=[build_face(box=(940, 520, 100, 100))])

    assert harness.offer().message == TOO_CLOSE


def test_an_unalignable_face_is_refused_without_leaking_the_reason():
    """
    SE-10: exception text does not reach the client. face_preprocessing raises
    ValueError for five different reasons and none of them is worth relaying.
    """
    def refuse(frame, landmarks):
        raise ValueError("Eye distance is too small for reliable alignment.")

    harness = Harness(align=refuse)

    verdict = harness.offer()

    assert verdict.message == ALIGN_FAILED
    assert "Eye distance" not in verdict.message
    assert harness.saved == []


def test_a_poor_quality_crop_is_refused():
    """A flat black crop fails the enrolment brightness floor."""
    harness = Harness(align=lambda f, m: np.zeros((200, 200), dtype=np.uint8))

    verdict = harness.offer()

    assert not verdict.accepted
    assert verdict.message not in (NO_FACE, OFF_CENTRE)
    assert harness.saved == []


# ==============================
# Holding a stage, and saving
# ==============================


def test_a_stage_must_be_held_before_anything_is_saved():
    harness = Harness()

    for frame in range(1, 6):
        verdict = harness.offer()
        assert not verdict.accepted
        assert verdict.hold == frame
        assert verdict.hold_required == 6

    assert harness.saved == []

    verdict = harness.offer()

    assert verdict.accepted
    assert harness.saved and harness.saved[0][0] == 1


def test_the_hold_must_be_continuous():
    """
    Any refusal resets the counter. A subject who drifts out of frame halfway
    through a hold starts that hold again, rather than accumulating credit.
    """
    harness = Harness()
    harness.offer(3)

    harness.faces = []
    harness.offer()

    harness.faces = [build_face()]
    verdict = harness.offer()

    assert verdict.hold == 1
    assert harness.saved == []


def test_images_are_numbered_from_one_and_consecutively():
    harness = Harness()

    for _ in range(4):
        harness.capture_one()

    assert [index for index, _ in harness.saved] == [1, 2, 3, 4]


def test_the_capture_delay_is_enforced_between_images():
    """
    Without it a held pose fills a stage in well under a second with
    near-identical frames.
    """
    harness = Harness()
    harness.capture_one()

    verdict = harness.offer(6)

    assert not verdict.accepted
    assert verdict.message == "Hold still..."
    assert len(harness.saved) == 1

    harness.clock.advance()
    assert harness.offer().accepted


def test_the_counter_does_not_advance_if_the_write_fails():
    """
    A save that raised but still counted would leave a gap in {1..100}.jpg and
    a session reporting complete with 99 images on disk.
    """
    harness = Harness()

    def explode(index, image):
        raise OSError("disk full")

    harness.session._hooks = EnrolmentHooks(
        detect=lambda frame: harness.faces,
        align=harness._do_align,
        save=explode,
        now=harness.clock,
    )

    with pytest.raises(OSError):
        harness.offer(6)

    assert harness.session.captured == 0


# ==============================
# Stage-specific gates
# ==============================


def test_a_frontal_face_does_not_satisfy_the_left_stage():
    harness = Harness()

    for _ in range(25):
        harness.capture_one()

    assert harness.session.captured == 25
    assert harness.session.progress().stage == "LEFT"

    verdict = harness.offer()

    assert not verdict.accepted
    assert "TURN SLIGHTLY LEFT" in verdict.message
    assert "detected STRAIGHT" in verdict.message


def test_turning_satisfies_the_left_stage():
    harness = Harness()

    for _ in range(25):
        harness.capture_one()

    # Past the turn threshold - now 0.085 of FACE width, not of the frame -
    # and still inside the geometry gate's nose band.
    harness.faces = [
        build_face(yaw_px=pose_gates.STRAIGHT_MAX_YAW_OF_WIDTH * 500 * 2)
    ]

    harness.clock.advance()
    verdict = harness.offer(6)

    assert verdict.accepted
    assert harness.session.captured == 26


def test_a_neutral_face_does_not_satisfy_the_smile_stage():
    plan = EnrolmentPlan(stages=(_stage("SMILE"),))
    harness = Harness(plan=plan)

    verdict = harness.offer()

    assert not verdict.accepted
    assert verdict.message == "SMILE"


def test_smiling_satisfies_the_smile_stage():
    plan = EnrolmentPlan(stages=(_stage("SMILE"),))
    harness = Harness(faces=[build_face(smiling=True)], plan=plan)

    harness.clock.advance()

    assert harness.offer(6).accepted


def test_a_distance_stage_refuses_a_face_outside_its_band():
    plan = EnrolmentPlan(stages=(_stage("FAR"),))
    harness = Harness(plan=plan)

    # The default 500x500 box is a face ratio of 0.121, well above FAR's
    # ceiling of 0.075 - so the subject is standing much too close for the
    # stage that asks them to step back.
    assert harness.offer().message == TOO_FAR


def test_a_distance_stage_refuses_a_turned_head():
    plan = EnrolmentPlan(stages=(_stage("CLOSE"),))
    # A 700x700 box is a face ratio of 0.236, inside CLOSE's 0.18-0.32 band,
    # so the distance rule is satisfied and the head angle is what decides.
    #
    # 1.5x the yaw limit rather than 2x: the geometry gate's nose band is
    # 0.18-0.82 of the box width, and at 2x the nose leaves the face entirely,
    # so the frame is refused as "no face" before the distance rule is
    # consulted at all.
    harness = Harness(
        faces=[
            build_face(
                box=(610, 190, 700, 700),
                yaw_px=pose_gates.DISTANCE_MAX_YAW_OF_WIDTH * 700 * 1.5,
            )
        ],
        plan=plan,
    )

    assert harness.offer().message == NOT_STRAIGHT


def test_a_distance_stage_holds_for_fewer_frames_than_a_pose_stage():
    assert _stage("CLOSE").hold_frames < _stage("STRAIGHT").hold_frames


def _stage(name):
    return next(s for s in DEFAULT_PLAN.stages if s.name == name)


# ==============================
# Completion
# ==============================


def test_a_session_completes_and_then_refuses_more_frames():
    plan = EnrolmentPlan(stages=(Stage_of("STRAIGHT", images=2),))
    harness = Harness(plan=plan)

    harness.capture_one()
    verdict = harness.capture_one()

    assert verdict.done
    assert verdict.message == "Capture complete."
    assert harness.session.captured == 2
    assert harness.session.done

    with pytest.raises(EnrolmentComplete):
        harness.offer()


def test_the_hold_is_paid_once_per_stage_not_once_per_image():
    """
    ⚠️ **"Capture is too slow", from the first UAT, measured and fixed.**

    `_save()` used to call `_reset_hold()`, so the subject had to re-earn
    `hold_frames` for *every single image in a stage* - while standing
    perfectly still in a pose they had never left. Six frames at the 200 ms
    upload tick is 1.2 s per image, and 85 of the 100 images are in six-frame
    stages: **102 of the measured 114 s floor**, on a server that spends 15 ms
    judging a frame.

    The hold answers "has this person settled into the pose?" - a question
    about entering a stage. So the first image in a stage costs the hold and
    the rest cost one frame each.
    """
    plan = EnrolmentPlan(stages=(Stage_of("STRAIGHT", images=4),))
    harness = Harness(plan=plan)

    harness.capture_one()
    first = harness.frames_used

    harness.capture_one()
    second = harness.frames_used

    harness.capture_one()
    third = harness.frames_used

    assert first == _stage("STRAIGHT").hold_frames, (
        "settling into a stage must still cost the full hold - that is what "
        "stops a half-made pose being captured"
    )
    assert (second, third) == (1, 1), (
        f"an image in an already-settled stage cost {second} and {third} "
        f"frames. Re-earning the hold per image is what made a capture take "
        f"nearly two minutes of flawless posing."
    )


def test_a_broken_pose_still_has_to_be_settled_again():
    """
    ⚠️ **The half of the hold that must NOT be relaxed**, and the reason the
    test above is not simply "the hold is gone".

    `_refuse()` resets the counter on any gate failure, so a stage has to be
    held *continuously*. Without this a subject could drift out of the pose
    between images and keep collecting them at one frame each - which is a
    worse dataset than the slow capture, and invisible until accuracy moved.
    """
    plan = EnrolmentPlan(stages=(Stage_of("STRAIGHT", images=4),))
    harness = Harness(plan=plan)

    harness.capture_one()
    harness.capture_one()

    assert harness.frames_used == 1

    # Break the pose: no face at all for one frame.
    harness.faces = []
    harness.offer()
    harness.faces = [build_face()]

    harness.capture_one()

    assert harness.frames_used == _stage("STRAIGHT").hold_frames, (
        "the pose was broken and the next image did not re-earn the hold"
    )


def Stage_of(name, images):
    """A one-stage plan built from a real stage, with a shorter image count."""
    from dataclasses import replace

    return replace(_stage(name), images=images)


def test_progress_reports_the_current_stage_without_a_frame():
    harness = Harness()

    progress = harness.session.progress()

    assert progress.captured == 0
    assert progress.total == MAX_IMAGES
    assert progress.stage == "STRAIGHT"
    assert not progress.done


def test_the_verdict_serialises_for_the_capture_page():
    harness = Harness()

    body = harness.offer().as_dict()

    assert set(body) == {
        "captured", "total", "stage", "instruction", "message",
        "accepted", "done", "hold", "hold_required", "box",
    }
    assert body["total"] == MAX_IMAGES


# ==============================
# One at a time
# ==============================


def test_the_registry_refuses_a_second_enrolment():
    registry = EnrolmentRegistry()
    first = Harness().session
    second = Harness().session

    assert registry.start(first)
    assert not registry.start(second)
    assert registry.current is first


def test_finishing_frees_the_slot():
    registry = EnrolmentRegistry()
    first = Harness().session
    second = Harness().session

    registry.start(first)

    assert registry.finish() is first
    assert registry.current is None
    assert registry.start(second)


def test_finishing_an_empty_registry_is_harmless():
    assert EnrolmentRegistry().finish() is None
