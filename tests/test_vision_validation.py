"""
Tests for the single face-geometry gate (MA-4).

MA-4 found `get_face_box()` and `is_valid_face_candidate()` implemented twice,
in `recognize_face.py` and `capture_dataset.py`, with different thresholds -
so enrolment and recognition accepted different populations of faces and
nothing recorded that they did.

The extraction was verified by differential test before it landed: the
pre-MA-4 functions were pulled out of the `phase-2-security` blob with `ast`
and run against these replacements over 40,000 randomised synthetic meshes.
Result: the recognition gate agreed on all 40,000, and the enrolment gate
disagreed on 105 (0.263%) - every one of them a case where the original's
integer truncation (`x + int(width * 0.18)`, with `nose_x` also truncated)
widened a band by under a pixel. Transcribing the original logic with the
`int()` calls removed agreed with this module on all 40,000, which is what
proves truncation is the *only* difference. The worst measured case sat
0.865 px from the band edge it straddled.

Two things are pinned here:

- the behaviour of each individual check, so a threshold cannot be edited
  silently, and
- **the divergence between the two profiles**, which is the whole reason
  there are two. `test_turned_head_separates_the_profiles` is the load-bearing
  one: if somebody "finishes" MA-4 by collapsing the profiles into a single
  threshold set, that test fails and explains why it must not be done.
"""

from __future__ import annotations

import ast

import pytest

from tests.conftest import PROJECT_ROOT, project_python_files
from vision.geometry import (
    box_area,
    box_center,
    box_iou,
    box_to_xywh,
    get_face_box,
)
from vision.landmarks import (
    LEFT_EYE_INNER,
    LEFT_EYE_LOWER,
    LEFT_EYE_OUTER,
    LEFT_EYE_UPPER,
    LEFT_MOUTH,
    LOWER_LIP,
    NOSE_TIP,
    RIGHT_EYE_INNER,
    RIGHT_EYE_LOWER,
    RIGHT_EYE_OUTER,
    RIGHT_EYE_UPPER,
    RIGHT_MOUTH,
    UPPER_LIP,
)
from vision.validation import (
    ENROLMENT_PROFILE,
    RECOGNITION_PROFILE,
    FaceGeometryProfile,
    frontality_reason,
    is_frontal,
    is_structurally_a_face,
    is_valid_face_candidate,
    rejection_reason,
    structure_rejection_reason,
)

FRAME_WIDTH = 1280
FRAME_HEIGHT = 720

# refine_landmarks=True gives a 478-point mesh. The two iris points below are
# read by neither gate, so they are free to pin the bounding box.
LANDMARK_COUNT = 478
CORNER_TOP_LEFT = 470
CORNER_BOTTOM_RIGHT = 477


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


def build_face(
    *,
    box: tuple[float, float, float, float] = (400.0, 200.0, 240.0, 260.0),
    eye_level: float = 0.32,
    eye_half_width: float = 0.20,
    nose_offset: float = 0.0,
    nose_level: float = 0.55,
    mouth_level: float = 0.75,
    mouth_half_width: float = 0.14,
    roll: float = 0.0,
) -> FakeMesh:
    """
    A synthetic mesh, parameterised as fractions of the face box.

    Defaults describe an ordinary frontal face that both profiles accept;
    each test moves one parameter until a specific check fires.
    """
    x1, y1, width, height = box

    def at(px: float, py: float) -> FakeLandmark:
        return FakeLandmark(px / FRAME_WIDTH, py / FRAME_HEIGHT)

    # Fill the mesh at the box centre, then pin two corners so the computed
    # bounding box is exactly the box asked for.
    landmarks = [at(x1 + width / 2, y1 + height / 2) for _ in range(LANDMARK_COUNT)]
    landmarks[CORNER_TOP_LEFT] = at(x1, y1)
    landmarks[CORNER_BOTTOM_RIGHT] = at(x1 + width, y1 + height)

    center_x = x1 + width / 2
    eye_y = y1 + height * eye_level
    left_x = center_x - width * eye_half_width
    right_x = center_x + width * eye_half_width

    left_y = eye_y - height * roll / 2
    right_y = eye_y + height * roll / 2

    # A small jitter so the four-point eye centre is not identical to the
    # single-point one; that difference is what the two profiles disagree on.
    jitter = width * 0.015

    landmarks[LEFT_EYE_OUTER] = at(left_x - jitter, left_y)
    landmarks[LEFT_EYE_INNER] = at(left_x + jitter, left_y)
    landmarks[LEFT_EYE_UPPER] = at(left_x, left_y - jitter)
    landmarks[LEFT_EYE_LOWER] = at(left_x, left_y + jitter)

    landmarks[RIGHT_EYE_INNER] = at(right_x - jitter, right_y)
    landmarks[RIGHT_EYE_OUTER] = at(right_x + jitter, right_y)
    landmarks[RIGHT_EYE_UPPER] = at(right_x, right_y - jitter)
    landmarks[RIGHT_EYE_LOWER] = at(right_x, right_y + jitter)

    landmarks[NOSE_TIP] = at(
        center_x + width * nose_offset,
        y1 + height * nose_level,
    )

    mouth_y = y1 + height * mouth_level
    landmarks[LEFT_MOUTH] = at(center_x - width * mouth_half_width, mouth_y)
    landmarks[RIGHT_MOUTH] = at(center_x + width * mouth_half_width, mouth_y)
    landmarks[UPPER_LIP] = at(center_x, mouth_y - height * 0.01)
    landmarks[LOWER_LIP] = at(center_x, mouth_y + height * 0.01)

    return FakeMesh(landmarks)


