import logging
from datetime import datetime

import cv2
import mysql.connector

from camera_utils import open_best_camera
from config.settings import settings
from face_preprocessing import align_face, preprocess_for_lbph
from infra.camera import CameraReader
from infra.db import db_cursor
from vision.geometry import get_face_box
from vision.landmarks import LEFT_EYE_OUTER, NOSE_TIP, RIGHT_EYE_OUTER
from vision.liveness import LivenessChallenge, LivenessConfig
from vision.quality import RECOGNITION_QUALITY, quality_issue
from vision.session import (
    RecognitionSession,
    SessionBusy,
    SessionHooks,
    SessionNotRunning,
)
from vision.tracking import TrackConfig
from vision.validation import RECOGNITION_PROFILE, is_valid_face_candidate

logger = logging.getLogger(__name__)

# Re-exported so app.py can catch them without importing vision.session
# directly. `generate_frames` is the only thing that raises them.
__all__ = [
    "SessionBusy",
    "SessionNotRunning",
    "generate_frames",
    "open_video_stream",
    "session",
    "start_camera",
    "stop_camera",
]


# =====================================================
# PATHS
#
# Resolved from config/settings.py rather than derived from __file__, so a
# deployment can relocate the biometric templates without editing source
# (PO-2). Kept as str because cv2.face read/write predate os.PathLike.
# =====================================================

TRAINER_FILE = str(settings.trainer_file)

LABELS_FILE = str(settings.labels_file)


# =====================================================
# MODEL AND LABEL LOADING (PE-4)
#
# This used to run at *import* time, and again on every start_camera().
# Importing this module therefore cost 9.2 s and read 55 MB of YAML before
# Flask had served a single request, which is why tests/conftest.py forbade
# importing it at all.
#
# Nothing here runs on import any more. RecognitionSession calls
# model_signature() - two stat() calls - and only calls load_model_and_labels()
# when the answer differs from the model it already holds, so a retrain is
# picked up on the next session start and an unchanged model is not re-read.
#
# The OpenCV contrib check moved in here from module scope, where it was a
# bare sys.exit(1). A library import must not be able to kill the interpreter;
# `import recognize_face` in an environment missing opencv-contrib used to take
# the whole process down, including the test runner collecting it.
# =====================================================

def model_signature():
    """
    A cheap fingerprint of the model on disk, or None if it is not there.

    Two stat() calls, against the 4.8 s an LBPH read costs. Size is included
    alongside mtime because Phase 0's outage was a *partial* write: a truncated
    trainer.yml can share an mtime with the write that produced it
    (tasks/lessons.md L1).
    """
    try:
        trainer_stat = settings.trainer_file.stat()
        labels_stat = settings.labels_file.stat()
    except OSError:
        return None

    return (
        trainer_stat.st_mtime_ns,
        trainer_stat.st_size,
        labels_stat.st_mtime_ns,
        labels_stat.st_size,
    )


