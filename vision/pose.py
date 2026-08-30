"""
Head pose, expression and framing gates for enrolment capture.

Lifted out of `capture_dataset.py` unchanged, so that browser-side enrolment
judges a frame exactly as the OpenCV capture window did. Like the rest of
`vision/`, nothing here imports MediaPipe, OpenCV or a camera: landmarks arrive
as the `LandmarkList` protocol and everything else is arithmetic. That is what
makes these testable in milliseconds, and it is why the differential test in
`tests/test_vision_pose.py` can drive both this and the pre-extraction original
over tens of thousands of synthetic meshes.

⚠️ **These thresholds are only meaningful at a fixed frame size, and two of
them are absolute pixels.** `CENTER_TOLERANCE` is 180 *pixels*, and the face
ratios below were tuned against the 1920x1080 that `capture_dataset.py` forced
on the camera (`CAP_PROP_FRAME_WIDTH`/`HEIGHT`). Nothing scales them.

A browser does not have a fixed frame size. The camera on the operator's laptop
may deliver 640x480, 1280x720 or 1920x1080 depending on the device and on what
the page asked for, and at 640x480 a `CENTER_TOLERANCE` of 180 px is a band
three times wider, in proportion, than the one these numbers were chosen for.

So the contract is: **the server resizes every uploaded frame to
`CANONICAL_FRAME_WIDTH` x `CANONICAL_FRAME_HEIGHT` before any of this runs.**
That is what keeps this extraction behaviour-preserving rather than a rewrite
wearing a refactor's clothes, and `vision/enrolment.py` is where it is enforced.

⚠️ **The pose thresholds were frame-normalised until 2026-08-21, and it made
"LOOK STRAIGHT" almost unreachable.** The paragraph that used to sit here
flagged the hazard, argued it was contained, and deferred the fix. It was not
contained. The argument was that "the enrolment stages *also* constrain
distance - CLOSE/MEDIUM/FAR pin the face ratio into narrow bands, and
`good_distance()` bounds it everywhere else". Only the first half is true, and
it covers **15 of the 100 images**. The five POSE stages - 75 images - pin
nothing, and `good_distance()` admits a face-area ratio from 0.012 to 0.55, a
**45x** range.

What that produced, measured over the 300 real enrolment images composited at a
range of sizes (`get_head_pose` returning the *frontal* subjects' verdict):

| face box | area ratio | verdict on a face looking straight ahead |
|---:|---:|---|
| 400 px | 0.05 | **UP** (15/15) |
| 500 px | 0.08 | STRAIGHT 13, UP 11 |
| 560 px | 0.10 | STRAIGHT 15, DOWN 8, UP 1 |
| 600 px | 0.12 | **DOWN** 22, STRAIGHT 2 |
| 800 px | 0.21 | **DOWN** 23, RIGHT 1 |
| 1050 px | 0.36 | **DOWN** 22, RIGHT 2 |

`pitch` is `nose.y - eye_centre.y` as a fraction of the *frame*, so it grows
with the face. STRAIGHT required it to land between `PITCH_UP` (0.095) and
`PITCH_DOWN` (0.110) - **a window 0.015 wide** - which is not a statement about
a head at all, it is a statement about standing at one particular distance.
Step back and you are UP; step forward and you are DOWN. A test even pinned
that window's width as though it were a feature.

**Every threshold below is now a fraction of face width or face height**, the
same units `vision/geometry.py` and `vision/liveness.py` work in, and
`get_head_pose()` requires the face box so it can use them. Derived from the
enrolment set rather than chosen - see the table beside each constant.
"""

from __future__ import annotations

from vision.geometry import LandmarkList
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

# =====================================================
# CANONICAL FRAME
#
# The resolution capture_dataset.py requested from the camera, and therefore
# the resolution every threshold below was tuned at. Uploaded frames are
# resized to this before they are judged - see the module docstring.
# =====================================================

CANONICAL_FRAME_WIDTH = 1920
CANONICAL_FRAME_HEIGHT = 1080

# Below this native width, upscaling to the canonical frame costs enough detail
# that ENROLMENT_QUALITY's blur floor starts rejecting perfectly good captures.
# Refusing up front with a clear reason beats an unexplained "Image is blurry."
MIN_NATIVE_FRAME_WIDTH = 960


# =====================================================
# FRAMING
# =====================================================

# The face must occupy between these fractions of the frame at every stage.
MIN_FACE_RATIO = 0.012
MAX_FACE_RATIO = 0.55

# The three distance stages narrow that band further. These are what make
# CLOSE, MEDIUM and FAR mean anything: without them the subject could stand
# still through all fifteen images.
CLOSE_MIN_RATIO = 0.18
CLOSE_MAX_RATIO = 0.32

