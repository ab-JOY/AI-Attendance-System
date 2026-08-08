"""
The single face-geometry gate (MA-4).

MA-4 found `is_valid_face_candidate()` implemented twice, in
`recognize_face.py` and `capture_dataset.py`, with different thresholds. The
audit called it "a silent accuracy leak", and the leak was real: nothing in
the codebase recorded that enrolment and recognition judged faces differently,
so the divergence could only be found by reading both files side by side.

**Why this module has two profiles rather than one threshold set.**

The obvious reading of MA-4 is "pick one set of numbers and delete the other".
That is wrong here, and the reason is in `capture_dataset.py`: 50 of the 100
images captured per student are deliberately off-axis - `LEFT_IMAGES = 15`,
`RIGHT_IMAGES = 15`, `UP_IMAGES = 10`, `DOWN_IMAGES = 10`. The recognition
gate rejects a face whose nose sits more than 0.28 x face-width from the eye
centre, because an attendance mark is a decision and a decision wants a
frontal face. Applying that to enrolment would reject the very poses
enrolment exists to collect.

So the two stages genuinely want different tolerances. What they must not
have is two implementations. Every check below runs once, for both callers;
the profile supplies the numbers, and `None` means "this profile does not
apply this check". The divergence is now a dozen lines of declared,
documented data in one file instead of two functions 1,400 lines apart that
happen not to match.

**One intentional numerical difference.** The enrolment original truncated
pixel coordinates and band edges to `int` (`x + int(width * 0.18)`); this
module works in floats throughout, as the recognition original did. The
effect is under one pixel on bands tens of pixels wide, and can only change
an outcome for a face sitting exactly on a boundary - where frame-to-frame
landmark jitter already decides it.
"""

from __future__ import annotations

from dataclasses import dataclass

from vision.geometry import (
    FaceBox,
    LandmarkList,
    average_landmark_pixel,
    landmark_pixel,
)
from vision.landmarks import (
    LEFT_EYE_QUAD,
    LEFT_EYE_SINGLE,
    LEFT_MOUTH,
    LOWER_LIP,
    NOSE_TIP,
    RIGHT_EYE_QUAD,
    RIGHT_EYE_SINGLE,
    RIGHT_MOUTH,
    UPPER_LIP,
)


@dataclass(frozen=True)
class FaceGeometryProfile:
    """
    The tolerances one stage of the pipeline applies to a candidate face.

    Every `... | None` field is a check that one profile applies and the other
    does not. That asymmetry is the finding MA-4 raised, written down.
    """

    name: str

    # --- size ---------------------------------------------------------
    min_face_width: int
    min_face_height: int

    # Face box area as a fraction of frame area. Recognition uses this to
    # reject both distant noise and a face pressed against the lens;
    # enrolment governs distance with its own CLOSE/MEDIUM/FAR stages
    # instead, so it does not apply the check.
    face_area_ratio: tuple[float, float] | None

    aspect_ratio: tuple[float, float]

    # --- eyes ---------------------------------------------------------
    # Which landmarks define an eye centre. Recognition averages four points
    # per eye; enrolment uses the outer corner alone. Outer-corner separation
    # is systematically wider than four-point-centre separation, so the two
    # eye-distance bands below are NOT directly comparable - 0.18 of face
    # width measured corner-to-corner is a narrower face than 0.18 measured
    # centre-to-centre.
    left_eye_landmarks: tuple[int, ...]
    right_eye_landmarks: tuple[int, ...]

    eye_distance_of_width: tuple[float, float]
    min_eye_distance_px: float

    # Head roll, expressed against two different denominators by the two
    # originals. Exactly one of these is set per profile.
    max_eye_roll_of_eye_distance: float | None
    max_eye_roll_of_face_height: float | None

    # Where the eye line may sit inside the face box. Recognition only.
    eye_level_band_of_box: tuple[float, float] | None

    # --- mouth --------------------------------------------------------
    # Mouth width relative to eye separation - a proportion check that
    # rejects mesh fits on non-faces. Recognition only.
    mouth_width_of_eye_distance: tuple[float, float] | None

    # The mouth must sit at least this far below the eye line.
    min_mouth_drop_of_height: float

    # --- nose ---------------------------------------------------------
    # The nose must sit between the eye line and the mouth, with slack.
    nose_above_eyes_of_height: float
    nose_below_mouth_of_height: float

    # Box-relative nose bands (enrolment) versus offset from the eye centre
    # (recognition). These express the same intent - "the nose is roughly
    # central" - at very different strictness, which is exactly why enrolment
    # can collect a turned head and recognition cannot.
    nose_x_band_of_box: tuple[float, float] | None
    nose_y_band_of_box: tuple[float, float] | None
    max_nose_offset_of_width: float | None

    max_mouth_offset_of_width: float | None


