"""
vision/pose.py, and proof that extracting it changed nothing.

The gates in `vision/pose.py` were lifted out of `capture_dataset.py` so that
browser-side enrolment judges a frame the way the OpenCV capture window did.
That is a *claim*, and lessons.md L7 is about exactly this: a behaviour-
preserving refactor is testable, the old code is one `git show` away, and a
docstring asserting equivalence is not a measurement.

So the first half of this file is a differential test. The original functions
are lifted out of the pre-extraction blob with `ast` - `capture_dataset.py`
cannot be imported, it opens a camera at module scope - and run beside the new
ones over tens of thousands of randomised synthetic meshes. Nothing may differ.

Three of the four gates are covered that way. The fourth, `get_head_pose`, is
not, and deliberately: the 2026-08-21 fix changed its behaviour on purpose - it
measures yaw and pitch against the face box now, not the frame - so equivalence
with the original is the wrong thing to assert. Its differential test was
removed rather than repaired, because there is nothing left for it to prove.
The behavioural half of this file carries it instead, and
test_the_pose_verdict_does_not_depend_on_how_close_the_face_is is the case the
old implementation could not have passed.

The second half is ordinary behavioural coverage: what each gate is actually
for, written so a future change that shifts a threshold has to admit it.
"""

from __future__ import annotations

import ast
import random
import subprocess

import pytest

from tests.conftest import PROJECT_ROOT
from vision import pose as new
from vision.landmarks import (
    LEFT_EYE_OUTER,
    LEFT_MOUTH,
    LEFT_UPPER_CHEEK,
    LOWER_LIP,
    NOSE_TIP,
    RIGHT_EYE_OUTER,
    RIGHT_MOUTH,
    RIGHT_UPPER_CHEEK,
    UPPER_LIP,
)

# The pre-extraction capture_dataset.py, pinned by blob hash rather than by
# ref. A branch name would drift the moment the file is deleted or main moves;
# a blob hash is what the differential test is actually about and cannot change
# under it. This is `git rev-parse main:capture_dataset.py` taken at the start
# of Phase 4, before any of this work.
ORIGINAL_BLOB = "9179e57e5f33563c55a429e376c7e5478ddee613"

# Lifted from the blob: three of the four gates, plus the module constants they
# close over. The constants are lifted too rather than imported from
# vision.pose, because importing them would make the comparison circular - the
# test would prove the new code agrees with itself.
#
# ⚠️ `get_head_pose` is deliberately absent, and so are the LEFT_EYE/RIGHT_EYE
# aliases only it read. It is the one gate whose behaviour was **meant** to
# change after the extraction: the 2026-08-21 fix made yaw and pitch fractions
# of the face box rather than of the frame, so the original and the current
# implementation disagree by design and a differential test between them could
# only ever fail. What replaces it is not a weaker test but a stronger one:
# test_the_pose_verdict_does_not_depend_on_how_close_the_face_is asserts the
# property the old code got wrong, which agreeing with the old code cannot.
ORIGINAL_FUNCTIONS = (
    "calculate_face_ratio",
    "good_distance",
    "face_centered",
    "detect_expression",
)

ORIGINAL_CONSTANTS = (
    "MIN_FACE_RATIO",
    "MAX_FACE_RATIO",
    "CENTER_TOLERANCE",
)

FRAME_WIDTH = new.CANONICAL_FRAME_WIDTH
FRAME_HEIGHT = new.CANONICAL_FRAME_HEIGHT

LANDMARK_COUNT = 478


class FakeLandmark:
    __slots__ = ("x", "y")

    def __init__(self, x: float, y: float) -> None:
        self.x = x
        self.y = y


class FakeMesh:
    """Stands in for a MediaPipe NormalizedLandmarkList."""

    __slots__ = ("landmark",)

    def __init__(self, landmark: list[FakeLandmark]) -> None:
        self.landmark = landmark