MEDIUM_MIN_RATIO = 0.07
MEDIUM_MAX_RATIO = 0.17

FAR_MIN_RATIO = 0.012
FAR_MAX_RATIO = 0.075

DISTANCE_BANDS = {
    "CLOSE": (CLOSE_MIN_RATIO, CLOSE_MAX_RATIO),
    "MEDIUM": (MEDIUM_MIN_RATIO, MEDIUM_MAX_RATIO),
    "FAR": (FAR_MIN_RATIO, FAR_MAX_RATIO),
}

# Pixels, at the canonical frame size. See the docstring.
CENTER_TOLERANCE = 180


# =====================================================
# POSE THRESHOLDS
# =====================================================

# =====================================================
# Derived from the 300 enrolment images, not chosen. Every value below is a
# fraction of FACE WIDTH (yaw) or FACE HEIGHT (pitch) - a property of the head
# rather than of how far away it is. Measured per stage:
#
#   stage      n      yaw / face width        pitch / face height
#                  min     med     max      min     med     max
#   STRAIGHT  75  -0.053  -0.017   0.028    0.202   0.233   0.257
#   LEFT      45   0.096   0.137   0.170    0.196   0.227   0.264
#   RIGHT     45  -0.202  -0.140  -0.094    0.165   0.223   0.257
#   UP        30  -0.053  -0.003   0.032    0.080   0.117   0.197
#   DOWN      30  -0.077  -0.022   0.000    0.234   0.261   0.354
#   CLOSE/MED/FAR 45  -0.076  -0.008  0.045  0.179   0.230   0.297
#
# ⚠️ Three subjects. Phase 6 is where these get a proper calibration; what
# they have now is a derivation from every image this project holds, which is
# strictly more than the frame-normalised values ever had.
# =====================================================

# A turn. The frontal stages reach |0.076| and the turned stages start at
# |0.094|, so the boundary sits in a real - if narrow - gap between them.
STRAIGHT_MAX_YAW_OF_WIDTH = 0.085

# Below this the head is tipped back (UP); above it, tipped forward (DOWN).
# UP tops out at 0.197 and STRAIGHT starts at 0.202, so the UP boundary is
# clean.
PITCH_UP_OF_HEIGHT = 0.200

# ⚠️ **DOWN and STRAIGHT genuinely overlap and no threshold separates them.**
# STRAIGHT runs to 0.257 and DOWN starts at 0.234, because the instruction is
# "look SLIGHTLY down" and these subjects obliged. The line is placed just
# above STRAIGHT's maximum rather than at the midpoint, so that **every one of
# the 75 STRAIGHT images is classified STRAIGHT** - that stage is the reported
# defect, it is the first thing a subject does, and no later stage is reachable
# until it passes. The cost is that DOWN wants a slightly more definite
# movement than this dataset recorded; the subject is told so on screen.
PITCH_DOWN_OF_HEIGHT = 0.262

# The distance stages want a roughly frontal face and judge it more loosely
# than the LEFT/RIGHT stages judge a turn. Wide enough to admit every measured
# CLOSE/MEDIUM/FAR frame (yaw to |0.076|, pitch 0.179-0.297) with margin, while
# still refusing a real turn.
DISTANCE_MAX_YAW_OF_WIDTH = 0.13
DISTANCE_MIN_PITCH_OF_HEIGHT = 0.12
DISTANCE_MAX_PITCH_OF_HEIGHT = 0.38

# Mouth width over mouth opening, plus how far the corners sit below the
# cheeks. A wide, closed, lifted mouth is a smile; a wide open one is speech.
SMILE_MIN_SCORE = 4.8
SMILE_MAX_LIFT = 0.34


def calculate_face_ratio(
    frame_width: int,
    frame_height: int,
    width: int,
    height: int,
) -> float:
    """The fraction of the frame's area the face box covers."""
    return (width * height) / (frame_width * frame_height)


def good_distance(
    frame_width: int,
    frame_height: int,
    width: int,
    height: int,
) -> bool:
    """True if the face is neither too far away nor filling the frame."""
    face_ratio = calculate_face_ratio(frame_width, frame_height, width, height)

    return MIN_FACE_RATIO <= face_ratio <= MAX_FACE_RATIO


def face_centered(
    frame_width: int,
    frame_height: int,
    x: int,
    y: int,
    width: int,
    height: int,
) -> bool:
    """
    True if the face box's centre is within CENTER_TOLERANCE of the frame's.

    Integer division on the centres, deliberately: it is what the original
    did, and a half-pixel here decides nothing.
    """
    face_center_x = x + width // 2
    face_center_y = y + height // 2

    frame_center_x = frame_width // 2
    frame_center_y = frame_height // 2

    return (
        abs(face_center_x - frame_center_x) <= CENTER_TOLERANCE
        and abs(face_center_y - frame_center_y) <= CENTER_TOLERANCE
    )