# ---------------------------------------------------------------------------
# The two profiles.
#
# Both sets of numbers are transcribed unchanged from the originals they
# replace, so this extraction is behaviour-preserving. Neither set has ever
# been calibrated against measured data - they are the "safe starting values"
# MA-6 flagged. Phase 6 is where they get derived rather than chosen.
# ---------------------------------------------------------------------------

RECOGNITION_PROFILE = FaceGeometryProfile(
    name="recognition",
    # Was recognize_face.MIN_FACE_WIDTH / MIN_FACE_HEIGHT.
    min_face_width=70,
    min_face_height=70,
    face_area_ratio=(0.010, 0.50),
    aspect_ratio=(0.55, 1.20),
    left_eye_landmarks=LEFT_EYE_QUAD,
    right_eye_landmarks=RIGHT_EYE_QUAD,
    eye_distance_of_width=(0.17, 0.68),
    min_eye_distance_px=0.0,
    max_eye_roll_of_eye_distance=0.55,
    max_eye_roll_of_face_height=None,
    eye_level_band_of_box=(0.15, 0.60),
    mouth_width_of_eye_distance=(0.35, 1.80),
    min_mouth_drop_of_height=0.10,
    nose_above_eyes_of_height=0.05,
    nose_below_mouth_of_height=0.08,
    nose_x_band_of_box=None,
    nose_y_band_of_box=None,
    # Frontal-only. This is the check that makes the profiles necessary.
    max_nose_offset_of_width=0.28,
    max_mouth_offset_of_width=0.28,
)


ENROLMENT_PROFILE = FaceGeometryProfile(
    name="enrolment",
    # Was capture_dataset.MIN_FACE_WIDTH / MIN_FACE_HEIGHT. Lower than
    # recognition's because the FAR distance stage collects faces down to
    # 1.2% of frame area on purpose.
    min_face_width=45,
    min_face_height=45,
    face_area_ratio=None,
    aspect_ratio=(0.55, 1.15),
    left_eye_landmarks=LEFT_EYE_SINGLE,
    right_eye_landmarks=RIGHT_EYE_SINGLE,
    eye_distance_of_width=(0.18, 0.70),
    min_eye_distance_px=8.0,
    max_eye_roll_of_eye_distance=None,
    max_eye_roll_of_face_height=0.25,
    eye_level_band_of_box=None,
    mouth_width_of_eye_distance=None,
    min_mouth_drop_of_height=0.08,
    nose_above_eyes_of_height=0.08,
    nose_below_mouth_of_height=0.08,
    # Pose-tolerant: the LEFT/RIGHT/UP/DOWN capture stages are 50 of the 100
    # images per student, and they cannot be collected under recognition's
    # nose-offset rule.
    nose_x_band_of_box=(0.18, 0.82),
    nose_y_band_of_box=(0.15, 0.88),
    max_nose_offset_of_width=None,
    max_mouth_offset_of_width=None,
)