def verdict(mesh: FakeMesh, profile: FaceGeometryProfile) -> str | None:
    box = get_face_box(mesh, FRAME_WIDTH, FRAME_HEIGHT)
    return rejection_reason(mesh, FRAME_WIDTH, FRAME_HEIGHT, box, profile)


ALL_PROFILES = [RECOGNITION_PROFILE, ENROLMENT_PROFILE]


# ---------------------------------------------------------------------------
# The baseline: an ordinary face is accepted by both stages
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile", ALL_PROFILES, ids=lambda p: p.name)
def test_ordinary_frontal_face_is_accepted(profile):
    assert verdict(build_face(), profile) is None


@pytest.mark.parametrize("profile", ALL_PROFILES, ids=lambda p: p.name)
def test_is_valid_face_candidate_agrees_with_rejection_reason(profile):
    """The boolean API is exactly the reason API, so they cannot drift."""
    for nose_offset in (0.0, 0.20, 0.40, -0.35):
        mesh = build_face(nose_offset=nose_offset)
        box = get_face_box(mesh, FRAME_WIDTH, FRAME_HEIGHT)

        accepted = is_valid_face_candidate(
            mesh, FRAME_WIDTH, FRAME_HEIGHT, box, profile
        )
        reason = rejection_reason(mesh, FRAME_WIDTH, FRAME_HEIGHT, box, profile)

        assert accepted == (reason is None)


# ---------------------------------------------------------------------------
# The divergence between the profiles - the reason there are two
# ---------------------------------------------------------------------------


def test_turned_head_separates_the_profiles():
    """
    A head turned far enough to satisfy capture_dataset's LEFT stage is
    accepted for enrolment and refused for recognition.

    This is the finding behind the two-profile design. `capture_dataset.py`
    collects 50 of its 100 images per student at LEFT/RIGHT/UP/DOWN poses;
    the recognition gate is frontal-only because an attendance mark is a
    decision. Collapsing these into one threshold set - the literal reading
    of MA-4 - would make half the enrolment stages uncapturable.
    """
    # Recognition refuses a nose more than 0.28 x width from the eye centre;
    # enrolment's band allows up to 0.32. 0.30 sits between the two, which is
    # precisely the population the profiles disagree about.
    turned = build_face(nose_offset=0.30)

    assert verdict(turned, ENROLMENT_PROFILE) is None
    assert verdict(turned, RECOGNITION_PROFILE) == "face is not frontal enough"


