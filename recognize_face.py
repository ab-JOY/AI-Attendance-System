import logging
import random
import sys
import threading
import time
from datetime import datetime

import cv2
import mediapipe as mp
import mysql.connector

from camera_utils import open_best_camera
from config.settings import settings
from face_preprocessing import align_face, preprocess_for_lbph
from vision.geometry import get_face_box
from vision.landmarks import LEFT_EYE_OUTER, NOSE_TIP, RIGHT_EYE_OUTER
from vision.quality import RECOGNITION_QUALITY, quality_issue
from vision.tracking import FaceTracker, TrackConfig
from vision.validation import RECOGNITION_PROFILE, is_valid_face_candidate

logger = logging.getLogger(__name__)


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
# CHECK OPENCV CONTRIB
# =====================================================

logger.info("OpenCV version: %s", cv2.__version__)

if not hasattr(cv2, "face"):
    logger.error(
        "OpenCV face module not found. Install with: "
        "python -m pip install opencv-contrib-python==4.10.0.84"
    )
    sys.exit(1)


# =====================================================
# MODEL AND LABEL LOADING
# The files are reloaded whenever a new attendance
# session starts, so the latest retrained model is used.
# =====================================================

recognizer = None
label_map = {}


def load_model_and_labels():
    global recognizer
    global label_map

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

    recognizer = new_recognizer
    label_map = new_label_map

    logger.info(
        "Trainer loaded successfully: %d student identities",
        len(label_map)
    )


try:
    load_model_and_labels()
except Exception:
    logger.warning(
        "Model load failed. Recognition is unavailable until a model is "
        "trained.",
        exc_info=True
    )
    recognizer = None
    label_map = {}


# =====================================================
# MEDIAPIPE FACE MESH
# =====================================================

try:
    mp_face_mesh = mp.solutions.face_mesh
except AttributeError:
    import mediapipe.python.solutions.face_mesh as mp_face_mesh

face_mesh = mp_face_mesh.FaceMesh(
    static_image_mode=False,
    max_num_faces=10,
    refine_landmarks=True,
    min_detection_confidence=0.60,
    min_tracking_confidence=0.55
)

logger.info("MediaPipe Face Mesh loaded successfully")


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
# THREADED CAMERA READER
# =====================================================


class CameraReader:
    """Reads camera frames in a background thread
    so the processing loop always gets the freshest
    frame without blocking on USB/camera latency."""

    def __init__(self, cap):
        self._cap = cap
        self._frame = None
        self._ret = False
        self._lock = threading.Lock()
        self._running = True
        self._thread = threading.Thread(
            target=self._reader,
            daemon=True
        )
        self._thread.start()

    def _reader(self):
        while self._running:
            ret, frame = self._cap.read()
            with self._lock:
                self._ret = ret
                self._frame = frame

    def read(self):
        with self._lock:
            if self._frame is None:
                return False, None
            return self._ret, self._frame.copy()

    def stop(self):
        self._running = False
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)


# =====================================================
# ACTIVE LIVENESS SETTINGS
# =====================================================

LIVENESS_CHALLENGES = [
    "TURN_LEFT",
    "TURN_RIGHT"
]

LIVENESS_TIMEOUT_SECONDS = 10.0

LIVENESS_LEFT_YAW = 0.030
LIVENESS_RIGHT_YAW = -0.030
LIVENESS_CENTER_YAW = 0.018

POST_LIVENESS_MATCH_FRAMES = 8


# =====================================================
# GLOBAL STATE
# =====================================================

cap = None
camera_reader = None
attendance_running = False
current_subject = None

recognized = set()


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


def get_face_yaw(face_landmarks):
    nose = face_landmarks.landmark[NOSE_TIP]
    left_eye = face_landmarks.landmark[LEFT_EYE_OUTER]
    right_eye = face_landmarks.landmark[RIGHT_EYE_OUTER]

    eye_center_x = (
        left_eye.x + right_eye.x
    ) / 2.0

    return nose.x - eye_center_x