@pytest.fixture(scope="module")
def original():
    """
    The pre-extraction functions, exec'd in a clean namespace.

    `capture_dataset.py` runs a camera capture session at import, so it cannot
    be imported to be inspected - which is precisely the case where a refactor
    is riskiest and skipping verification is most tempting. `ast` lifts the
    definitions out without executing the module.
    """
    try:
        source = subprocess.run(
            ["git", "cat-file", "blob", ORIGINAL_BLOB],
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=True,
        ).stdout.decode("utf-8")
    except (subprocess.CalledProcessError, FileNotFoundError) as error:
        pytest.skip(f"Cannot read the pre-extraction blob: {error}")

    tree = ast.parse(source)

    namespace: dict = {
        "LEFT_EYE_OUTER": LEFT_EYE_OUTER,
        "RIGHT_EYE_OUTER": RIGHT_EYE_OUTER,
        "NOSE_TIP": NOSE_TIP,
        "LEFT_MOUTH": LEFT_MOUTH,
        "RIGHT_MOUTH": RIGHT_MOUTH,
        "UPPER_LIP": UPPER_LIP,
        "LOWER_LIP": LOWER_LIP,
        "LEFT_UPPER_CHEEK": LEFT_UPPER_CHEEK,
        "RIGHT_UPPER_CHEEK": RIGHT_UPPER_CHEEK,
    }

    wanted = []

    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in ORIGINAL_FUNCTIONS:
            wanted.append(node)
        elif isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if any(name in ORIGINAL_CONSTANTS for name in names):
                wanted.append(node)

    exec(  # noqa: S102 - lifting the old implementation is the point
        compile(ast.Module(body=wanted, type_ignores=[]), "<original>", "exec"),
        namespace,
    )

    missing = [name for name in ORIGINAL_FUNCTIONS if name not in namespace]
    assert not missing, (
        f"Did not lift {missing} out of the original. The differential test "
        "would pass vacuously."
    )

    return namespace


# The nine landmarks vision/pose.py actually reads. Randomising only these,
# into a mesh allocated once, is what keeps 200,000 trials to a couple of
# seconds instead of a minute and a half of building landmarks nothing looks
# at. The set is asserted complete by test_the_read_landmarks_are_all_of_them.
READ_LANDMARKS = (
    NOSE_TIP,
    LEFT_EYE_OUTER,
    RIGHT_EYE_OUTER,
    LEFT_MOUTH,
    RIGHT_MOUTH,
    UPPER_LIP,
    LOWER_LIP,
    LEFT_UPPER_CHEEK,
    RIGHT_UPPER_CHEEK,
)


def random_mesh(rng: random.Random, mesh: FakeMesh | None = None) -> FakeMesh:
    """
    A mesh with every landmark this module reads placed at random.

    Deliberately not anatomically constrained. Hand-picked cases all sit in the
    middle of a band, and L7 records that every one of the 105 disagreements
    found in the MA-4 extraction lived at an edge.

    Pass `mesh` to reuse one allocation across trials; the coordinates are
    replaced, so each trial still sees fresh values.
    """
    if mesh is None:
        mesh = FakeMesh([FakeLandmark(0.0, 0.0) for _ in range(LANDMARK_COUNT)])

    for index in READ_LANDMARKS:
        mesh.landmark[index] = FakeLandmark(rng.uniform(0.0, 1.0), rng.uniform(0.0, 1.0))

    return mesh


def random_box(rng: random.Random) -> tuple[int, int, int, int]:
    width = rng.randint(1, FRAME_WIDTH)
    height = rng.randint(1, FRAME_HEIGHT)
    x = rng.randint(-width, FRAME_WIDTH)
    y = rng.randint(-height, FRAME_HEIGHT)

    return x, y, width, height


# ==============================
# Differential: nothing changed
# ==============================

TRIALS = 40_000


def test_face_ratio_is_unchanged(original):
    rng = random.Random(20260809)
    mismatches = []

    for _ in range(TRIALS):
        _, _, width, height = random_box(rng)

        before = original["calculate_face_ratio"](
            FRAME_WIDTH, FRAME_HEIGHT, width, height
        )
        after = new.calculate_face_ratio(FRAME_WIDTH, FRAME_HEIGHT, width, height)

        if before != after:
            mismatches.append((width, height, before, after))

    assert not mismatches, f"{len(mismatches)} of {TRIALS} differ: {mismatches[:5]}"