def test_a_turned_head_is_still_structurally_a_face():
    """
    ⚠️ **The split that lets a student pass the liveness challenge.**

    `test_turned_head_separates_the_profiles` above pins the property that a
    turned head is not *recognisable*. This pins the property that it is still
    **a face** - which is a different question, and conflating the two is what
    broke liveness.

    `recognize_face._stream_frames()` calls its gate before
    `tracker.assign()`, and a refused frame hits a bare `continue`. So while
    frontality lived inside the one gate, a student obeying "Turn LEFT" was
    not merely un-recognised: their frame was dropped, their box vanished,
    their track's `last_seen` was never refreshed, and `expire_old_tracks()`
    deleted the track 1.5 s later. They turned back to "Verifying 0/20",
    re-confirmed, drew a fresh random challenge, and repeated indefinitely.

    So `structure_rejection_reason()` must accept exactly what
    `rejection_reason()` refuses *for frontality reasons only*.
    """
    turned = build_face(nose_offset=0.30)
    box = get_face_box(turned, FRAME_WIDTH, FRAME_HEIGHT)

    # The whole gate still refuses it - recognition is unchanged.
    assert verdict(turned, RECOGNITION_PROFILE) == "face is not frontal enough"

    # ... but it is a face, and must keep being tracked and drawn.
    assert (
        structure_rejection_reason(
            turned, FRAME_WIDTH, FRAME_HEIGHT, box, RECOGNITION_PROFILE
        )
        is None
    )
    assert is_structurally_a_face(
        turned, FRAME_WIDTH, FRAME_HEIGHT, box, RECOGNITION_PROFILE
    )
    assert not is_frontal(
        turned, FRAME_WIDTH, FRAME_HEIGHT, box, RECOGNITION_PROFILE
    )


@pytest.mark.parametrize("profile", ALL_PROFILES, ids=lambda p: p.name)
@pytest.mark.parametrize(
    "nose_offset", [0.0, 0.10, 0.20, 0.27, 0.29, 0.35, -0.30, -0.45]
)
def test_the_split_reassembles_into_the_original_gate(profile, nose_offset):
    """
    structure + frontality == the gate that existed before they were split.

    The guard against the split quietly changing what recognition accepts.
    Every threshold in `RECOGNITION_PROFILE` is uncalibrated (Phase 6 derives
    them), so a behaviour change here would be invisible until it showed up as
    an accuracy number nobody could explain - lessons.md L7: when a refactor
    "should be" equivalent, prove it.
    """
    mesh = build_face(nose_offset=nose_offset)
    box = get_face_box(mesh, FRAME_WIDTH, FRAME_HEIGHT)

    whole = rejection_reason(mesh, FRAME_WIDTH, FRAME_HEIGHT, box, profile)
    structure = structure_rejection_reason(
        mesh, FRAME_WIDTH, FRAME_HEIGHT, box, profile
    )
    frontality = frontality_reason(
        mesh, FRAME_WIDTH, FRAME_HEIGHT, box, profile
    )

    assert whole == (structure or frontality)


def test_enrolment_applies_no_frontality_check_at_all():
    """
    `ENROLMENT_PROFILE` sets both frontality fields to None, so the new
    function is a no-op for it however far the head is turned. This is what
    makes the split safe rather than a relaxation: the checks that remain in
    `structure_rejection_reason()` are the ones enrolment has always used
    alone to identify a face, across 50 deliberately off-axis images per
    student.
    """
    for nose_offset in (0.0, 0.30, 0.45, -0.45):
        mesh = build_face(nose_offset=nose_offset)
        box = get_face_box(mesh, FRAME_WIDTH, FRAME_HEIGHT)

        assert (
            frontality_reason(
                mesh, FRAME_WIDTH, FRAME_HEIGHT, box, ENROLMENT_PROFILE
            )
            is None
        )


def test_profiles_declare_their_divergence_explicitly():
    """
    Every check one profile applies and the other does not is a `None` field.

    If a future change makes the profiles identical, this test fails and
    sends the reader to `test_turned_head_separates_the_profiles`.
    """
    recognition_only = {
        "face_area_ratio",
        "eye_level_band_of_box",
        "mouth_width_of_eye_distance",
        "max_nose_offset_of_width",
        "max_mouth_offset_of_width",
    }
    enrolment_only = {
        "nose_x_band_of_box",
        "nose_y_band_of_box",
    }

    for field in recognition_only:
        assert getattr(RECOGNITION_PROFILE, field) is not None, field
        assert getattr(ENROLMENT_PROFILE, field) is None, field

    for field in enrolment_only:
        assert getattr(RECOGNITION_PROFILE, field) is None, field
        assert getattr(ENROLMENT_PROFILE, field) is not None, field

    # Head roll is measured against different denominators by the two stages;
    # exactly one of the pair is set per profile.
    for profile in ALL_PROFILES:
        set_rules = [
            profile.max_eye_roll_of_eye_distance is not None,
            profile.max_eye_roll_of_face_height is not None,
        ]
        assert sum(set_rules) == 1, profile.name


def test_enrolment_accepts_smaller_faces_than_recognition():
    """The FAR capture stage collects faces recognition would refuse outright."""
    small = build_face(box=(400.0, 200.0, 60.0, 66.0))

    assert verdict(small, ENROLMENT_PROFILE) is None
    assert verdict(small, RECOGNITION_PROFILE) == "face too small"


