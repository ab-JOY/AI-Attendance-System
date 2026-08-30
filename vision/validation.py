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
    # ⚠️ **The ceiling was 0.70 until 2026-08-21, and it sat on the median of
    # its own distribution.** Measured live over 2,709 frames of a real
    # operator holding all nine enrolment stages in front of a real camera:
    #
    #   stage      n    eye/width  min     p50     max    over 0.70
    #   STRAIGHT  165              0.686   0.699   0.718     39%
    #   LEFT      264              0.538   0.585   0.707      2%
    #   RIGHT     326              0.403   0.534   0.710      1%
    #   UP        327              0.682   0.692   0.716     13%
    #   DOWN      326              0.689   0.718   0.739     95%
    #   SMILE     327              0.691   0.698   0.710     32%
    #   CLOSE     321              0.697   0.722   0.738     98%
    #   MEDIUM    326              0.690   0.701   0.716     59%
    #   FAR       327              0.659   0.673   0.703      0%
    #   ALL      2709              0.403   0.697   0.739
    #
    # A ceiling of 0.70 admitted **61.7%** of frames from a person doing
    # everything right, and which side of it a frame landed on was decided by
    # landmark jitter - so enrolment presented as intermittent rather than
    # broken, and the refusal was reported as "No face detected."
    #
    # This is expected geometry, not an unusual face: the mesh covers the face
    # oval and not the ears, so the OUTER eye corners span about 0.70 of it.
    # The measured maximum is 0.739; 0.80 clears it by ~8% so a different face
    # shape has somewhere to land, while still refusing a mesh fit whose
    # "eyes" are most of the box. 0.75 would also admit 100% of the frames
    # above and was rejected for having only 0.011 of margin.
    #
    # ⚠️ **The floor was never the problem and has not moved.** The turned
    # stages are what probe it - a turn shortens the projected eye span - and
    # LEFT/RIGHT bottom out at 0.403 against a 0.18 floor. One subject; see
    # tasks/audit-face-detector.md §3 for what that does and does not license.
    eye_distance_of_width=(0.18, 0.80),
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


