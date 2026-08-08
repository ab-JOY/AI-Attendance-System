import logging
import random
import sys
import threading
import time
from collections import Counter, deque
from datetime import datetime

import cv2
import mediapipe as mp
import mysql.connector

from camera_utils import open_best_camera
from config.settings import settings
from face_preprocessing import align_face, preprocess_for_lbph
from vision.geometry import box_area, box_center, box_iou, get_face_box
from vision.landmarks import LEFT_EYE_OUTER, NOSE_TIP, RIGHT_EYE_OUTER
from vision.quality import RECOGNITION_QUALITY, quality_issue
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

REAL_FACE_CONFIRM_FRAMES = 5


# =====================================================
# TRACKING SETTINGS
# =====================================================

TRACK_TIMEOUT_SECONDS = 1.5
TRACK_MAX_DISTANCE = 180.0
TRACK_MIN_IOU = 0.08
TRACK_MIN_SIZE_SIMILARITY = 0.40


# =====================================================
# RECOGNITION SETTINGS
# Lower LBPH distance is better.
# These are safe starting values and should later be
# calibrated using held-out test images.
#
# RECOGNITION_THRESHOLD now comes from config/settings.py so production and
# the eval_*.py harnesses read one value and cannot drift apart. It is still
# 58.0; making it configurable is not the same as tuning it. See
# tasks/lessons.md L2 - this threshold is calibrated against the distance
# scale that LBPH_PARAMS in train_model.py produces, and changing either one
# without the other silently rejects every face.
# =====================================================

RECOGNITION_THRESHOLD = settings.recognition_threshold
CONFIRMATION_CONFIDENCE = 52.0

PREDICTION_HISTORY_SIZE = 20
MIN_LABEL_AGREEMENT = 0.85
MIN_CONSECUTIVE_IDENTITY_FRAMES = 8

MAX_WEAK_FRAMES_BEFORE_CLEAR = 3
MAX_CONFIRMED_MISMATCH_FRAMES = 5
LIVENESS_RESET_MISMATCH_FRAMES = 2


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

tracks = {}
track_verification = {}
next_track_id = 0


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
    global tracks
    global track_verification
    global next_track_id

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
    tracks = {}
    track_verification = {}
    next_track_id = 0

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
    global tracks
    global track_verification

    attendance_running = False
    tracks = {}
    track_verification = {}

    if camera_reader is not None:
        camera_reader.stop()
        camera_reader = None

    if cap is not None:
        cap.release()
        cap = None

    logger.info("Attendance ended")


# =====================================================
# TRACKING
# Uses face overlap, center distance, and size similarity
# to reduce identity transfer when faces cross.
# =====================================================


def expire_old_tracks():
    current_time = time.time()

    expired_track_ids = [
        track_id
        for track_id, track_data in tracks.items()
        if (
            current_time
            - track_data["last_seen"]
            > TRACK_TIMEOUT_SECONDS
        )
    ]

    for track_id in expired_track_ids:
        tracks.pop(track_id, None)
        track_verification.pop(track_id, None)


def assign_track_id(box, used_track_ids):
    global next_track_id

    expire_old_tracks()

    center_x, center_y = box_center(box)
    new_area = max(box_area(box), 1)

    best_track_id = None
    best_score = float("-inf")

    for track_id, track_data in tracks.items():
        if track_id in used_track_ids:
            continue

        old_box = track_data["box"]
        old_center_x, old_center_y = box_center(old_box)
        old_area = max(box_area(old_box), 1)

        center_distance = (
            (center_x - old_center_x) ** 2
            + (center_y - old_center_y) ** 2
        ) ** 0.5

        overlap = box_iou(box, old_box)

        size_similarity = (
            min(new_area, old_area)
            / max(new_area, old_area)
        )

        dynamic_distance_limit = max(
            TRACK_MAX_DISTANCE,
            max(
                box[2] - box[0],
                box[3] - box[1]
            ) * 0.85
        )

        if size_similarity < TRACK_MIN_SIZE_SIMILARITY:
            continue

        if (
            overlap < TRACK_MIN_IOU
            and center_distance > dynamic_distance_limit
        ):
            continue

        normalized_distance = (
            center_distance
            / max(dynamic_distance_limit, 1.0)
        )

        score = (
            overlap * 2.0
            + size_similarity
            - normalized_distance
        )

        if score > best_score:
            best_score = score
            best_track_id = track_id

    if best_track_id is None:
        best_track_id = next_track_id
        next_track_id += 1

    tracks[best_track_id] = {
        "box": box,
        "center": (center_x, center_y),
        "last_seen": time.time()
    }

    return best_track_id