def load_model_and_labels():
    """
    Read the LBPH model and its labels. Returns `(recognizer, label_map)`.

    Raises rather than returning a sentinel: a missing or malformed model is
    not a condition the caller can paper over, and RecognitionSession turns
    the exception into a refused session start with a logged reason.
    """
    logger.info("OpenCV version: %s", cv2.__version__)

    if not hasattr(cv2, "face"):
        raise RuntimeError(
            "OpenCV face module not found. Install with: "
            "python -m pip install opencv-contrib-python==4.10.0.84"
        )

    if not settings.trainer_file.exists():
        raise FileNotFoundError(
            f"trainer.yml was not found: {TRAINER_FILE}"
        )

    if not settings.labels_file.exists():
        raise FileNotFoundError(
            f"labels.txt was not found: {LABELS_FILE}"
        )

    new_recognizer = (
        cv2.face.LBPHFaceRecognizer_create()
    )
    new_recognizer.read(TRAINER_FILE)

    new_label_map = {}
    loaded_student_ids = set()

    with open(
        LABELS_FILE,
        encoding="utf-8"
    ) as labels_file:
        for line_number, line in enumerate(
            labels_file,
            start=1
        ):
            line = line.strip()

            if not line:
                continue

            try:
                label_text, folder_name = line.split(
                    ",",
                    1
                )

                label = int(label_text.strip())

                folder_parts = folder_name.split(
                    "_",
                    1
                )

                if len(folder_parts) != 2:
                    raise ValueError(
                        "Expected student_id_student_name"
                    )

                student_id = folder_parts[0].strip()
                student_name = folder_parts[1].strip()

                if not student_id or not student_name:
                    raise ValueError(
                        "Student ID or name is empty"
                    )

                if label in new_label_map:
                    raise ValueError(
                        f"Duplicate LBPH label {label}"
                    )

                if student_id in loaded_student_ids:
                    raise ValueError(
                        f"Duplicate student ID {student_id}"
                    )

                new_label_map[label] = {
                    "student_id": student_id,
                    "name": student_name
                }

                loaded_student_ids.add(
                    student_id
                )

            except ValueError as error:
                raise ValueError(
                    "Invalid labels.txt entry on line "
                    f"{line_number}: {line} ({error})"
                ) from error

    if not new_label_map:
        raise ValueError(
            "labels.txt does not contain any students."
        )

    logger.info(
        "Trainer loaded successfully: %d student identities",
        len(new_label_map)
    )

    return new_recognizer, new_label_map


# =====================================================
# MEDIAPIPE FACE MESH (PE-4)
#
# `import mediapipe` is 4.29 s of the old 9.2 s import cost - measured, and
# more than the 55 MB model read. Deferring only the model would have left
# ~5.5 s, which was still too expensive for the test suite to import this
# module. So the import is inside the function, and it happens when a session
# starts rather than when anything imports this file.
#
# Constructing the FaceMesh itself is 0.04 s, so a session gets a fresh one.
# That is deliberate: static_image_mode=False means the detector carries
# tracking state between frames, and a new session must not inherit the last
# session's idea of where the faces were.
# =====================================================


def make_detector():
    """A MediaPipe FaceMesh for one session."""
    import mediapipe as mp

    try:
        mp_face_mesh = mp.solutions.face_mesh
    except AttributeError:
        import mediapipe.python.solutions.face_mesh as mp_face_mesh

    detector = mp_face_mesh.FaceMesh(
        static_image_mode=False,
        max_num_faces=10,
        refine_landmarks=True,
        min_detection_confidence=0.60,
        min_tracking_confidence=0.55
    )

    logger.info("MediaPipe Face Mesh loaded successfully")

    return detector


# =====================================================
# FACE VALIDATION AND QUALITY SETTINGS
#
# The geometry thresholds and the mesh landmark indices used to live here,
# duplicated in capture_dataset.py with different values (MA-4). They now live
# in vision/validation.py as RECOGNITION_PROFILE, beside the enrolment profile
# that differs from it, so the divergence is visible in one place.
#
# MIN_FACE_WIDTH / MIN_FACE_HEIGHT are still referenced below for the "Move
# closer" hint, which is a UI nudge rather than a gate, so they are read from
# the profile rather than restated.
# =====================================================

MIN_FACE_WIDTH = RECOGNITION_PROFILE.min_face_width
MIN_FACE_HEIGHT = RECOGNITION_PROFILE.min_face_height


# =====================================================
# TRACKING AND VERIFICATION SETTINGS
#
# These were eleven loose module constants read directly by seven functions.
# They are one TrackConfig now, passed to the tracker that uses them, so a
# threshold cannot be read by something that was never meant to see it and
# the whole set can be varied in a test (MA-12, and MA-6's "60 magic numbers
# with no configuration layer").
#
# The values are unchanged. Like the geometry profiles, they are the "safe
# starting values" the original comment described and have never been
# calibrated; Phase 6 is where that happens.
# =====================================================