def rejection_reason(
    face_landmarks: LandmarkList,
    frame_width: int,
    frame_height: int,
    box: FaceBox,
    profile: FaceGeometryProfile,
) -> str | None:
    """
    Why `profile` refuses this face, or `None` if it accepts it.

    `is_valid_face_candidate()` is this function's boolean face. Keeping the
    reason available costs nothing and turns "the camera will not pick me up"
    into something diagnosable at DEBUG level.
    """
    x1, y1, x2, y2 = box
    face_width = x2 - x1
    face_height = y2 - y1

    if face_width < profile.min_face_width or face_height < profile.min_face_height:
        return "face too small"

    if profile.face_area_ratio is not None:
        frame_area = max(frame_width * frame_height, 1)
        area_ratio = (face_width * face_height) / frame_area
        low, high = profile.face_area_ratio

        if not low <= area_ratio <= high:
            return "face area out of range"

    aspect_ratio = face_width / float(face_height)
    low, high = profile.aspect_ratio

    if not low <= aspect_ratio <= high:
        return "face aspect ratio out of range"

    left_eye = average_landmark_pixel(
        face_landmarks, profile.left_eye_landmarks, frame_width, frame_height
    )
    right_eye = average_landmark_pixel(
        face_landmarks, profile.right_eye_landmarks, frame_width, frame_height
    )

    # Normalise so "left" is genuinely the leftmost. Every use below is
    # sign-independent, so this cannot change an outcome; it is here so the
    # names mean what they say.
    if left_eye[0] > right_eye[0]:
        left_eye, right_eye = right_eye, left_eye

    eye_difference_x = right_eye[0] - left_eye[0]
    eye_difference_y = right_eye[1] - left_eye[1]
    eye_distance = (eye_difference_x**2 + eye_difference_y**2) ** 0.5

    low, high = profile.eye_distance_of_width
    minimum_eye_distance = max(profile.min_eye_distance_px, face_width * low)
    maximum_eye_distance = face_width * high

    if not minimum_eye_distance <= eye_distance <= maximum_eye_distance:
        return "eye separation out of range"

    roll = abs(eye_difference_y)

    if (
        profile.max_eye_roll_of_eye_distance is not None
        and roll > eye_distance * profile.max_eye_roll_of_eye_distance
    ):
        return "head roll too large"

    if (
        profile.max_eye_roll_of_face_height is not None
        and roll > face_height * profile.max_eye_roll_of_face_height
    ):
        return "head roll too large"

    eye_center_x = (left_eye[0] + right_eye[0]) / 2.0
    eye_center_y = (left_eye[1] + right_eye[1]) / 2.0

    if profile.eye_level_band_of_box is not None:
        low, high = profile.eye_level_band_of_box

        if not y1 + face_height * low <= eye_center_y <= y1 + face_height * high:
            return "eye line outside the expected band"

    nose = landmark_pixel(face_landmarks, NOSE_TIP, frame_width, frame_height)
    left_mouth = landmark_pixel(face_landmarks, LEFT_MOUTH, frame_width, frame_height)
    right_mouth = landmark_pixel(face_landmarks, RIGHT_MOUTH, frame_width, frame_height)
    upper_lip = landmark_pixel(face_landmarks, UPPER_LIP, frame_width, frame_height)
    lower_lip = landmark_pixel(face_landmarks, LOWER_LIP, frame_width, frame_height)

    mouth_center_x = (left_mouth[0] + right_mouth[0]) / 2.0
    mouth_center_y = (upper_lip[1] + lower_lip[1]) / 2.0
    mouth_width = abs(right_mouth[0] - left_mouth[0])

    if profile.mouth_width_of_eye_distance is not None:
        low, high = profile.mouth_width_of_eye_distance

        if not eye_distance * low <= mouth_width <= eye_distance * high:
            return "mouth width out of proportion"

    if profile.nose_x_band_of_box is not None:
        low, high = profile.nose_x_band_of_box

        if not x1 + face_width * low <= nose[0] <= x1 + face_width * high:
            return "nose outside the horizontal band"

    if profile.nose_y_band_of_box is not None:
        low, high = profile.nose_y_band_of_box

        if not y1 + face_height * low <= nose[1] <= y1 + face_height * high:
            return "nose outside the vertical band"

    if mouth_center_y <= eye_center_y + face_height * profile.min_mouth_drop_of_height:
        return "mouth is not below the eyes"

    above = eye_center_y - face_height * profile.nose_above_eyes_of_height
    below = mouth_center_y + face_height * profile.nose_below_mouth_of_height

    if not above <= nose[1] <= below:
        return "nose is not between the eyes and the mouth"

    if (
        profile.max_nose_offset_of_width is not None
        and abs(nose[0] - eye_center_x) > face_width * profile.max_nose_offset_of_width
    ):
        return "face is not frontal enough"

    if (
        profile.max_mouth_offset_of_width is not None
        and abs(mouth_center_x - eye_center_x)
        > face_width * profile.max_mouth_offset_of_width
    ):
        return "mouth is not aligned with the eyes"

    return None


def is_valid_face_candidate(
    face_landmarks: LandmarkList,
    frame_width: int,
    frame_height: int,
    box: FaceBox,
    profile: FaceGeometryProfile,
) -> bool:
    """True if `profile` accepts this face as a real, usable face."""
    return (
        rejection_reason(face_landmarks, frame_width, frame_height, box, profile)
        is None
    )
