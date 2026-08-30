import logging
from datetime import datetime, timedelta
from enum import Enum
from typing import NamedTuple

import cv2
import mysql.connector

from camera_utils import open_best_camera
from config.settings import settings
from face_preprocessing import align_face, preprocess_for_lbph
from infra.camera import CameraReader
from infra.db import db_cursor
from security.paths import UnsafeStudentPathError, validate_student_id
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
from vision.validation import (
    RECOGNITION_PROFILE,
    is_frontal,
    is_structurally_a_face,
)

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
                label_text, id_text = line.split(
                    ",",
                    1
                )

                label = int(label_text.strip())

                # `{label},{student_id}`. It was `{label},{id}_{name}` before
                # todo.md §7.5, and validate_student_id refuses an underscore,
                # so an un-migrated labels.txt fails here with the line quoted
                # rather than loading a model whose identities are wrong.
                student_id = validate_student_id(id_text)

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
                    "name": student_id,
                }

                loaded_student_ids.add(
                    student_id
                )

            except (ValueError, UnsafeStudentPathError) as error:
                raise ValueError(
                    "Invalid labels.txt entry on line "
                    f"{line_number}: {line} ({error})"
                ) from error

    if not new_label_map:
        raise ValueError(
            "labels.txt does not contain any students."
        )

    _attach_display_names(new_label_map)

    logger.info(
        "Trainer loaded successfully: %d student identities",
        len(new_label_map)
    )

    return new_recognizer, new_label_map


def _attach_display_names(label_map):
    """
    Fill in the on-screen name for each loaded identity, from `students`.

    ⚠️ **The name is not in labels.txt any more, and this is the reason
    (todo.md §7.5).** It used to come from the dataset folder name, so a
    student corrected from `Jose Cruz` to `José Cruz` kept the old spelling on
    the overlay until somebody retrained - the model was the authority on how
    to spell a person's name, which it has no business being.

    **A failure here is not a failure to load the model.** Recognition works
    without a display name; what it cannot do is start at all if this raises.
    So a database that is down leaves the ID showing - accurate, unhelpful,
    and better than a refused session. The same fallback covers a trained
    student whose row has since been deleted.

    `save_attendance()` already reads the official name from `students` for the
    row it writes. This is that pattern, one layer up.
    """
    try:
        with db_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT student_id, name FROM students WHERE student_id IN ("
                + ",".join(["%s"] * len(label_map))
                + ")",
                tuple(entry["student_id"] for entry in label_map.values()),
            )

            names = {row["student_id"]: row["name"] for row in cursor.fetchall()}

    except Exception:
        logger.exception(
            "Could not read student names; the overlay will show IDs instead"
        )
        return

    missing = []

    for entry in label_map.values():
        name = names.get(entry["student_id"])

        if name:
            entry["name"] = name
        else:
            missing.append(entry["student_id"])

    if missing:
        logger.warning(
            "%d trained student(s) have no row in `students`: %s. They will "
            "be shown by ID and refused at save_attendance().",
            len(missing),
            ", ".join(missing),
        )


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
    """
    A MediaPipe FaceMesh for one session.

    The construction moved to infra/facemesh.py when browser enrolment needed
    one too, so there is one place that knows how to build a FaceMesh and one
    place carrying the AttributeError fallback for MediaPipe's two module
    layouts. The settings are unchanged and still named here by their profile.
    The deferred import survives the move - see that module on why.
    """
    from infra.facemesh import make_recognition_detector

    return make_recognition_detector()


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
# The values are unchanged apart from confirmation_confidence, which is now
# derived rather than restated - see below. Like the geometry profiles, they
# are the "safe starting values" the original comment described and have never
# been calibrated; Phase 6 is where that happens.
# =====================================================


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
#
# Defined above TRACK_CONFIG because the confirmation bar is derived from it.
# =====================================================

RECOGNITION_THRESHOLD = settings.recognition_threshold