TRACK_CONFIG = TrackConfig(
    timeout_seconds=1.5,
    max_distance=180.0,
    min_iou=0.08,
    min_size_similarity=0.40,
    real_face_confirm_frames=5,
    prediction_history_size=20,
    min_label_agreement=0.85,
    min_consecutive_identity_frames=8,
    confirmation_confidence=52.0,
    max_weak_frames_before_clear=3,
    max_confirmed_mismatch_frames=5,
    liveness_reset_mismatch_frames=2,
)

REAL_FACE_CONFIRM_FRAMES = TRACK_CONFIG.real_face_confirm_frames
PREDICTION_HISTORY_SIZE = TRACK_CONFIG.prediction_history_size


# =====================================================
# RECOGNITION SETTINGS
# Lower LBPH distance is better.
#
# RECOGNITION_THRESHOLD now comes from config/settings.py so production and
# the eval_*.py harnesses read one value and cannot drift apart. It is still
# 58.0; making it configurable is not the same as tuning it. See
# tasks/lessons.md L2 - this threshold is calibrated against the distance
# scale that LBPH_PARAMS in train_model.py produces, and changing either one
# without the other silently rejects every face.
# =====================================================

RECOGNITION_THRESHOLD = settings.recognition_threshold


# =====================================================
# PERFORMANCE SETTINGS
# MediaPipe runs on a smaller copy while face alignment
# and LBPH still use the original camera frame.
# =====================================================

MEDIAPIPE_PROCESS_WIDTH = 960
STREAM_JPEG_QUALITY = 70
STREAM_FRAME_DELAY = 0.0


# =====================================================
# THREADED CAMERA READER (PE-6)
#
# The class used to be defined here: a `while self._running: cap.read()` loop
# with a plain Lock and no frame-ready signal. It moved to infra/camera.py and
# grew a Condition, so the producer stops spinning when the camera stops
# producing and the consumer stops re-processing frames it has already seen.
# See that module for what each half of PE-6 actually cost.
# =====================================================


# =====================================================
# ACTIVE LIVENESS (SE-12)
#
# Six loose constants used to live here - one of two fixed challenges, a
# 10-second timeout, and three yaw thresholds in FRAME-NORMALISED units. They
# are now vision/liveness.py, and the thresholds are in FACE-WIDTH units.
#
# That unit change is the substance of this, not tidying. The geometry gate
# works in face widths (it refuses a nose past 0.28w); the old liveness
# thresholds did not, so the turn they demanded grew with distance while the
# turn a person actually makes stays at a constant ~0.14w. Past roughly a
# 137 px face box the challenge asked for more turn than the gate would allow,
# so a student standing a normal distance back could never pass it and nothing
# said why. Measured; the table is in vision/liveness.py.
# =====================================================

LIVENESS_CONFIG = LivenessConfig()

POST_LIVENESS_MATCH_FRAMES = LIVENESS_CONFIG.post_match_frames


# =====================================================
# THE SESSION (RE-2, RE-10, PE-4)
#
# `cap`, `camera_reader`, `attendance_running`, `current_subject`,
# `recognized`, `recognizer`, `label_map` and the module-level `tracker` used
# to live here, as eight globals mutated from the Flask worker thread and the
# camera thread with no lock. They are now inside `session`, behind an RLock,
# with one stream allowed at a time - see vision/session.py for what the lock
# protects and what the single-viewer invariant protects instead.
#
# The heavy collaborators are passed in rather than imported by vision/, which
# is what keeps that package free of MediaPipe, the camera and the model.
# =====================================================


def configure_camera(capture):
    """Resolution and buffering for a freshly opened camera."""
    capture.set(
        cv2.CAP_PROP_FRAME_WIDTH,
        1280
    )
    capture.set(
        cv2.CAP_PROP_FRAME_HEIGHT,
        720
    )
    capture.set(
        cv2.CAP_PROP_BUFFERSIZE,
        1
    )

    actual_width = int(
        capture.get(cv2.CAP_PROP_FRAME_WIDTH)
    )
    actual_height = int(
        capture.get(cv2.CAP_PROP_FRAME_HEIGHT)
    )

    logger.info(
        "Camera resolution: %sx%s",
        actual_width,
        actual_height
    )

    # Discard the first few frames: a camera that has just opened often hands
    # back a stale or half-exposed buffer.
    for _ in range(3):
        capture.read()