def structure_rejection_reason(
    face_landmarks: LandmarkList,
    frame_width: int,
    frame_height: int,
    box: FaceBox,
    profile: FaceGeometryProfile,
) -> str | None:
    """
    Why `profile` refuses this as **a face**, ignoring which way it is facing.

    Every check except the two frontality ones, which are
    `frontality_reason()`. Splitting them is what lets recognition keep
    *tracking* a head that has turned - see that function's docstring for the
    defect this closes.

    Enrolment is the evidence that the split is sound: `ENROLMENT_PROFILE`
    applies none of the frontality checks and still identifies faces reliably
    enough to collect 50 deliberately off-axis images per student. What is
    left here is sufficient to answer "is this a face".
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

    # Only the y coordinate is structural. The eye centre's x is what
    # frontality measures the nose against, and that lives in
    # frontality_reason() now.
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

    return None


def frontality_reason(
    face_landmarks: LandmarkList,
    frame_width: int,
    frame_height: int,
    box: FaceBox,
    profile: FaceGeometryProfile,
) -> str | None:
    """
    Why this face is not frontal enough for `profile`, or None if it is.

    ⚠️ **This used to be the last two checks of `rejection_reason()`, and
    living there is what made the liveness challenge unpassable.**

    `recognize_face._stream_frames()` calls the geometry gate *before*
    `tracker.assign()`, and a rejected frame hits a bare `continue`. So while
    these two checks decided whether a face was **seen at all**, a student
    obeying "Turn LEFT" was dropped the moment their nose passed
    0.28 x face-width: no box drawn, no liveness progress, no mismatch
    counted, and - the part that actually breaks it - **no refresh of the
    track's `last_seen`**, so `expire_old_tracks()` deleted the track 1.5 s
    later. The student turned back to find themselves at "Verifying 0/20"
    again, re-confirmed, drew a fresh random challenge, and repeated.

    Measured: the challenge demands >= 0.10 face-widths of turn
    (`LivenessConfig.turn_of_face_width`) while this ceiling sits at about
    0.32 in the same units - the two measure yaw from different landmarks, and
    the gate reads about 0.87x the liveness value. That is a real window, but
    the student is told only "Turn LEFT" with no upper bound and no feedback,
    and enrolment - which says "TURN **SLIGHTLY** LEFT" and produced the
    training data - already reaches 80% of this ceiling.

    So frontality moved from "may I see you" to **"may I decide about you"**.
    It now gates confirmation and nothing else: an attendance mark still
    requires a frontal face, which is the property `RECOGNITION_PROFILE`'s
    comment describes and the one worth keeping. Tracking, drawing and the
    liveness challenge no longer apply it.
    """
    x1, _y1, x2, _y2 = box
    face_width = x2 - x1

    if (
        profile.max_nose_offset_of_width is None
        and profile.max_mouth_offset_of_width is None
    ):
        return None

    left_eye = average_landmark_pixel(
        face_landmarks, profile.left_eye_landmarks, frame_width, frame_height
    )
    right_eye = average_landmark_pixel(
        face_landmarks, profile.right_eye_landmarks, frame_width, frame_height
    )

    eye_center_x = (left_eye[0] + right_eye[0]) / 2.0

    nose = landmark_pixel(face_landmarks, NOSE_TIP, frame_width, frame_height)

    if (
        profile.max_nose_offset_of_width is not None
        and abs(nose[0] - eye_center_x) > face_width * profile.max_nose_offset_of_width
    ):
        return "face is not frontal enough"

    if profile.max_mouth_offset_of_width is not None:
        left_mouth = landmark_pixel(
            face_landmarks, LEFT_MOUTH, frame_width, frame_height
        )
        right_mouth = landmark_pixel(
            face_landmarks, RIGHT_MOUTH, frame_width, frame_height
        )
        mouth_center_x = (left_mouth[0] + right_mouth[0]) / 2.0

        if (
            abs(mouth_center_x - eye_center_x)
            > face_width * profile.max_mouth_offset_of_width
        ):
            return "mouth is not aligned with the eyes"

    return None


def rejection_reason(
    face_landmarks: LandmarkList,
    frame_width: int,
    frame_height: int,
    box: FaceBox,
    profile: FaceGeometryProfile,
) -> str | None:
    """
    Why `profile` refuses this face, or `None` if it accepts it.

    Structure first, then frontality - the same sequence of checks, returning
    the same reasons, that this function ran before the two were separated.
    Kept whole because "is this a face I am willing to act on?" is still a
    question worth asking in one call, and because every existing caller and
    test means exactly that by it.

    `is_valid_face_candidate()` is this function's boolean face. Keeping the
    reason available costs nothing and turns "the camera will not pick me up"
    into something diagnosable at DEBUG level.
    """
    return structure_rejection_reason(
        face_landmarks, frame_width, frame_height, box, profile
    ) or frontality_reason(
        face_landmarks, frame_width, frame_height, box, profile
    )


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


def is_structurally_a_face(
    face_landmarks: LandmarkList,
    frame_width: int,
    frame_height: int,
    box: FaceBox,
    profile: FaceGeometryProfile,
) -> bool:
    """True if this is a face, whichever way it is turned."""
    return (
        structure_rejection_reason(
            face_landmarks, frame_width, frame_height, box, profile
        )
        is None
    )


def is_frontal(
    face_landmarks: LandmarkList,
    frame_width: int,
    frame_height: int,
    box: FaceBox,
    profile: FaceGeometryProfile,
) -> bool:
    """True if this face is square-on enough for `profile` to decide about."""
    return (
        frontality_reason(face_landmarks, frame_width, frame_height, box, profile)
        is None
    )