# ---------------------------------------------------------------------------
# Individual checks - one test per rejection branch
# ---------------------------------------------------------------------------


def test_face_below_minimum_size_is_rejected():
    tiny = build_face(box=(400.0, 200.0, 40.0, 44.0))
    assert verdict(tiny, ENROLMENT_PROFILE) == "face too small"
    assert verdict(tiny, RECOGNITION_PROFILE) == "face too small"


def test_face_filling_the_frame_is_rejected_by_recognition():
    huge = build_face(box=(10.0, 10.0, 1000.0, 700.0))
    assert verdict(huge, RECOGNITION_PROFILE) == "face area out of range"


def test_implausible_aspect_ratio_is_rejected():
    wide = build_face(box=(300.0, 200.0, 300.0, 200.0))
    for profile in ALL_PROFILES:
        assert verdict(wide, profile) == "face aspect ratio out of range"


@pytest.mark.parametrize("eye_half_width", [0.03, 0.45])
@pytest.mark.parametrize("profile", ALL_PROFILES, ids=lambda p: p.name)
def test_impossible_eye_separation_is_rejected(profile, eye_half_width):
    mesh = build_face(eye_half_width=eye_half_width)
    assert verdict(mesh, profile) == "eye separation out of range"


# Measured live on 2026-08-21: a real operator, a real 1920x1080 camera, all
# nine enrolment stages, 2,709 frames, through the production detector and the
# production canonicalisation. `eye_distance` is outer-corner to outer-corner
# over the FaceMesh box width - ENROLMENT_PROFILE's own definition.
#
# ⚠️ **The LEFT and RIGHT rows carry the right numbers under unreliable
# labels.** The probe showed an un-mirrored preview and said only "TURN
# SLIGHTLY LEFT", so the operator turned the wrong way on roughly half the LEFT
# frames and said so afterwards. Mislabelling which turn a frame was does not
# change the set of frames measured, and the ceiling is a maximum over all of
# them - set by DOWN and CLOSE, which nobody was confused about. The turned
# stages only ever probe the FLOOR, and the evidence there is that a real turn
# in either direction reaches 0.403 against a 0.18 floor.
#
# What it does cost is sampling: genuine left turns are under-represented, so
# the LEFT row is a weaker estimate than n=264 suggests. Do not read the
# per-stage minima as a left/right asymmetry. See
# tasks/audit-face-detector.md §1.5.
#
# (stage, frames, minimum, median, maximum)
MEASURED_ENROLMENT_EYE_OF_WIDTH = (
    ("STRAIGHT", 165, 0.686, 0.699, 0.718),
    ("LEFT", 264, 0.538, 0.585, 0.707),
    ("RIGHT", 326, 0.403, 0.534, 0.710),
    ("UP", 327, 0.682, 0.692, 0.716),
    ("DOWN", 326, 0.689, 0.718, 0.739),
    ("SMILE", 327, 0.691, 0.698, 0.710),
    ("CLOSE", 321, 0.697, 0.722, 0.738),
    ("MEDIUM", 326, 0.690, 0.701, 0.716),
    ("FAR", 327, 0.659, 0.673, 0.703),
)


@pytest.mark.parametrize(
    ("stage", "frames", "lowest", "median", "highest"),
    MEASURED_ENROLMENT_EYE_OF_WIDTH,
    ids=[row[0] for row in MEASURED_ENROLMENT_EYE_OF_WIDTH],
)
def test_the_eye_band_admits_every_measured_enrolment_pose(
    stage, frames, lowest, median, highest
):
    """
    ⚠️ **The regression this exists to prevent, and the defect it replaces.**

    `eye_distance_of_width` was `(0.18, 0.70)` while the quantity it bounds
    ran 0.403-0.739 with a median of 0.697. A ceiling on the median of its own
    distribution is not a gate - landmark jitter decides it - and it admitted
    **61.7%** of frames from an operator doing everything correctly. The
    refusal then surfaced as "No face detected.", which is why three UAT
    rounds went looking at the detector.

    The parametrisation is by stage on purpose: the *turned* stages are the
    ones that probe the floor, because a turn shortens the projected eye span.
    A future change that fixes the ceiling by moving the floor would pass a
    frontal-only check and fail here on LEFT and RIGHT.

    ⚠️ One subject. This asserts the band covers what has actually been
    measured; it is not a claim that 0.403-0.739 is the population.
    """
    low, high = ENROLMENT_PROFILE.eye_distance_of_width

    assert low < lowest, (
        f"{stage}: the floor {low} refuses a measured frame at {lowest} "
        f"(n={frames})"
    )
    assert high > highest, (
        f"{stage}: the ceiling {high} refuses a measured frame at {highest} "
        f"(n={frames}). This is the 0.70 defect returning."
    )
    assert not low <= median <= low + (high - low) * 0.05, (
        f"{stage}: the median {median} sits at the very bottom of the band"
    )