def test_good_distance_is_unchanged(original):
    rng = random.Random(20260810)
    mismatches = []

    for _ in range(TRIALS):
        _, _, width, height = random_box(rng)

        before = original["good_distance"](FRAME_WIDTH, FRAME_HEIGHT, width, height)
        after = new.good_distance(FRAME_WIDTH, FRAME_HEIGHT, width, height)

        if before != after:
            mismatches.append((width, height, before, after))

    assert not mismatches, f"{len(mismatches)} of {TRIALS} differ: {mismatches[:5]}"


def test_face_centered_is_unchanged(original):
    rng = random.Random(20260811)
    mismatches = []

    for _ in range(TRIALS):
        x, y, width, height = random_box(rng)

        before = original["face_centered"](
            FRAME_WIDTH, FRAME_HEIGHT, x, y, width, height
        )
        after = new.face_centered(FRAME_WIDTH, FRAME_HEIGHT, x, y, width, height)

        if before != after:
            mismatches.append((x, y, width, height, before, after))

    assert not mismatches, f"{len(mismatches)} of {TRIALS} differ: {mismatches[:5]}"


def test_expression_is_unchanged(original):
    rng = random.Random(20260813)
    mesh = None
    mismatches = []

    for _ in range(TRIALS):
        mesh = random_mesh(rng, mesh)

        before = original["detect_expression"](mesh)
        after = new.detect_expression(mesh)

        if before != after:
            mismatches.append((before, after))

    assert not mismatches, f"{len(mismatches)} of {TRIALS} differ: {mismatches[:5]}"


def test_the_random_meshes_exercise_every_pose():
    """
    A mesh generator that only ever produced STRAIGHT would make the pose
    thresholds untested by anything here, and would have hidden the
    frame-relative bug for another release. This asserts it actually reaches
    all five branches.
    """
    rng = random.Random(20260814)
    mesh = None
    seen = set()

    for _ in range(5_000):
        mesh = random_mesh(rng, mesh)
        seen.add(new.get_head_pose(mesh, 1, 1, 1, 1)[0])

    assert seen == {"STRAIGHT", "LEFT", "RIGHT", "UP", "DOWN"}, (
        f"Random meshes only reached {sorted(seen)}"
    )


def test_the_random_meshes_exercise_both_expressions():
    rng = random.Random(20260816)
    mesh = None
    seen = set()

    for _ in range(5_000):
        mesh = random_mesh(rng, mesh)
        seen.add(new.detect_expression(mesh))

    assert seen == {"SMILE", "NEUTRAL"}, f"Random meshes only reached {sorted(seen)}"


def test_the_read_landmarks_are_all_of_them():
    """
    READ_LANDMARKS is an optimisation, and an optimisation that leaves a
    landmark out would randomise less than the differential test claims to.
    This reads the module's source and checks nothing else is indexed.
    """
    import inspect

    source = inspect.getsource(new)

    referenced = {
        name
        for name in dir(new)
        if name.isupper() and f"landmark[{name}]" in source
    }

    expected = {
        "NOSE_TIP",
        "LEFT_EYE_OUTER",
        "RIGHT_EYE_OUTER",
        "LEFT_MOUTH",
        "RIGHT_MOUTH",
        "UPPER_LIP",
        "LOWER_LIP",
        "LEFT_UPPER_CHEEK",
        "RIGHT_UPPER_CHEEK",
    }

    assert referenced == expected, (
        f"vision/pose.py reads {sorted(referenced)}; READ_LANDMARKS covers "
        f"{sorted(expected)}. Update READ_LANDMARKS."
    )


def test_the_random_boxes_exercise_both_sides_of_every_framing_gate():
    """The same non-vacuity check for the framing gates."""
    rng = random.Random(20260815)

    distances = set()
    centred = set()

    for _ in range(5_000):
        x, y, width, height = random_box(rng)
        distances.add(new.good_distance(FRAME_WIDTH, FRAME_HEIGHT, width, height))
        centred.add(new.face_centered(FRAME_WIDTH, FRAME_HEIGHT, x, y, width, height))

    assert distances == {True, False}
    assert centred == {True, False}


# ==============================
# Behaviour
# ==============================


def mesh_with(positions: dict[int, tuple[float, float]]) -> FakeMesh:
    """A mesh with everything at the origin except the named landmarks."""
    landmarks = [FakeLandmark(0.0, 0.0) for _ in range(LANDMARK_COUNT)]

    for index, (x, y) in positions.items():
        landmarks[index] = FakeLandmark(x, y)

    return FakeMesh(landmarks)