# =====================================================
# VERIFICATION STATE
# =====================================================


def create_track_state(valid_face_frames=0):
    return {
        "valid_face_frames": valid_face_frames,
        "history": deque(
            maxlen=PREDICTION_HISTORY_SIZE
        ),
        "consecutive_id": None,
        "consecutive_count": 0,
        "weak_frames": 0,
        "mismatch_frames": 0,
        "student_id": None,
        "student_name": None,
        "confirmed": False,
        "attendance_saved": False,
        "liveness": None
    }


def reset_identity_state(state):
    return create_track_state(
        valid_face_frames=state.get(
            "valid_face_frames",
            REAL_FACE_CONFIRM_FRAMES
        )
    )


def handle_weak_unconfirmed_prediction(state):
    state["weak_frames"] += 1
    state["consecutive_id"] = None
    state["consecutive_count"] = 0

    if (
        state["weak_frames"]
        >= MAX_WEAK_FRAMES_BEFORE_CLEAR
    ):
        state["history"].clear()
        state["weak_frames"] = 0

    return state


def add_prediction_to_history(
    state,
    student_id,
    student_name,
    confidence
):
    state["weak_frames"] = 0

    if state["consecutive_id"] == student_id:
        state["consecutive_count"] += 1
    else:
        state["consecutive_id"] = student_id
        state["consecutive_count"] = 1

    state["history"].append(
        {
            "student_id": student_id,
            "student_name": student_name,
            "confidence": float(confidence)
        }
    )