TRACK_CONFIG = TrackConfig(
    timeout_seconds=1.5,
    max_distance=180.0,
    min_iou=0.08,
    min_size_similarity=0.40,
    real_face_confirm_frames=5,
    prediction_history_size=20,
    min_label_agreement=0.85,
    min_consecutive_identity_frames=8,
    # ⚠️ This was a separate literal 52.0, and that was a live bug.
    #
    # A frame's prediction is voted into the window when its distance is at or
    # under RECOGNITION_THRESHOLD (58.0). The window's *average* then had to
    # beat 52.0 to lock the identity. So any student whose live distance
    # settled between the two produced a full, unanimous, 20/20 window that
    # could never confirm: the box stayed yellow, the overlay read
    # "Verifying 20/20 100%" forever, nothing was logged and no attendance was
    # saved. Reported from a live run at distance 56.1, squarely in the band.
    #
    # This is lessons.md L2 again - two thresholds on one metric scale, tuned
    # independently, and the stricter one silently swallowing a band of
    # legitimate matches. The fix is to have one number, not two.
    #
    # Measured before changing it, against this model (docs/benchmarks.md §8):
    # 750 impostor images from 50 unenrolled Georgia Tech subjects score
    # 62.2 at the very closest, and the closest subject mean is 69.9. Genuine
    # same-session crops top out at 34.9. Nothing at all lies between 52.0 and
    # 58.0 in either population, so raising the bar costs zero measured false
    # acceptances and leaves 4.2 of headroom to the nearest impostor frame.
    #
    # Phase 6 may reintroduce a stricter confirmation bar than the match
    # threshold - but from a DET curve, with the gap justified, and with the
    # unreachable-band failure made impossible by the diagnostics below.
    confirmation_confidence=RECOGNITION_THRESHOLD,
    max_weak_frames_before_clear=3,
    max_confirmed_mismatch_frames=5,
    liveness_reset_mismatch_frames=2,
)

REAL_FACE_CONFIRM_FRAMES = TRACK_CONFIG.real_face_confirm_frames
PREDICTION_HISTORY_SIZE = TRACK_CONFIG.prediction_history_size


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
# capture_dataset.py. They now come from vision/, which this module imports -
# and since Phase 5 deleted capture_dataset.py, vision/enrolment.py is the
# other consumer, using the enrolment profile beside the recognition one.
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
    """
    Is this a face worth tracking? **Structure only - not frontality.**

    ⚠️ It used to be `is_valid_face_candidate()`, which also applies the
    profile's two frontality checks, and that is what made the liveness
    challenge unpassable. This gate runs in `_stream_frames()` *before*
    `tracker.assign()`, so refusing a turned head here does not merely skip a
    recognition attempt - it drops the frame entirely, which means the track's
    `last_seen` is never refreshed and `expire_old_tracks()` deletes it 1.5 s
    later. A student obeying "Turn LEFT" was therefore deleted for obeying,
    and came back to "Verifying 0/20".

    Frontality still decides whether an identity may be *confirmed* - see
    `face_is_frontal()` and `vision/validation.frontality_reason()`.
    """
    return is_structurally_a_face(
        face_landmarks,
        frame_width,
        frame_height,
        box,
        RECOGNITION_PROFILE
    )