def create_liveness_state():
    return {
        "challenge": random.choice(
            LIVENESS_CHALLENGES
        ),
        "started_at": time.time(),
        "movement_seen": False,
        "passed": False,
        "post_match_count": 0
    }


def update_liveness(liveness_state, yaw):
    if liveness_state is None:
        return create_liveness_state()

    elapsed = (
        time.time()
        - liveness_state["started_at"]
    )

    if elapsed > LIVENESS_TIMEOUT_SECONDS:
        return create_liveness_state()

    challenge = liveness_state["challenge"]

    if not liveness_state["movement_seen"]:
        if (
            challenge == "TURN_LEFT"
            and yaw >= LIVENESS_LEFT_YAW
        ) or (
            challenge == "TURN_RIGHT"
            and yaw <= LIVENESS_RIGHT_YAW
        ):
            liveness_state["movement_seen"] = True

    elif abs(yaw) <= LIVENESS_CENTER_YAW:
        liveness_state["passed"] = True

    return liveness_state


def get_liveness_message(liveness_state):
    if liveness_state is None:
        return "Preparing liveness..."

    if liveness_state.get("passed", False):
        post_match_count = liveness_state.get(
            "post_match_count",
            0
        )

        if post_match_count < POST_LIVENESS_MATCH_FRAMES:
            return (
                "Liveness passed - Rechecking "
                f"{post_match_count}/"
                f"{POST_LIVENESS_MATCH_FRAMES}"
            )

        return "Liveness Passed"

    challenge = liveness_state["challenge"]

    if not liveness_state["movement_seen"]:
        if challenge == "TURN_LEFT":
            return "Liveness: Turn LEFT"

        return "Liveness: Turn RIGHT"

    return "Liveness: Return to CENTER"


# =====================================================
# DATABASE
# =====================================================


def save_attendance(student_id, subject, status):
    conn = None
    cursor = None

    try:
        conn = mysql.connector.connect(
            **settings.db_kwargs()
        )

        cursor = conn.cursor(
            dictionary=True
        )

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

            conn.commit()

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

    finally:
        if cursor is not None:
            cursor.close()

        if conn is not None:
            conn.close()


# =====================================================
# CAMERA CONTROL
# =====================================================


def start_camera(subject_code):
    global cap
    global camera_reader
    global attendance_running
    global current_subject
    global recognized

    try:
        load_model_and_labels()
    except Exception:
        logger.exception("Model reload failed, cannot start attendance")
        return False

    if recognizer is None or not label_map:
        logger.error("No trained model available, cannot start attendance")
        return False

    current_subject = subject_code

    recognized = set()
    tracker.reset()

    if cap is None:
        cap = open_best_camera()

        if cap is None or not cap.isOpened():
            logger.error("Cannot open camera")
            cap = None
            return False

        cap.set(
            cv2.CAP_PROP_FRAME_WIDTH,
            1280
        )
        cap.set(
            cv2.CAP_PROP_FRAME_HEIGHT,
            720
        )
        cap.set(
            cv2.CAP_PROP_BUFFERSIZE,
            1
        )

        actual_width = int(
            cap.get(cv2.CAP_PROP_FRAME_WIDTH)
        )
        actual_height = int(
            cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
        )

        logger.info(
            "Camera resolution: %sx%s",
            actual_width,
            actual_height
        )

        for _ in range(3):
            cap.read()

        camera_reader = CameraReader(cap)

    attendance_running = True
    logger.info("Attendance started")

    return True


def stop_camera():
    global cap
    global camera_reader
    global attendance_running

    attendance_running = False
    tracker.reset()

    if camera_reader is not None:
        camera_reader.stop()
        camera_reader = None

    if cap is not None:
        cap.release()
        cap = None

    logger.info("Attendance ended")


# =====================================================
# TRACKING AND VERIFICATION STATE (MA-12, RE-2)
#
# `expire_old_tracks`, `assign_track_id` and the five functions that mutated
# a track's verification dict used to live here, over 200 lines, operating on
# the module globals `tracks`, `track_verification` and `next_track_id`.
#
# They are now vision/tracking.py - FaceTracker owns the table, TrackState
# owns one face's progress - which makes the decision logic testable without
# a camera. `tracker` below is the single instance; start_camera() resets it.
# =====================================================