def evaluate_history(state):
    history_items = list(state["history"])

    if not history_items:
        return None

    label_counts = Counter(
        item["student_id"]
        for item in history_items
    )

    dominant_id, dominant_count = (
        label_counts.most_common(1)[0]
    )

    dominant_items = [
        item
        for item in history_items
        if item["student_id"] == dominant_id
    ]

    agreement_ratio = (
        dominant_count / len(history_items)
    )

    average_confidence = sum(
        item["confidence"]
        for item in dominant_items
    ) / len(dominant_items)

    dominant_name = dominant_items[-1][
        "student_name"
    ]

    return {
        "dominant_id": dominant_id,
        "dominant_name": dominant_name,
        "agreement_ratio": agreement_ratio,
        "average_confidence": average_confidence,
        "history_count": len(history_items),
        "dominant_count": dominant_count
    }


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
    locked_id = state["student_id"]
    locked_name = state["student_name"]

    candidate_agrees = (
        candidate_id is not None
        and candidate_id == locked_id
    )

    if not candidate_agrees:
        state["mismatch_frames"] += 1

        if state.get("liveness") is not None:
            state["liveness"]["post_match_count"] = 0

        if (
            state["mismatch_frames"]
            >= LIVENESS_RESET_MISMATCH_FRAMES
        ):
            state["liveness"] = create_liveness_state()

        if (
            state["mismatch_frames"]
            >= MAX_CONFIRMED_MISMATCH_FRAMES
        ):
            state = reset_identity_state(state)

        return (
            "Unknown",
            "Verifying...",
            "Identity check lost",
            state
        )

    state["mismatch_frames"] = 0

    if state.get("attendance_saved", False):
        return (
            locked_id,
            locked_name,
            "Present",
            state
        )

    state["liveness"] = update_liveness(
        state.get("liveness"),
        current_yaw
    )

    if state["liveness"].get("passed", False):
        state["liveness"]["post_match_count"] = (
            state["liveness"].get(
                "post_match_count",
                0
            ) + 1
        )

    liveness_passed = state["liveness"].get(
        "passed",
        False
    )

    post_match_count = state["liveness"].get(
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
            state["attendance_saved"] = True

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
            state.get("liveness")
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
# VIDEO GENERATOR
# =====================================================


def generate_frames():
    global cap
    global attendance_running
    global track_verification

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

        # Run MediaPipe on a smaller copy to reduce lag.
        # Landmarks are normalized, so they can still be
        # mapped correctly to the original full frame.
        if frame_width > MEDIAPIPE_PROCESS_WIDTH:
            process_scale = (
                MEDIAPIPE_PROCESS_WIDTH
                / float(frame_width)
            )

            process_height = max(
                int(frame_height * process_scale),
                1
            )

            process_frame = cv2.resize(
                frame,
                (
                    MEDIAPIPE_PROCESS_WIDTH,
                    process_height
                ),
                interpolation=cv2.INTER_AREA
            )
        else:
            process_frame = frame

        rgb = cv2.cvtColor(
            process_frame,
            cv2.COLOR_BGR2RGB
        )

        rgb.flags.writeable = False
        results = face_mesh.process(rgb)
        rgb.flags.writeable = True

        used_track_ids = set()
        claimed_student_ids = set()
        displayed_face_count = 0

        if results.multi_face_landmarks:
            for face_landmarks in results.multi_face_landmarks:
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

                track_id = assign_track_id(
                    raw_box,
                    used_track_ids
                )
                used_track_ids.add(track_id)

                state = track_verification.get(
                    track_id,
                    create_track_state()
                )

                state["valid_face_frames"] += 1
                track_verification[track_id] = state

                # Do not draw or recognize a newly detected object
                # until it has remained a geometrically valid face
                # for several consecutive frames.
                if (
                    state["valid_face_frames"]
                    < REAL_FACE_CONFIRM_FRAMES
                ):
                    continue

                displayed_face_count += 1

                x1, y1, x2, y2 = raw_box
                face_width = x2 - x1
                face_height = y2 - y1

                confidence = 999.0
                label = -1
                quality_issue = None

                skip_recognition = (
                    state.get("confirmed", False)
                    and state.get(
                        "attendance_saved", False
                    )
                )

                try:
                    aligned_face = align_face(
                        frame,
                        face_landmarks
                    )

                    quality_issue = get_face_quality_issue(
                        aligned_face
                    )

                    if (
                        quality_issue is None
                        and not skip_recognition
                    ):
                        processed_face = preprocess_for_lbph(
                            aligned_face
                        )

                        label, confidence = recognizer.predict(
                            processed_face
                        )

                        # DEBUG, not INFO: this fires once per tracked face
                        # per frame. At ~15 fps with three faces in shot that
                        # is 45 lines a second, which makes the log useless
                        # for anything else. Set LOG_LEVEL=DEBUG in .env when
                        # you are actually diagnosing recognition.
                        logger.debug(
                            "Track: %s | Label: %s | Distance: %.1f",
                            track_id,
                            label,
                            confidence
                        )

                except Exception:
                    logger.debug(
                        "Recognition failed for track %s",
                        track_id,
                        exc_info=True
                    )
                    quality_issue = "Face alignment failed"

                student_id = "Unknown"
                student_name = "Unknown"
                status = "Unknown"

                strong_prediction = (
                    quality_issue is None
                    and confidence <= RECOGNITION_THRESHOLD
                    and label in label_map
                )

                candidate_id = None
                candidate_name = None

                if strong_prediction:
                    candidate = label_map[label]
                    candidate_id = candidate["student_id"]
                    candidate_name = candidate["name"]

                # IMPORTANT: a confirmed track is immutable.
                # Never let a later LBPH prediction overwrite the
                # identity already locked to this physical track.
                # This prevents one face from changing from Jeff to
                # Julio when the LBPH result fluctuates.
                if state.get("confirmed", False):
                    locked_id = state.get("student_id")
                    locked_name = state.get("student_name")

                    if state.get("attendance_saved", False):
                        student_id = locked_id
                        student_name = locked_name
                        status = "Present"

                        if locked_id:
                            claimed_student_ids.add(locked_id)

                    elif locked_id in claimed_student_ids:
                        student_id = locked_id
                        student_name = locked_name
                        status = "Duplicate identity blocked"

                    else:
                        (
                            student_id,
                            student_name,
                            status,
                            state
                        ) = process_confirmed_track(
                            state,
                            candidate_id,
                            get_face_yaw(face_landmarks),
                            current_subject
                        )

                        if student_id != "Unknown":
                            claimed_student_ids.add(student_id)

                elif quality_issue is not None:
                    state = handle_weak_unconfirmed_prediction(
                        state
                    )
                    student_name = "Face detected"
                    status = quality_issue

                else:
                    # Only an unconfirmed/new track may inherit an
                    # identity that already completed attendance.
                    # A confirmed track above can never be overwritten.
                    already_completed = (
                        candidate_id is not None
                        and candidate_id in recognized
                    )

                    if already_completed:
                        state["student_id"] = candidate_id
                        state["student_name"] = candidate_name
                        state["confirmed"] = True
                        state["attendance_saved"] = True
                        state["mismatch_frames"] = 0
                        state["history"].clear()

                        student_id = candidate_id
                        student_name = candidate_name
                        status = "Present"

                        claimed_student_ids.add(candidate_id)

                    elif strong_prediction:
                        if candidate_id in claimed_student_ids:
                            state = handle_weak_unconfirmed_prediction(
                                state
                            )
                            student_name = "Unknown"
                            status = "Duplicate identity blocked"

                        else:
                            add_prediction_to_history(
                                state,
                                candidate_id,
                                candidate_name,
                                confidence
                            )

                            history_result = evaluate_history(
                                state
                            )

                            student_name = "Verifying..."

                            if history_result is None:
                                status = "Verifying 0/30"

                            else:
                                history_count = history_result[
                                    "history_count"
                                ]
                                agreement_ratio = history_result[
                                    "agreement_ratio"
                                ]
                                average_confidence = history_result[
                                    "average_confidence"
                                ]
                                dominant_id = history_result[
                                    "dominant_id"
                                ]
                                dominant_name = history_result[
                                    "dominant_name"
                                ]

                                enough_history = (
                                    history_count
                                    >= PREDICTION_HISTORY_SIZE
                                )

                                strong_agreement = (
                                    agreement_ratio
                                    >= MIN_LABEL_AGREEMENT
                                )

                                strong_average = (
                                    average_confidence
                                    <= CONFIRMATION_CONFIDENCE
                                )

                                consecutive_match = (
                                    state["consecutive_id"]
                                    == dominant_id
                                    and state["consecutive_count"]
                                    >= MIN_CONSECUTIVE_IDENTITY_FRAMES
                                )

                                can_confirm = (
                                    enough_history
                                    and strong_agreement
                                    and strong_average
                                    and consecutive_match
                                    and dominant_id
                                    not in claimed_student_ids
                                )

                                if can_confirm:
                                    state["student_id"] = dominant_id
                                    state["student_name"] = dominant_name
                                    state["confirmed"] = True
                                    state["attendance_saved"] = False
                                    state["mismatch_frames"] = 0
                                    state["liveness"] = (
                                        create_liveness_state()
                                    )

                                    student_id = dominant_id
                                    student_name = dominant_name
                                    status = get_liveness_message(
                                        state["liveness"]
                                    )

                                    claimed_student_ids.add(
                                        dominant_id
                                    )

                                else:
                                    status = (
                                        f"Verifying {history_count}/"
                                        f"{PREDICTION_HISTORY_SIZE} "
                                        f"{agreement_ratio * 100:.0f}%"
                                    )

                    else:
                        state = handle_weak_unconfirmed_prediction(
                            state
                        )

                        if (
                            face_width < MIN_FACE_WIDTH * 1.25
                            or face_height < MIN_FACE_HEIGHT * 1.25
                        ):
                            student_name = "Face detected"
                            status = "Move closer"
                        else:
                            student_name = "Unknown"
                            status = "Unknown"

                track_verification[track_id] = state

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

        success, buffer = cv2.imencode(
            ".jpg",
            frame,
            [
                int(cv2.IMWRITE_JPEG_QUALITY),
                STREAM_JPEG_QUALITY
            ]
        )

        if not success:
            continue

        frame_bytes = buffer.tobytes()

        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n\r\n"
            + frame_bytes
            + b"\r\n"
        )

    if cap is not None:
        cap.release()
        cap = None