session = RecognitionSession(
    hooks=SessionHooks(
        open_camera=open_best_camera,
        load_model=load_model_and_labels,
        model_signature=model_signature,
        make_detector=make_detector,
        make_reader=lambda capture: CameraReader(capture),
        configure_camera=configure_camera,
    ),
    config=TRACK_CONFIG,
)


# =====================================================
# FACE GEOMETRY AND QUALITY (MA-4)
#
# get_face_box, box_iou, box_center, box_area, is_valid_face_candidate and
# the quality gate all used to be defined here, and again - differently - in
# capture_dataset.py. They now come from vision/, which both modules import.
#
# The two wrappers below exist so the call sites in this file stay readable:
# they bind the recognition profile once instead of repeating it at every
# call. Recognition never uses any other profile.
# =====================================================


def face_passes_geometry_gate(
    face_landmarks,
    frame_width,
    frame_height,
    box
):
    return is_valid_face_candidate(
        face_landmarks,
        frame_width,
        frame_height,
        box,
        RECOGNITION_PROFILE
    )


def get_face_quality_issue(aligned_face):
    return quality_issue(
        aligned_face,
        RECOGNITION_QUALITY
    )


# =====================================================
# LIVENESS
# =====================================================


def get_face_yaw(face_landmarks, frame_width=None, box=None):
    """
    How far the nose sits from the eye centre.

    With `frame_width` and `box`, the answer is in **face-width units** - the
    unit the geometry gate and the liveness challenge both work in, and a
    property of the head rather than of how far away it is. Without them it is
    the raw frame-normalised value the mesh gives, which is what the drawing
    code and the old callers used.

    SE-12 is the story of those two being conflated: see vision/liveness.py.
    """
    nose = face_landmarks.landmark[NOSE_TIP]
    left_eye = face_landmarks.landmark[LEFT_EYE_OUTER]
    right_eye = face_landmarks.landmark[RIGHT_EYE_OUTER]

    eye_center_x = (
        left_eye.x + right_eye.x
    ) / 2.0

    yaw = nose.x - eye_center_x

    if frame_width is None or box is None or box.width <= 0:
        return yaw

    return yaw * frame_width / box.width


def create_liveness_state():
    """A fresh challenge for a newly confirmed track."""
    return LivenessChallenge(config=LIVENESS_CONFIG)


# =====================================================
# DATABASE
# =====================================================