def distance_band(stage: str) -> tuple[float, float]:
    """
    The (minimum, maximum) face ratio for a distance stage.

    Raises KeyError for any other stage, which is the right answer: asking for
    the distance band of SMILE is a programming error, not a runtime condition.
    """
    return DISTANCE_BANDS[stage]


def head_is_roughly_straight(yaw: float, pitch: float) -> bool:
    """
    True if the head is frontal enough for a distance-stage capture.

    The distance stages are about how far away the subject is standing, so a
    turned head there is contamination rather than a second pose: those images
    are meant to differ from the frontal ones in scale only.
    """
    return not (
        abs(yaw) > DISTANCE_MAX_YAW_OF_WIDTH
        or pitch > DISTANCE_MAX_PITCH_OF_HEIGHT
        or pitch < DISTANCE_MIN_PITCH_OF_HEIGHT
    )


def get_head_pose(
    face_landmarks: LandmarkList,
    frame_width: int,
    frame_height: int,
    face_width: float,
    face_height: float,
) -> tuple[str, float, float]:
    """
    Classify the head as STRAIGHT, LEFT, RIGHT, UP or DOWN.

    Returns the pose with the yaw and pitch it was decided from, because the
    distance stages need the raw numbers and the debug overlay shows them.

    `yaw` is the nose's horizontal offset from the midpoint of the eyes as a
    fraction of **face width**; `pitch` its vertical offset as a fraction of
    **face height**.

    ⚠️ **The face box is required, and that is the fix rather than a tidy-up.**
    These were fractions of the *frame* until 2026-08-21, which made every
    answer a function of how far away the subject stood: a face looking
    straight ahead was classified UP at 400 px, STRAIGHT only around 500-560 px,
    and DOWN from 600 px upward. See the module docstring for the measured
    table. Passing the box is what makes the frame-relative version
    unrepresentable.

    Yaw is tested before pitch, unchanged: a turned head is a turned head
    whatever it is doing vertically.
    """
    nose = face_landmarks.landmark[NOSE_TIP]
    left_eye = face_landmarks.landmark[LEFT_EYE_OUTER]
    right_eye = face_landmarks.landmark[RIGHT_EYE_OUTER]

    eye_center_x = (left_eye.x + right_eye.x) / 2
    eye_center_y = (left_eye.y + right_eye.y) / 2

    # Landmarks are frame-normalised, so multiply back up to pixels before
    # dividing by the face box - otherwise this is the bug it replaces.
    yaw = (nose.x - eye_center_x) * frame_width / max(face_width, 1)
    pitch = (nose.y - eye_center_y) * frame_height / max(face_height, 1)

    if yaw > STRAIGHT_MAX_YAW_OF_WIDTH:
        return "LEFT", yaw, pitch

    if yaw < -STRAIGHT_MAX_YAW_OF_WIDTH:
        return "RIGHT", yaw, pitch

    if pitch >= PITCH_DOWN_OF_HEIGHT:
        return "DOWN", yaw, pitch

    if pitch <= PITCH_UP_OF_HEIGHT:
        return "UP", yaw, pitch

    return "STRAIGHT", yaw, pitch


def detect_expression(face_landmarks: LandmarkList) -> str:
    """
    "SMILE" or "NEUTRAL".

    Two conditions, and both are load-bearing. A high width-to-opening ratio
    alone is satisfied by a closed neutral mouth, so the corners must also sit
    high relative to the cheeks. An open mouth drives the ratio down, which is
    what keeps speech out.
    """
    left_corner = face_landmarks.landmark[LEFT_MOUTH]
    right_corner = face_landmarks.landmark[RIGHT_MOUTH]
    upper_lip = face_landmarks.landmark[UPPER_LIP]
    lower_lip = face_landmarks.landmark[LOWER_LIP]
    left_cheek = face_landmarks.landmark[LEFT_UPPER_CHEEK]
    right_cheek = face_landmarks.landmark[RIGHT_UPPER_CHEEK]

    mouth_width = abs(right_corner.x - left_corner.x)
    mouth_open = abs(lower_lip.y - upper_lip.y)

    left_lift = left_corner.y - left_cheek.y
    right_lift = right_corner.y - right_cheek.y

    smile_lift = (left_lift + right_lift) / 2

    # The epsilon stops a perfectly closed mouth dividing by zero. It is the
    # original's, kept rather than tidied: removing it would change the score
    # for every frame, not just the degenerate one.
    smile_score = mouth_width / (mouth_open + 0.001)

    if smile_score > SMILE_MIN_SCORE and smile_lift < SMILE_MAX_LIFT:
        return "SMILE"

    return "NEUTRAL"