def test_the_eye_band_keeps_real_margin_around_the_measured_extremes():
    """
    Admitting the measured frames is not enough - 0.75 would have done that
    with 0.011 to spare, which is the same "threshold on the edge of its own
    data" shape one decimal place further out.
    """
    low, high = ENROLMENT_PROFILE.eye_distance_of_width

    highest = max(row[4] for row in MEASURED_ENROLMENT_EYE_OF_WIDTH)
    lowest = min(row[2] for row in MEASURED_ENROLMENT_EYE_OF_WIDTH)

    assert high - highest >= 0.05, (
        f"only {high - highest:.3f} of headroom above the measured maximum "
        f"{highest}; a differently-shaped face has nowhere to land"
    )
    assert lowest - low >= 0.05, (
        f"only {lowest - low:.3f} of margin below the measured minimum"
    )


@pytest.mark.parametrize("profile", ALL_PROFILES, ids=lambda p: p.name)
def test_extreme_head_roll_is_rejected(profile):
    """
    Narrow-set eyes at a steep tilt.

    Roll and eye separation are measured from the same two points, so a large
    roll on widely-spaced eyes trips the separation band first and tests the
    wrong branch. Narrowing the eyes keeps the separation legal for both
    profiles while the vertical offset alone trips the roll rule.
    """
    mesh = build_face(eye_half_width=0.14, roll=0.31)
    assert verdict(mesh, profile) == "head roll too large"


def test_eye_line_too_low_is_rejected_by_recognition():
    mesh = build_face(eye_level=0.70, nose_level=0.80, mouth_level=0.92)
    assert verdict(mesh, RECOGNITION_PROFILE) == "eye line outside the expected band"


@pytest.mark.parametrize("mouth_half_width", [0.02, 0.45])
def test_disproportionate_mouth_is_rejected_by_recognition(mouth_half_width):
    mesh = build_face(mouth_half_width=mouth_half_width)
    assert verdict(mesh, RECOGNITION_PROFILE) == "mouth width out of proportion"


@pytest.mark.parametrize("profile", ALL_PROFILES, ids=lambda p: p.name)
def test_mouth_above_the_eyes_is_rejected(profile):
    """An upside-down mesh fit, which is what this check exists to catch."""
    mesh = build_face(eye_level=0.75, mouth_level=0.25, nose_level=0.50)
    assert verdict(mesh, profile) is not None


@pytest.mark.parametrize("profile", ALL_PROFILES, ids=lambda p: p.name)
def test_nose_outside_the_face_is_rejected(profile):
    mesh = build_face(nose_offset=0.60)
    assert verdict(mesh, profile) is not None


def test_mouth_offset_from_the_eyes_is_rejected_by_recognition():
    mesh = build_face()
    # Slide only the mouth sideways; the eyes and nose stay put.
    for index in (LEFT_MOUTH, RIGHT_MOUTH, UPPER_LIP, LOWER_LIP):
        mesh.landmark[index].x += 90.0 / FRAME_WIDTH

    assert verdict(mesh, RECOGNITION_PROFILE) == "mouth is not aligned with the eyes"


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


def test_get_face_box_is_the_clamped_extent_of_every_landmark():
    # Coordinates chosen to be exactly representable after the round trip
    # through normalised floats: 400/1280, 640/1280, 180/720 and 450/720 are
    # all exact binary fractions, so no truncation noise reaches the assert.
    mesh = build_face(box=(400.0, 180.0, 240.0, 270.0))
    box = get_face_box(mesh, FRAME_WIDTH, FRAME_HEIGHT)

    assert (box.x1, box.y1, box.x2, box.y2) == (400, 180, 640, 450)
    assert (box.width, box.height) == (240, 270)
    assert box_to_xywh(box) == (400, 180, 240, 270)