def face_at(yaw: float, pitch: float) -> FakeMesh:
    """
    A mesh whose nose sits `yaw` right and `pitch` below the eye midpoint.

    The eyes are placed symmetrically about the origin so that the midpoint is
    exactly 0.0 and the nose's coordinates *are* the yaw and pitch. Building it
    around 0.5 instead costs a rounding step - `(0.40 + 0.110) - 0.40` is
    0.10999999999999999, not 0.110 - which is enough to land a boundary case on
    the wrong side of a `>=` and make the test lie about the code.

    `get_head_pose` is pure arithmetic on landmark coordinates and never
    assumes the 0..1 range MediaPipe happens to use, so this is legitimate.
    """
    return mesh_with(
        {
            LEFT_EYE_OUTER: (-0.10, 0.0),
            RIGHT_EYE_OUTER: (0.10, 0.0),
            NOSE_TIP: (yaw, pitch),
        }
    )


def pose_of(yaw: float, pitch: float) -> str:
    """
    The verdict for a head whose yaw/pitch are already face-relative.

    Unit frame and unit face, so `get_head_pose()`'s conversion is the
    identity and the mesh coordinates *are* the fractions the thresholds are
    expressed in. That keeps these cases readable while still driving the
    real signature - which now requires the face box, because the thresholds
    are fractions of it (see the module docstring).
    """
    return new.get_head_pose(face_at(yaw, pitch), 1, 1, 1, 1)[0]


@pytest.mark.parametrize(
    ("yaw", "pitch", "expected"),
    [
        (0.10, 0.23, "LEFT"),
        (-0.10, 0.23, "RIGHT"),
        (0.0, 0.30, "DOWN"),
        (0.0, 0.15, "UP"),
        (0.0, 0.23, "STRAIGHT"),
    ],
)
def test_head_pose_classification(yaw, pitch, expected):
    assert pose_of(yaw, pitch) == expected


def test_yaw_decides_before_pitch():
    """
    A turned head is LEFT or RIGHT even when the pitch would also qualify.
    Preserved from the original's ordering, and it matters: the LEFT stage is
    collectable by someone who also happens to be looking down.
    """
    assert pose_of(0.10, 0.40) == "LEFT"
    assert pose_of(-0.10, 0.05) == "RIGHT"


def test_the_pose_verdict_does_not_depend_on_how_close_the_face_is():
    """
    ⚠️ **The regression this file exists to prevent from now on**, and the
    defect it replaces `test_straight_is_a_narrow_band` with.

    That test asserted `PITCH_DOWN - PITCH_UP == 0.015` and called STRAIGHT "a
    narrow band ... spelled out because it is surprising". It was surprising
    because it was wrong: the band was 0.015 of *frame* height, so whether a
    head counted as straight was a statement about where the subject stood.
    Measured on the real enrolment set, a face looking directly at the camera
    was classified UP at a 400 px face box, STRAIGHT only between about 500
    and 560 px, and DOWN from 600 px upward - which is what "it can't detect
    looking forward face" turned out to mean.

    The same head at any size must now give the same answer.
    """
    # One head: nose a quarter of a face-height below the eye line, dead
    # centre. Frontal by construction.
    for face_px in (100, 250, 500, 800, 1200):
        yaw_px = 0.0
        pitch_px = 0.23 * face_px

        verdict = new.get_head_pose(
            face_at(yaw_px / 1920, pitch_px / 1080),
            1920,
            1080,
            face_px,
            face_px,
        )[0]

        assert verdict == "STRAIGHT", (
            f"a frontal head reads {verdict} at a {face_px} px face box. The "
            f"thresholds are size-dependent again."
        )


def smiling_mesh(mouth_open: float, lift: float) -> FakeMesh:
    """
    A mouth `mouth_open` tall with its corners `lift` below the cheeks.

    Note the direction, because the name reads the other way: y grows
    downwards, so `lift` here is `corner.y - cheek.y`, and a *larger* value
    means the corners hang *lower*. A smile pulls them up, which makes the
    number smaller - hence `SMILE_MAX_LIFT` being an upper bound.
    """
    return mesh_with(
        {
            LEFT_MOUTH: (0.40, 0.70),
            RIGHT_MOUTH: (0.60, 0.70),
            UPPER_LIP: (0.50, 0.70),
            LOWER_LIP: (0.50, 0.70 + mouth_open),
            LEFT_UPPER_CHEEK: (0.40, 0.70 - lift),
            RIGHT_UPPER_CHEEK: (0.60, 0.70 - lift),
        }
    )