tracker = FaceTracker(config=TRACK_CONFIG)


# =====================================================
# CONFIRMED IDENTITY + LIVENESS
# Liveness advances only while LBPH still agrees with
# the locked identity. This prevents another face from
# completing the challenge after a track switch.
# =====================================================


def process_confirmed_track(
    state,
    candidate_id,
    current_yaw,
    subject
):
    locked_id = state.student_id
    locked_name = state.student_name

    candidate_agrees = (
        candidate_id is not None
        and candidate_id == locked_id
    )

    if not candidate_agrees:
        state.mismatch_frames += 1

        if state.liveness is not None:
            state.liveness["post_match_count"] = 0

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

    state.liveness = update_liveness(
        state.liveness,
        current_yaw
    )

    if state.liveness.get("passed", False):
        state.liveness["post_match_count"] = (
            state.liveness.get(
                "post_match_count",
                0
            ) + 1
        )

    liveness_passed = state.liveness.get(
        "passed",
        False
    )

    post_match_count = state.liveness.get(
        "post_match_count",
        0
    )

    identity_reconfirmed = (
        post_match_count
        >= POST_LIVENESS_MATCH_FRAMES
    )

    if liveness_passed and identity_reconfirmed:
        attendance_saved = save_attendance(
            locked_id,
            subject,
            "Present"
        )

        if attendance_saved:
            recognized.add(locked_id)
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
        get_liveness_message(
            state.liveness
        ),
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


def detect_faces(frame):
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
    results = face_mesh.process(rgb)
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


def predict_identity(frame, face_landmarks, state, track_id):
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
        label, confidence = recognizer.predict(processed_face)

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
    *,
    state,
    label,
    confidence,
    issue,
    face_landmarks,
    box,
    claimed_student_ids
):
    """
    Turn one frame's prediction into what the operator sees for this track.

    Returns `(student_id, student_name, status, state)`. `state` is returned
    rather than only mutated because losing an identity replaces the object
    (`TrackState.reset_identity`), exactly as the original did.
    """
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
            state,
            candidate_id,
            face_landmarks,
            claimed_student_ids
        )

    if issue is not None:
        state.note_weak_prediction()
        return "Unknown", "Face detected", issue, state

    # Only an unconfirmed track may inherit an identity that already
    # completed attendance. A confirmed track above can never be overwritten.
    if candidate_id is not None and candidate_id in recognized:
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
    state,
    candidate_id,
    face_landmarks,
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

    student_id, student_name, status, state = process_confirmed_track(
        state,
        candidate_id,
        get_face_yaw(face_landmarks),
        current_subject
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
        get_liveness_message(state.liveness),
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


def generate_frames():
    global cap
    global attendance_running

    while attendance_running:
        if cap is None:
            break

        if camera_reader is None:
            break

        ret, frame = camera_reader.read()

        if not ret:
            time.sleep(0.02)
            continue

        frame_height, frame_width = frame.shape[:2]

        results = detect_faces(frame)

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

            track_id = tracker.assign(raw_box, used_track_ids)
            used_track_ids.add(track_id)

            state = tracker.state_for(track_id)
            state.note_valid_frame()

            # Do not draw or recognize a newly detected object
            # until it has remained a geometrically valid face
            # for several consecutive frames.
            if state.is_provisional:
                continue

            displayed_face_count += 1

            label, confidence, issue = predict_identity(
                frame,
                face_landmarks,
                state,
                track_id
            )

            student_id, student_name, status, state = resolve_track_identity(
                state=state,
                label=label,
                confidence=confidence,
                issue=issue,
                face_landmarks=face_landmarks,
                box=raw_box,
                claimed_student_ids=claimed_student_ids
            )

            tracker.replace_state(track_id, state)

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

    if cap is not None:
        cap.release()
        cap = None