def save_attendance(student_id, subject, status):
    """
    Write one attendance row, or report why not. True if the student is
    enrolled and their attendance for today is now recorded.

    PE-7 named this function specifically: it opened a brand new MySQL
    connection per recognised face event, from inside the recognition loop. It
    now takes one from the pool for the duration of the write and gives it
    straight back.
    """
    try:
        with db_cursor(dictionary=True, commit=True) as cursor:
            cursor.execute(
                """
                SELECT student_id, name
                FROM students
                WHERE student_id = %s
                """,
                (student_id,)
            )

            enrolled_student = cursor.fetchone()

            if enrolled_student is None:
                logger.warning(
                    "Rejected attendance: %s is not enrolled", student_id
                )
                return False

            official_name = enrolled_student["name"]

            # RE-3: read-then-write, still a TOCTOU race. The real fix is a
            # UNIQUE constraint on (student_id, subject_code, attendance_date)
            # and it belongs to Phase 4, where the schema is already moving.
            cursor.execute(
                """
                SELECT student_id
                FROM attendance
                WHERE student_id = %s
                AND attendance_date = %s
                AND subject_code = %s
                """,
                (
                    student_id,
                    datetime.now().date(),
                    subject
                )
            )

            existing_record = cursor.fetchone()

            if existing_record is None:
                cursor.execute(
                    """
                    INSERT INTO attendance
                    (
                        student_id,
                        student_name,
                        subject_code,
                        attendance_date,
                        time_in,
                        status
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        student_id,
                        official_name,
                        subject,
                        datetime.now().date(),
                        datetime.now().strftime(
                            "%H:%M:%S"
                        ),
                        status
                    )
                )

                logger.info(
                    "Attendance recorded: %s - %s", student_id, official_name
                )

            else:
                logger.info(
                    "Attendance already recorded for %s", student_id
                )

        return True

    except mysql.connector.Error:
        logger.exception("Database error while saving attendance")
        return False


# =====================================================
# CAMERA CONTROL
# =====================================================


def start_camera(subject_code):
    """
    Begin an attendance session. True if recognition can now run.

    Thin now: the model reload, the camera open, the tracker reset and the
    "already running" decision all belong to the session, which holds the lock
    that makes them one atomic transition (RE-2).
    """
    return session.start(subject_code)


def stop_camera():
    """End the session and release the camera."""
    session.stop()


# =====================================================
# TRACKING AND VERIFICATION STATE (MA-12, RE-2)
#
# `expire_old_tracks`, `assign_track_id` and the five functions that mutated
# a track's verification dict used to live here, over 200 lines, operating on
# the module globals `tracks`, `track_verification` and `next_track_id`.
#
# They are now vision/tracking.py - FaceTracker owns the table, TrackState
# owns one face's progress - which makes the decision logic testable without
# a camera. The single instance moved again in this commit: it belonged to this
# module, unlocked; it now belongs to `session`, which resets it on start.
# =====================================================


# =====================================================
# CONFIRMED IDENTITY + LIVENESS
# Liveness advances only while LBPH still agrees with
# the locked identity. This prevents another face from
# completing the challenge after a track switch.
# =====================================================


def process_confirmed_track(
    context,
    state,
    candidate_id,
    current_yaw
):
    subject = context.subject
    locked_id = state.student_id
    locked_name = state.student_name

    candidate_agrees = (
        candidate_id is not None
        and candidate_id == locked_id
    )

    if not candidate_agrees:
        state.mismatch_frames += 1

        if state.liveness is not None:
            state.liveness.note_identity_mismatch()

        if (
            state.mismatch_frames
            >= TRACK_CONFIG.liveness_reset_mismatch_frames
        ):
            state.liveness = create_liveness_state()

        if (
            state.mismatch_frames
            >= TRACK_CONFIG.max_confirmed_mismatch_frames
        ):
            state = state.reset_identity()

        return (
            "Unknown",
            "Verifying...",
            "Identity check lost",
            state
        )

    state.mismatch_frames = 0

    if state.attendance_saved:
        return (
            locked_id,
            locked_name,
            "Present",
            state
        )

    if state.liveness is None:
        state.liveness = create_liveness_state()

    state.liveness.update(current_yaw)
    state.liveness.note_identity_match()

    if state.liveness.identity_reconfirmed:
        attendance_saved = save_attendance(
            locked_id,
            subject,
            "Present"
        )

        if attendance_saved:
            context.recognized.add(locked_id)
            state.attendance_saved = True

            return (
                locked_id,
                locked_name,
                "Present",
                state
            )

        return (
            locked_id,
            locked_name,
            "Not Enrolled",
            state
        )

    return (
        locked_id,
        locked_name,
        state.liveness.message(),
        state
    )


# =====================================================
# DRAWING
# =====================================================


def status_color(status):
    if status == "Present":
        return 0, 255, 0

    if status.startswith("Verifying"):
        return 0, 255, 255

    if (
        status.startswith("Liveness")
        or status == "Preparing liveness..."
    ):
        return 255, 255, 0

    if status == "Not Enrolled":
        return 0, 165, 255

    if (
        "Move closer" in status
        or "blurry" in status.lower()
        or "dark" in status.lower()
        or "bright" in status.lower()
        or "contrast" in status.lower()
    ):
        return 0, 165, 255

    return 0, 0, 255


def draw_face_result(
    frame,
    box,
    student_name,
    status,
    confidence,
    track_id
):
    frame_height, _ = frame.shape[:2]
    x1, y1, x2, y2 = box

    color = status_color(status)

    cv2.rectangle(
        frame,
        (x1, y1),
        (x2, y2),
        color,
        2
    )

    cv2.putText(
        frame,
        student_name,
        (
            x1,
            max(y1 - 10, 25)
        ),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.70,
        color,
        2
    )

    if confidence >= 900:
        score_text = "--"
    else:
        score_text = f"{confidence:.1f}"

    cv2.putText(
        frame,
        f"{status} ({score_text})",
        (
            x1,
            min(y2 + 25, frame_height - 10)
        ),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.55,
        color,
        2
    )


# =====================================================
# PER-FACE DECISION (MA-12)
#
# These two functions are the body that used to sit inline in
# generate_frames(), seven levels deep. Pulling them out is the whole point
# of MA-12: the loop below now reads as detect -> track -> predict -> decide
# -> draw, and each step can be read on its own.
#
# The order of the branches in resolve_track_identity() is load-bearing and
# is unchanged from the original. A confirmed track is checked *first* and is
# immutable, which is what stops a fluctuating LBPH result from renaming a
# face mid-session.
# =====================================================


def detect_faces(context, frame):
    """
    Run MediaPipe FaceMesh on a downscaled copy of the frame.

    Landmarks come back normalised, so they map onto the full-resolution
    frame unchanged - which is why face alignment and LBPH still get the
    original pixels while detection pays for a smaller image.
    """
    frame_height, frame_width = frame.shape[:2]

    if frame_width > MEDIAPIPE_PROCESS_WIDTH:
        process_scale = MEDIAPIPE_PROCESS_WIDTH / float(frame_width)

        process_frame = cv2.resize(
            frame,
            (
                MEDIAPIPE_PROCESS_WIDTH,
                max(int(frame_height * process_scale), 1)
            ),
            interpolation=cv2.INTER_AREA
        )
    else:
        process_frame = frame

    rgb = cv2.cvtColor(process_frame, cv2.COLOR_BGR2RGB)

    rgb.flags.writeable = False
    results = context.detector.process(rgb)
    rgb.flags.writeable = True

    return results


def encode_frame(frame):
    """The MJPEG part boundary for one frame, or None if encoding failed."""
    success, buffer = cv2.imencode(
        ".jpg",
        frame,
        [
            int(cv2.IMWRITE_JPEG_QUALITY),
            STREAM_JPEG_QUALITY
        ]
    )

    if not success:
        return None

    return (
        b"--frame\r\n"
        b"Content-Type: image/jpeg\r\n\r\n"
        + buffer.tobytes()
        + b"\r\n"
    )


def predict_identity(context, frame, face_landmarks, state, track_id):
    """
    Align, quality-check and run LBPH on one face.

    Returns `(label, confidence, issue)`. `issue` is a human-readable
    quality problem or None; a confidence of 999.0 means no prediction was
    attempted, which draw_face_result() renders as "--".
    """
    # A track that has already recorded attendance does not need recognising
    # again - the identity is locked and the row is written. Skipping the
    # predict here is what keeps a full classroom from paying for 314M float
    # operations per already-marked face per frame (PE-3).
    skip_recognition = state.confirmed and state.attendance_saved

    try:
        aligned_face = align_face(frame, face_landmarks)
        issue = get_face_quality_issue(aligned_face)

        if issue is not None or skip_recognition:
            return -1, 999.0, issue

        processed_face = preprocess_for_lbph(aligned_face)
        label, confidence = context.recognizer.predict(processed_face)

        # DEBUG, not INFO: this fires once per tracked face per frame. At
        # ~15 fps with three faces in shot that is 45 lines a second, which
        # makes the log useless for anything else. Set LOG_LEVEL=DEBUG in
        # .env when you are actually diagnosing recognition.
        logger.debug(
            "Track: %s | Label: %s | Distance: %.1f",
            track_id,
            label,
            confidence
        )

        return label, confidence, None

    except Exception:
        logger.debug(
            "Recognition failed for track %s",
            track_id,
            exc_info=True
        )
        return -1, 999.0, "Face alignment failed"


def resolve_track_identity(
    context,
    *,
    state,
    label,
    confidence,
    issue,
    face_landmarks,
    box,
    frame_width,
    claimed_student_ids
):
    """
    Turn one frame's prediction into what the operator sees for this track.

    Returns `(student_id, student_name, status, state)`. `state` is returned
    rather than only mutated because losing an identity replaces the object
    (`TrackState.reset_identity`), exactly as the original did.
    """
    label_map = context.label_map

    strong_prediction = (
        issue is None
        and confidence <= RECOGNITION_THRESHOLD
        and label in label_map
    )

    candidate_id = None
    candidate_name = None

    if strong_prediction:
        candidate = label_map[label]
        candidate_id = candidate["student_id"]
        candidate_name = candidate["name"]

    # A confirmed track is immutable. Never let a later LBPH prediction
    # overwrite the identity already locked to this physical track - this is
    # what prevents one face from changing from Jeff to Julio when the LBPH
    # result fluctuates.
    if state.confirmed:
        return resolve_confirmed_track(
            context,
            state,
            candidate_id,
            face_landmarks,
            box,
            frame_width,
            claimed_student_ids
        )

    if issue is not None:
        state.note_weak_prediction()
        return "Unknown", "Face detected", issue, state

    # Only an unconfirmed track may inherit an identity that already
    # completed attendance. A confirmed track above can never be overwritten.
    if candidate_id is not None and candidate_id in context.recognized:
        state.adopt_completed_identity(candidate_id, candidate_name)
        claimed_student_ids.add(candidate_id)
        return candidate_id, candidate_name, "Present", state

    if not strong_prediction:
        state.note_weak_prediction()
        return "Unknown", *hint_for_weak_track(box), state

    if candidate_id in claimed_student_ids:
        state.note_weak_prediction()
        return "Unknown", "Unknown", "Duplicate identity blocked", state

    return accumulate_towards_confirmation(
        state,
        candidate_id,
        candidate_name,
        confidence,
        claimed_student_ids
    )


def resolve_confirmed_track(
    context,
    state,
    candidate_id,
    face_landmarks,
    box,
    frame_width,
    claimed_student_ids
):
    """A track whose identity is already locked: hold it, or lose it."""
    locked_id = state.student_id
    locked_name = state.student_name

    if state.attendance_saved:
        if locked_id:
            claimed_student_ids.add(locked_id)
        return locked_id, locked_name, "Present", state

    if locked_id in claimed_student_ids:
        return locked_id, locked_name, "Duplicate identity blocked", state

    # SE-12: yaw in face-width units, which needs the box. Passing the raw
    # frame-normalised value here is what made the challenge unpassable for
    # anyone standing more than about a metre back.
    student_id, student_name, status, state = process_confirmed_track(
        context,
        state,
        candidate_id,
        get_face_yaw(face_landmarks, frame_width, box)
    )

    if student_id != "Unknown":
        claimed_student_ids.add(student_id)

    return student_id, student_name, status, state


def accumulate_towards_confirmation(
    state,
    candidate_id,
    candidate_name,
    confidence,
    claimed_student_ids
):
    """
    A good prediction on an unconfirmed track: add a vote, then check.

    Confirmation needs a full window of votes that overwhelmingly agree, a
    good average distance, and a consecutive run on the same identity. See
    TrackState.can_confirm().
    """
    state.add_prediction(candidate_id, candidate_name, confidence)
    verdict = state.evaluate_history()

    if verdict is None:
        return (
            "Unknown",
            "Verifying...",
            f"Verifying 0/{PREDICTION_HISTORY_SIZE}",
            state
        )

    if not state.can_confirm(verdict, claimed_student_ids):
        return (
            "Unknown",
            "Verifying...",
            (
                f"Verifying {verdict.history_count}/{PREDICTION_HISTORY_SIZE} "
                f"{verdict.agreement_ratio * 100:.0f}%"
            ),
            state
        )

    state.confirm(
        verdict.dominant_id,
        verdict.dominant_name,
        create_liveness_state()
    )
    claimed_student_ids.add(verdict.dominant_id)

    return (
        verdict.dominant_id,
        verdict.dominant_name,
        state.liveness.message(),
        state
    )


def hint_for_weak_track(box):
    """`(name, status)` for a face we cannot match - a nudge, not a verdict."""
    if (
        box.width < MIN_FACE_WIDTH * 1.25
        or box.height < MIN_FACE_HEIGHT * 1.25
    ):
        return "Face detected", "Move closer"

    return "Unknown", "Unknown"


# =====================================================
# VIDEO GENERATOR
# =====================================================


def open_video_stream():
    """
    Claim the single stream slot and return the frame generator.

    Split from `generate_frames()` on purpose. The slot has to be claimed
    *before* Flask builds the `Response`, because once the response headers are
    on the wire a refusal can no longer be expressed as a 409 - the browser is
    already being handed a `multipart/x-mixed-replace` body. A generator's body
    does not run until it is first iterated, which is after that point.

    Raises `SessionNotRunning` or `SessionBusy`; `/video_feed` maps both onto
    status codes.
    """
    token = session.acquire_viewer()

    return generate_frames(token)


def generate_frames(token):
    """
    The MJPEG stream: one part per frame, for as long as the session runs.

    Ends when the session stops, when the camera goes away, or when the client
    disconnects (which closes the generator). In every case the `finally`
    gives up the viewer slot and **nothing here releases the camera** - that
    was RE-10, where one browser tab closing ended the session for everyone.
    """
    try:
        yield from _stream_frames()
    finally:
        session.release_viewer(token)


def _stream_frames():
    while True:
        context = session.snapshot()

        if not context.running:
            break

        if context.reader is None or context.detector is None:
            break

        ret, frame = context.reader.read()

        if not ret:
            # PE-6: `read()` blocks until a frame the loop has not seen
            # arrives, or its timeout expires, so there is nothing to sleep
            # off here any more. Falling through re-snapshots the session,
            # which is how a stop that happened while we were waiting is
            # noticed.
            continue

        frame_height, frame_width = frame.shape[:2]

        results = detect_faces(context, frame)

        used_track_ids = set()
        claimed_student_ids = set()
        displayed_face_count = 0

        for face_landmarks in results.multi_face_landmarks or []:
            raw_box = get_face_box(
                face_landmarks,
                frame_width,
                frame_height
            )

            if not face_passes_geometry_gate(
                face_landmarks,
                frame_width,
                frame_height,
                raw_box
            ):
                # Invalid object-like detections are ignored
                # completely and receive no visible box.
                continue

            track_id = context.tracker.assign(raw_box, used_track_ids)
            used_track_ids.add(track_id)

            state = context.tracker.state_for(track_id)
            state.note_valid_frame()

            # Do not draw or recognize a newly detected object
            # until it has remained a geometrically valid face
            # for several consecutive frames.
            if state.is_provisional:
                continue

            displayed_face_count += 1

            label, confidence, issue = predict_identity(
                context,
                frame,
                face_landmarks,
                state,
                track_id
            )

            student_id, student_name, status, state = resolve_track_identity(
                context,
                state=state,
                label=label,
                confidence=confidence,
                issue=issue,
                face_landmarks=face_landmarks,
                box=raw_box,
                frame_width=frame_width,
                claimed_student_ids=claimed_student_ids
            )

            context.tracker.replace_state(track_id, state)

            draw_face_result(
                frame,
                raw_box,
                student_name,
                status,
                confidence,
                track_id
            )

        if displayed_face_count == 0:
            cv2.putText(
                frame,
                "No valid face detected",
                (20, 40),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                (0, 0, 255),
                2
            )

        part = encode_frame(frame)

        if part is None:
            continue

        yield part

    # RE-10 was three lines here: `if cap is not None: cap.release()`. The
    # camera is shared session state, and a generator ends whenever its client
    # goes away - so closing one browser tab released the camera underneath
    # every other viewer and ended the session for the whole room. Teardown
    # belongs to stop_camera(), which is the only thing that knows the session
    # is actually over.