def test_get_face_box_clamps_to_the_frame():
    mesh = build_face(box=(-50.0, -80.0, 200.0, 220.0))
    box = get_face_box(mesh, FRAME_WIDTH, FRAME_HEIGHT)

    assert box.x1 == 0
    assert box.y1 == 0
    assert box.x2 <= FRAME_WIDTH - 1
    assert box.y2 <= FRAME_HEIGHT - 1


def test_box_iou_of_identical_boxes_is_one():
    box = (10, 10, 110, 110)
    assert box_iou(box, box) == pytest.approx(1.0)


def test_box_iou_of_disjoint_boxes_is_zero():
    assert box_iou((0, 0, 50, 50), (500, 500, 550, 550)) == 0.0


def test_box_iou_of_half_overlapping_boxes():
    # Two 100x100 boxes offset by 50 in x: intersection 50x100 = 5000,
    # union 10000 + 10000 - 5000 = 15000.
    assert box_iou((0, 0, 100, 100), (50, 0, 150, 100)) == pytest.approx(1 / 3)


def test_box_center_and_area():
    box = (10, 20, 110, 220)
    assert box_center(box) == (60.0, 120.0)
    assert box_area(box) == 100 * 200


def test_box_area_of_an_inverted_box_is_zero():
    assert box_area((100, 100, 10, 10)) == 0


# ---------------------------------------------------------------------------
# The duplicates must not come back
# ---------------------------------------------------------------------------

GATE_FUNCTIONS = ("is_valid_face_candidate", "get_face_box")

# ⚠️ These three used to name `recognize_face.py` and `capture_dataset.py`
# explicitly, because those were the two files that carried the divergent
# copies. Phase 5 deleted the second one, which would have left a two-module
# ban covering one module - and nothing at all covering the files this phase
# adds. They now sweep every first-party module except the owner, so a
# reintroduced copy is caught wherever somebody puts it.
#
# The two gate functions have two owners, not one: `get_face_box` is the
# geometry primitive and lives in vision/geometry.py, `is_valid_face_candidate`
# is the decision built on it and lives in vision/validation.py.
GATE_OWNERS = {
    "get_face_box": "geometry.py",
    "is_valid_face_candidate": "validation.py",
}


def _modules_that_must_not_own_the_gate():
    owners = set(GATE_OWNERS.values())
    return [path for path in project_python_files() if path.name not in owners]


@pytest.mark.parametrize(
    "path",
    _modules_that_must_not_own_the_gate(),
    ids=lambda path: path.name,
)
def test_no_module_redefines_the_gate(path):
    """
    Only vision/validation.py may define the geometry gate.

    This is what keeps MA-4 closed. A local `def is_valid_face_candidate`
    would shadow the import and silently restore the divergence, exactly as
    lessons.md L5 describes for `settings`.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    defined = {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in GATE_FUNCTIONS
    }

    assert not defined, (
        f"{path.name} defines {sorted(defined)} locally. The face-geometry "
        f"gate lives in vision/validation.py (MA-4); import it instead."
    )


@pytest.mark.parametrize("module_name", ["recognize_face.py"])
def test_modules_import_the_shared_gate(module_name):
    source = (PROJECT_ROOT / module_name).read_text(encoding="utf-8")
    assert "vision.validation" in source, (
        f"{module_name} must import the shared gate from vision.validation."
    )


def test_landmark_indices_are_written_down_once():
    """
    A MediaPipe mesh index may only appear in vision/landmarks.py.

    Two modules used to carry their own block of these numbers. They matched
    by luck, and a mesh index typed twice is a mesh index that can drift once.
    """
    for module_name in ("recognize_face.py",):
        path = PROJECT_ROOT / module_name
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

        offenders = [
            target.id
            for node in tree.body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
            and target.id
            in {
                "NOSE_TIP",
                "LEFT_EYE_OUTER",
                "LEFT_EYE_INNER",
                "LEFT_EYE_UPPER",
                "LEFT_EYE_LOWER",
                "RIGHT_EYE_INNER",
                "RIGHT_EYE_OUTER",
                "RIGHT_EYE_UPPER",
                "RIGHT_EYE_LOWER",
                "LEFT_MOUTH",
                "RIGHT_MOUTH",
                "UPPER_LIP",
                "LOWER_LIP",
            }
            and isinstance(node.value, ast.Constant)
        ]

        assert not offenders, (
            f"{module_name} redefines mesh indices {sorted(offenders)} as "
            f"literals. Import them from vision.landmarks."
        )