def face_is_frontal(
    face_landmarks,
    frame_width,
    frame_height,
    box
):
    """
    Is this face square-on enough to make a decision about?

    Gates confirmation, and nothing else. An attendance mark is a decision and
    a decision wants a frontal face - that property is unchanged. What changed
    is that a face failing it is still seen, still boxed, and still keeps its
    place in a liveness challenge.
    """
    return is_frontal(
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


class ActiveSubject(NamedTuple):
    """
    Which offering an attendance session is recording, and under which session.

    `subject_code` is carried for logging and for the operator's screen only.
    Every write keys on `subject_id`, because a subjects row is a section
    offering and two sections can share a code (see migration 004).
    """

    subject_id: int
    session_id: int | None
    subject_code: str


def derive_status(arrival, scheduled_start):
    """
    "Present" or "Late", from the subject's scheduled start time (FS-8).

    Everything this system has ever recorded is "Present": `subjects.time_in`
    was a VARCHAR while `attendance.time_in` is a TIME, so there was nothing
    sound to compare. Migration 005 made it a real TIME.

    A subject with no scheduled start cannot make anyone late, so the answer is
    Present - that is a statement about missing data, not punctuality.
    """
    if scheduled_start is None:
        return "Present"

    # MySQL hands back a TIME as a timedelta from midnight.
    if isinstance(scheduled_start, timedelta):
        scheduled = (datetime.min + scheduled_start).time()
    else:
        scheduled = scheduled_start

    return "Late" if arrival > scheduled else "Present"


class NotRecorded(Enum):
    """
    Why a recognised face produced no attendance row, and what to show.

    ⚠️ **These were one `None` and one orange "Not Enrolled" on the overlay**
    (US-10). Three different situations rendered identically: a student who is
    not in this class, a face the model knows that the database does not, and
    the database being unreachable. Only the first is a roster problem, and it
    is the only one the operator can act on at the kiosk - so a MySQL outage
    looked like somebody had forgotten to add a student to a class.

    **Falsy on purpose.** `save_attendance()` returns the written status or one
    of these, and every existing caller tests the result for truth. Keeping
    them false means the reason can be carried without any call site having to
    learn about it first.
    """

    NOT_IN_CLASS = "Not in this class"
    UNKNOWN_STUDENT = "Not enrolled"
    ERROR = "Not recorded - system error"

    def __bool__(self):
        return False


def save_attendance(student_id, subject):
    """
    Record one student as present for one offering, once.

    `subject` is an ActiveSubject. Returns the status that was written -
    "Present" or "Late" - or None if nothing was recorded.

    ⚠️ **There is deliberately no `status` parameter.** There used to be one,
    defaulting to None and meaning "work it out from the schedule", added so a
    correction could force a value. The recognition path was then given one
    too:

        save_attendance(locked_id, subject, "Present")

    and since the function resolved `status or derive_status(...)`, that
    literal short-circuited the derivation for the only production caller there
    is. Every row the system wrote was Present whatever the subject's scheduled
    start, and /reports rendered a Late counter that was structurally 0 - the
    exact shape of FS-4, which Phase 4 existed to remove. FS-8 was closed in
    Phase 4 and quietly reopened by the argument.

    The parameter is gone rather than merely unused at the call site. Its
    claimed consumer, the FS-10 correction route, does not go through this
    function at all - it writes through `repositories.attendance.set_status()`,
    which is where a forced status belongs, beside the audit row that explains
    it. With no parameter the defect is unrepresentable, which is worth more
    than a test forbidding it.

    **The return value is the status rather than True**, so the overlay can
    show what was actually recorded. A student marked Late on the register and
    "Present" on the screen is two screens disagreeing about one session, which
    is FS-7 one layer up.

    Three things changed with the Phase 4 schema, and each removes a defect:

    **RE-3 - the read-then-write is gone.** Duplicate suppression used to be a
    SELECT followed by an INSERT, a TOCTOU race under concurrent recognition
    that a comment in this function acknowledged. There is now a UNIQUE index
    on (student_id, subject_id, attendance_date) and the write is an
    INSERT ... ON DUPLICATE KEY UPDATE, so the database decides. The old check
    is deleted rather than kept alongside it, as the Phase 3 handover asked.

    ⚠️ **The exception handling matters more than it looks.** This function
    used to wrap everything in `except mysql.connector.Error: return False`.
    IntegrityError is a subclass of that, so once the UNIQUE index existed a
    perfectly ordinary duplicate would have been read as failure - the caller
    renders a falsy result as "Not Enrolled", never sets attendance_saved, and
    retries on every frame. ON DUPLICATE KEY avoids raising at all, and the
    handler below no longer has to distinguish them.

    **FS-3 - enrolment is checked against `enrolments`, not `students`.** Being
    in the database is not being in the class. Without this, any recognised
    face is recorded against whatever subject happens to be running.

    **FS-8 - Late is derived** from the subject's scheduled start.
    """
    try:
        with db_cursor(dictionary=True, commit=True) as cursor:
            cursor.execute(
                """
                SELECT s.student_id, s.name, sub.time_in AS scheduled_start
                FROM students s
                JOIN enrolments e
                    ON e.student_id = s.student_id
                JOIN subjects sub
                    ON sub.id = e.subject_id
                WHERE s.student_id = %s
                AND e.subject_id = %s
                """,
                (student_id, subject.subject_id)
            )

            enrolled = cursor.fetchone()

            if enrolled is None:
                # ⚠️ **Two causes, and the operator needs them apart** (US-10).
                # The log always said which; the screen never did, so "add them
                # to the class list" and "the model is ahead of the database"
                # looked the same to the person standing at the kiosk.
                #
                # The second query runs only on this branch - once per
                # confirmed identity, not per frame - so the hot path above is
                # unchanged.
                cursor.execute(
                    "SELECT student_id FROM students WHERE student_id = %s",
                    (student_id,)
                )

                known = cursor.fetchone() is not None

                logger.warning(
                    "Rejected attendance: %s is %s (subject %s / %s)",
                    student_id,
                    "not in this class" if known
                    else "not in the students table",
                    subject.subject_id,
                    subject.subject_code,
                )

                return (
                    NotRecorded.NOT_IN_CLASS if known
                    else NotRecorded.UNKNOWN_STUDENT
                )

            # One reading, used for the row and for the Late decision. The old
            # code called datetime.now() three separate times, so a write
            # crossing midnight could compare one date and store another.
            now = datetime.now()

            resolved = derive_status(now.time(), enrolled["scheduled_start"])

            cursor.execute(
                """
                INSERT INTO attendance
                (
                    student_id,
                    subject_id,
                    session_id,
                    attendance_date,
                    time_in,
                    status
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE id = id
                """,
                (
                    student_id,
                    subject.subject_id,
                    subject.session_id,
                    now.date(),
                    now.strftime("%H:%M:%S"),
                    resolved,
                )
            )

            if cursor.rowcount == 0:
                logger.info(
                    "Attendance already recorded for %s", student_id
                )
            else:
                logger.info(
                    "Attendance recorded: %s - %s (%s)",
                    student_id,
                    enrolled["name"],
                    resolved,
                )

        return resolved

    except mysql.connector.Error:
        logger.exception("Database error while saving attendance")

        # Not a roster problem, and it must not read as one on the overlay.
        return NotRecorded.ERROR

# =====================================================
# CAMERA CONTROL
# =====================================================


def start_camera(subject):
    """
    Begin an attendance session. True if recognition can now run.

    `subject` is an ActiveSubject rather than the free-text code it used to be.
    It is a NamedTuple, so the session's "is this the same subject?" comparison
    still works by value - but the identity it carries is now `subject_id`,
    which is what every attendance write keys on.

    Thin: the model reload, the camera open, the tracker reset and the
    "already running" decision all belong to the session, which holds the lock
    that makes them one atomic transition (RE-2).
    """
    return session.start(subject)


def stop_camera():
    """End the session and release the camera."""
    session.stop()


# ⚠️ `attendance_is_running()` used to sit here and is gone (Phase 5).
#
# It existed for one caller: the enrolment routes asked it before spawning
# `capture_dataset.py`, because that subprocess opened the *same physical
# device* this session holds and would otherwise fail with a bare read error.
# Browser enrolment uses the operator's camera through getUserMedia, so the
# contention it guarded against no longer exists. `session.is_running` remains
# for anything that genuinely needs to know.


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
#
# ⚠️ "Still agrees" has two opposites and they are NOT
# the same evidence. Reading a *different* enrolled
# student is a contradiction and the thing this guard
# exists to catch. Reading *nothing usable* - a blurred
# crop mid-turn, a quality rejection, a distance over
# the threshold - says nothing about identity at all.
# Treating them alike is what made the liveness
# challenge unpassable; see the two branches below.
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

    # A different enrolled student on a locked track. Evidence of a track
    # switch - somebody else's face taking over a challenge already in
    # progress - so this stays exactly as strict as it was: the sequence is
    # redrawn after two such frames and the identity is dropped after five.
    if candidate_id is not None and candidate_id != locked_id:
        state.mismatch_frames += 1
        state.unreadable_frames = 0

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
            logger.warning(
                "Dropped the locked identity %s after %d frames reading a "
                "different student (%s). The track will start over.",
                locked_id,
                state.mismatch_frames,
                candidate_id,
            )
            state = state.reset_identity()

        return (
            "Unknown",
            "Verifying...",
            "Identity check lost",
            state
        )

    # No usable prediction. Absence of evidence, and on the frames the
    # challenge itself produces: a head turned to satisfy "Turn LEFT" is
    # blurred while it moves and its crop is what the quality gate is most
    # likely to refuse. The sequence is NOT redrawn - putting a fresh random
    # prompt on screen while the student is still performing the last one is
    # the behaviour that made this look like the system had lost them - and
    # the identity survives far longer, because nothing here contradicts it.
    if candidate_id is None:
        state.unreadable_frames += 1

        if state.liveness is not None:
            state.liveness.note_identity_mismatch()

            # ⚠️ **The challenge advances here, on a frame with no identity.**
            #
            # The paragraph above stopped the sequence being *redrawn* on
            # these frames. It did not let it move *forward* on them, and the
            # branch returns below - so `update()` was only ever reached from
            # the readable path, and the challenge could only advance on
            # frames where LBPH produced a match.
            #
            # That is the wrong precondition, because it is the requested
            # movement itself that destroys the match: a turning head is
            # motion-blurred, and a blurred crop is what
            # RECOGNITION_QUALITY.min_blur_variance refuses; a profile view
            # also pushes the distance past RECOGNITION_THRESHOLD. So the
            # system asked for a turn and then discarded exactly the frames
            # the turn produced, while the 8 s step timeout ran on wall-clock
            # regardless. Measured live: 4 min 07 s to record one student.
            #
            # The yaw owes LBPH nothing. get_face_yaw() reads the FaceMesh
            # landmarks, which are present whenever MediaPipe found a face -
            # the precondition for being in this function at all.
            #
            # ⚠️ This does not recognise anybody. Completing the sequence sets
            # `passed`; attendance additionally needs post_match_frames of
            # *real* matches, and note_identity_mismatch() above keeps that
            # counter at zero for every frame that reaches this branch. The
            # challenge proves a live person moved on cue; the re-check proves
            # it was still this student. Those stay separate.
            state.liveness.update(current_yaw)

        if (
            state.unreadable_frames
            >= TRACK_CONFIG.max_unreadable_frames_before_clear
        ):
            logger.warning(
                "Dropped the locked identity %s after %d consecutive frames "
                "with no usable match. The face was probably lost rather than "
                "replaced.",
                locked_id,
                state.unreadable_frames,
            )
            state = state.reset_identity()

            return (
                "Unknown",
                "Verifying...",
                "Identity check lost",
                state
            )

        # The identity is held, so the student keeps their name and their
        # place in the sequence. The status says what the system needs rather
        # than reporting a failure at them.
        return (
            locked_id,
            locked_name,
            "Hold still - looking for your face",
            state
        )

    state.mismatch_frames = 0
    state.unreadable_frames = 0

    if state.attendance_saved:
        return (
            locked_id,
            locked_name,
            state.recorded_status,
            state
        )

    if state.liveness is None:
        state.liveness = create_liveness_state()

    # A step that times out redraws the sequence inside update(), silently -
    # the operator sees the prompt change and has no way to tell that from
    # having completed a step. Logged at the transition, not per frame, the
    # same way describe_confirmation_block() reports a stuck track.
    #
    # This is the instrument that did not exist: the only account of this
    # defect was a person watching the screen, because none of the three ways
    # a challenge can stall wrote a line anywhere.
    restarts_before = state.liveness.restarts
    step_before = state.liveness.current_step

    state.liveness.update(current_yaw)

    if state.liveness.restarts > restarts_before:
        logger.warning(
            "Liveness challenge for %s timed out on step %s after %.0f s and "
            "was redrawn (restart %d). The student is now being asked for a "
            "different sequence.",
            locked_id,
            step_before,
            LIVENESS_CONFIG.step_timeout_seconds,
            state.liveness.restarts,
        )

    state.liveness.note_identity_match()

    if state.liveness.identity_reconfirmed:
        # ⚠️ No status argument. Passing "Present" here is what made FS-8
        # unreachable in production - see save_attendance(). The schedule
        # decides, and what it decided is what goes on the overlay.
        recorded = save_attendance(locked_id, subject)

        if recorded:
            context.recognized.add(locked_id)
            state.attendance_saved = True
            state.attendance_status = recorded

            return (
                locked_id,
                locked_name,
                recorded,
                state
            )

        # `recorded` is a NotRecorded here - falsy, and carrying which of the
        # three things went wrong (US-10).
        return (
            locked_id,
            locked_name,
            recorded.value,
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

    # ⚠️ Late is *recorded*, not refused, and the colour has to say so. Without
    # this branch it falls through to the final `return 0, 0, 255` - red, the
    # same box a rejected face gets - so the first student the system ever
    # marked Late would have been told on screen that recognition had failed.
    # Amber: distinct from the green of an on-time mark, and distinct from the
    # orange (0, 165, 255) used for "Not Enrolled" and the image-quality hints.
    if status == "Late":
        return 0, 215, 255

    if status.startswith("Verifying"):
        return 0, 255, 255

    if (
        status.startswith("Liveness")
        or status == "Preparing liveness..."
    ):
        return 255, 255, 0

    # ⚠️ Actionable at the kiosk - orange, the colour the other hints use. The
    # student is standing there and somebody can fix a class list.
    if status in (
        NotRecorded.NOT_IN_CLASS.value,
        NotRecorded.UNKNOWN_STUDENT.value,
    ):
        return 0, 165, 255

    # ⚠️ **Red, and deliberately not orange.** This one is not a roster
    # problem: the database was unreachable and nothing was written. Showing it
    # in the same colour as "add them to the class list" is what US-10 was
    # about - an outage that looks like paperwork does not get reported as an
    # outage.
    if status == NotRecorded.ERROR.value:
        return 0, 0, 255

    # ⚠️ Both of these are *instructions to a person the system has not given
    # up on*, and neither may be red. Falling through to red is the FS-7 shape
    # the Late branch above exists to prevent: "Look at the camera" in the
    # same colour as a rejection tells the student recognition failed, at the
    # exact moment they need to believe it is still working.
    #
    # "Hold still - looking for your face" is drawn on a track whose identity
    # is still locked and whose liveness progress is intact - see
    # process_confirmed_track(). Orange, the colour the other actionable hints
    # already use.
    if (
        status.startswith("Hold still")
        or status == "Look at the camera"
        or "Move closer" in status
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
    frame_height,
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

    # ⚠️ **An unconfirmed track makes no identity decision on a face that is
    # not square-on**, and this is where the frontality check went when it was
    # taken out of the tracking gate (see face_passes_geometry_gate).
    #
    # It has to sit above both branches below, not just the accumulation:
    # adopting a completed identity re-locks the track and puts a mark on the
    # screen, which is as much a decision as confirming one. What the two
    # positions have in common is that the answer is about *who this is*, and
    # this system does not answer that from a profile view.
    #
    # The face is still tracked, still boxed and still told what to do - which
    # is the whole difference from dropping the frame.
    if not face_is_frontal(face_landmarks, frame_width, frame_height, box):
        state.note_weak_prediction()
        return "Unknown", "Face detected", "Look at the camera", state

    # Only an unconfirmed track may inherit an identity that already
    # completed attendance. A confirmed track above can never be overwritten.
    if candidate_id is not None and candidate_id in context.recognized:
        state.adopt_completed_identity(candidate_id, candidate_name)
        claimed_student_ids.add(candidate_id)
        return candidate_id, candidate_name, state.recorded_status, state

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
        return locked_id, locked_name, state.recorded_status, state

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


def describe_confirmation_block(blocker, verdict, first_time):
    """
    What to put under the box while a track is not confirming.

    "Verifying 20/20 100%" is only useful while the numbers are still moving.
    Once the window is full and agreeing, repeating it tells the operator
    nothing about why nothing is happening - which is exactly the state a
    student stood in while the confirmation bar sat below the recognition
    threshold. These strings say which condition is unmet, and the two that a
    person can actually do something about say what to do.

    `distance` in particular is logged as well as drawn: it is the one that can
    persist indefinitely without the picture looking wrong. `first_time` is
    what keeps that to one line per stuck track instead of one per frame - at
    roughly 7 fps the useful message would otherwise be the thing that buried
    itself.
    """
    if blocker == "window":
        return f"Verifying {verdict.history_count}/{PREDICTION_HISTORY_SIZE}"

    if blocker == "agreement":
        return (
            f"Verifying {verdict.history_count}/{PREDICTION_HISTORY_SIZE} "
            f"{verdict.agreement_ratio * 100:.0f}%"
        )

    if blocker == "distance":
        if first_time:
            logger.warning(
            "Track will not confirm: %s matched %d/%d frames at %.0f%% "
            "agreement, but the average distance %.1f is above the "
                "confirmation bar %.1f. Nothing will be recorded for this "
                "student until the match improves.",
                verdict.dominant_id,
                verdict.dominant_count,
                verdict.history_count,
                verdict.agreement_ratio * 100,
                verdict.average_confidence,
                TRACK_CONFIG.confirmation_confidence,
            )
        return "Match too weak - move closer / more light"

    if blocker == "consecutive":
        return "Hold still"

    if blocker == "claimed":
        return "Already recorded this session"

    return "Verifying..."


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

    blocker = state.confirmation_blocker(verdict, claimed_student_ids)

    if blocker is not None:
        first_time = state.last_blocker != blocker
        state.last_blocker = blocker

        return (
            "Unknown",
            "Verifying...",
            describe_confirmation_block(blocker, verdict, first_time),
            state
        )

    state.last_blocker = None

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
        for part in _stream_frames():
            # The heartbeat that lets an abandoned slot be reclaimed. The
            # `finally` below is the tidy path and it is not reliable: a
            # generator's `finally` runs when it is closed or collected, and
            # for an MJPEG response whose browser has navigated away that is
            # not prompt. Without this, the slot stayed held and the next
            # `/video_feed` was refused with "already open in another window" -
            # the relogin lockout.
            session.note_viewer_frame(token)
            yield part
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
                frame_height=frame_height,
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