def test_a_wide_closed_lifted_mouth_is_a_smile():
    assert new.detect_expression(smiling_mesh(mouth_open=0.01, lift=0.0)) == "SMILE"


def test_an_open_mouth_is_not_a_smile():
    """Speech drives the width-to-opening ratio down. That is the point."""
    assert new.detect_expression(smiling_mesh(mouth_open=0.20, lift=0.0)) == "NEUTRAL"


def test_corners_hanging_below_the_cheeks_are_not_a_smile():
    """
    The lift condition is load-bearing: a mouth this wide and this closed
    satisfies the score condition on its own, so only the lift stops a
    downturned neutral mouth being logged as a smile.
    """
    assert new.detect_expression(smiling_mesh(mouth_open=0.01, lift=0.50)) == "NEUTRAL"

    # And the score condition alone would have accepted it - which is what
    # makes the assertion above about the lift rather than about the width.
    assert new.detect_expression(smiling_mesh(mouth_open=0.01, lift=0.0)) == "SMILE"


def test_a_perfectly_closed_mouth_does_not_divide_by_zero():
    assert new.detect_expression(smiling_mesh(mouth_open=0.0, lift=0.0)) == "SMILE"


def test_distance_bands_do_not_overlap_and_ascend():
    """
    CLOSE, MEDIUM and FAR have to be distinguishable or the three stages
    collect the same picture three times.
    """
    close = new.distance_band("CLOSE")
    medium = new.distance_band("MEDIUM")
    far = new.distance_band("FAR")

    assert far[1] < medium[1] < close[1]
    assert far[0] < medium[0] < close[0]
    assert medium[1] < close[0], "MEDIUM and CLOSE overlap"
    assert far[1] < medium[1]


def test_every_distance_band_sits_inside_the_global_limits():
    """
    A stage band outside MIN/MAX_FACE_RATIO would be uncollectable: the global
    gate runs first and would reject every frame the stage wanted.
    """
    for stage in ("CLOSE", "MEDIUM", "FAR"):
        minimum, maximum = new.distance_band(stage)

        assert minimum >= new.MIN_FACE_RATIO, f"{stage} floor below the global gate"
        assert maximum <= new.MAX_FACE_RATIO, f"{stage} ceiling above the global gate"


def test_asking_for_the_distance_band_of_a_pose_stage_is_an_error():
    with pytest.raises(KeyError):
        new.distance_band("SMILE")


@pytest.mark.parametrize(
    ("yaw", "pitch", "straight"),
    [
        (0.0, 0.23, True),
        (0.20, 0.23, False),
        (-0.20, 0.23, False),
        (0.0, 0.50, False),
        (0.0, 0.05, False),
    ],
)
def test_distance_stages_require_a_roughly_frontal_head(yaw, pitch, straight):
    assert new.head_is_roughly_straight(yaw, pitch) is straight


def test_the_distance_straightness_rule_is_looser_than_the_turn_threshold():
    """
    A face the LEFT stage would not yet count as turned must still be accepted
    by a distance stage, or the two rules fight and CLOSE becomes uncollectable
    for anyone who does not hold their head perfectly still.
    """
    assert new.DISTANCE_MAX_YAW_OF_WIDTH > new.STRAIGHT_MAX_YAW_OF_WIDTH


def test_the_canonical_frame_is_the_resolution_the_thresholds_were_tuned_at():
    """
    CENTER_TOLERANCE is absolute pixels and the ratios were measured at
    1920x1080. If the canonical frame ever changes, these numbers have to be
    re-derived rather than carried over - this test is where that argument
    gets had.
    """
    assert (new.CANONICAL_FRAME_WIDTH, new.CANONICAL_FRAME_HEIGHT) == (1920, 1080)
    assert new.CENTER_TOLERANCE == 180
    assert new.MIN_NATIVE_FRAME_WIDTH <= new.CANONICAL_FRAME_WIDTH
