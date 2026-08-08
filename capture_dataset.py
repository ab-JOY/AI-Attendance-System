import ctypes
import logging
import os
import sys
import time

import cv2
import mediapipe as mp
import numpy as np

from camera_utils import (
    get_available_cameras,
    get_saved_camera_index,
    open_best_camera,
    open_camera_by_index,
    save_camera_index,
)
from config.exit_codes import EXIT_CANCELLED, EXIT_FAILURE, EXIT_SUCCESS
from config.logging_config import configure_logging
from security.paths import UnsafeStudentPathError, student_dataset_path
from vision.geometry import box_to_xywh, get_face_box
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
from vision.quality import ENROLMENT_QUALITY, quality_issue
from vision.validation import ENROLMENT_PROFILE, is_valid_face_candidate

# This module runs as a subprocess launched by app.py, so it is an entry point
# and owns logging configuration for its own process. app.py branches on the
# exit code only and never reads this process's stdout, so routing output
# through logging cannot affect enrolment.
#
# Everything here executes at module scope with no main() guard - that is
# MA-2, scoped to Phase 5. Importing this module runs a camera capture
# session; do not import it to inspect it.
configure_logging()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# EXIT CODES - FS-12
#
# Every failure path below used to be a bare `sys.exit()`, which exits with
# status 0. app.py reads 0 as a completed enrolment and retrains the model,
# so a missing dependency, a bad argument or an unopenable camera was
# reported to the operator as success for a student who had no dataset.
#
# The contract now lives in config/exit_codes.py and both sides import it.
# Anything that is not a complete capture must exit non-zero.
# ---------------------------------------------------------------------------

try:
    from face_preprocessing import align_face

except ImportError:
    logger.critical(
        "face_preprocessing.py could not be imported. Place it beside "
        "capture_dataset.py.",
        exc_info=True
    )
    sys.exit(EXIT_FAILURE)


# =====================================================
# GET STUDENT INFORMATION FROM FLASK
# =====================================================

if len(sys.argv) < 3:
    logger.error(
        "Usage: python capture_dataset.py <student_id> <student_name>"
    )
    sys.exit(EXIT_FAILURE)

student_id = sys.argv[1].strip()
student_name = sys.argv[2].strip()
preferred_camera_idx = sys.argv[3].strip() if len(sys.argv) > 3 else None

if not student_id or not student_name:
    logger.error("Student ID and name are required")
    sys.exit(EXIT_FAILURE)


# =====================================================
# DATASET DIRECTORY
# =====================================================
#
# SE-3. This used to be a bare f-string joined onto the dataset directory,
# with the two values coming - through app.py and a subprocess call - from an
# unvalidated web form. The same helper that app.py uses builds the path here,
# so enrolment and management cannot disagree about where a student's folder
# is, and neither can be talked into a path outside dataset/.
#
# The check runs before os.makedirs, which matters beyond the traversal: an
# empty dataset/{id}_{name} left behind by a failed run is what produces the
# dangling folders behind FS-2 and FS-9. See tests/test_capture_exit_codes.py.

try:
    dataset_path = str(student_dataset_path(student_id, student_name))

except UnsafeStudentPathError as error:
    logger.error("Refusing to capture into an unsafe dataset path: %s", error)
    sys.exit(EXIT_FAILURE)

os.makedirs(
    dataset_path,
    exist_ok=True
)


# =====================================================
# WINDOW SETTINGS
# =====================================================

WINDOW_NAME = "Capture Dataset"
WINDOW_WIDTH = 1200
WINDOW_HEIGHT = 800

try:
    user32 = ctypes.windll.user32
    screen_width = user32.GetSystemMetrics(0)
    screen_height = user32.GetSystemMetrics(1)
except Exception:
    screen_width = 1920
    screen_height = 1080

window_x = max(
    (screen_width - WINDOW_WIDTH) // 2,
    0
)

window_y = max(
    (screen_height - WINDOW_HEIGHT) // 2,
    0
)

cv2.namedWindow(
    WINDOW_NAME,
    cv2.WINDOW_NORMAL
)

cv2.resizeWindow(
    WINDOW_NAME,
    WINDOW_WIDTH,
    WINDOW_HEIGHT
)

cv2.moveWindow(
    WINDOW_NAME,
    window_x,
    window_y
)

loading_screen = np.zeros(
    (
        WINDOW_HEIGHT,
        WINDOW_WIDTH,
        3
    ),
    dtype=np.uint8
)

cv2.putText(
    loading_screen,
    "Opening Camera...",
    (340, 400),
    cv2.FONT_HERSHEY_SIMPLEX,
    1.5,
    (255, 255, 255),
    3
)

cv2.imshow(
    WINDOW_NAME,
    loading_screen
)

cv2.waitKey(1)


# =====================================================
# MEDIAPIPE INITIALIZATION
# =====================================================

try:
    mp_face_mesh = mp.solutions.face_mesh
    mp_draw = mp.solutions.drawing_utils
except AttributeError:
    import mediapipe.python.solutions.drawing_utils as mp_draw
    import mediapipe.python.solutions.face_mesh as mp_face_mesh

face_mesh = mp_face_mesh.FaceMesh(
    static_image_mode=False,
    max_num_faces=2,
    refine_landmarks=True,
    min_detection_confidence=0.55,
    min_tracking_confidence=0.50
)

logger.info("MediaPipe Face Mesh loaded")


# =====================================================
# OPEN CAMERA
# =====================================================

cap = open_best_camera(preferred_index=preferred_camera_idx)

if cap is None or not cap.isOpened():
    logger.error("Cannot open any camera")

    face_mesh.close()
    cv2.destroyAllWindows()
    sys.exit(EXIT_FAILURE)

current_camera_idx = get_saved_camera_index()
if current_camera_idx is None:
    current_camera_idx = 0

cap.set(
    cv2.CAP_PROP_FRAME_WIDTH,
    1920
)

cap.set(
    cv2.CAP_PROP_FRAME_HEIGHT,
    1080
)

cap.set(
    cv2.CAP_PROP_BUFFERSIZE,
    1
)

actual_width = int(
    cap.get(
        cv2.CAP_PROP_FRAME_WIDTH
    )
)

actual_height = int(
    cap.get(
        cv2.CAP_PROP_FRAME_HEIGHT
    )
)

logger.info(
    "Camera resolution: %sx%s",
    actual_width,
    actual_height
)

logger.info(
    "Dataset capture starting for student %s (%s)",
    student_id,
    student_name
)


# =====================================================
# DATASET DISTRIBUTION
# One enrollment session, 100 images
# =====================================================

STRAIGHT_IMAGES = 25
LEFT_IMAGES = 15
RIGHT_IMAGES = 15
UP_IMAGES = 10
DOWN_IMAGES = 10
SMILE_IMAGES = 10
CLOSE_IMAGES = 5
MEDIUM_IMAGES = 5
FAR_IMAGES = 5

MAX_IMAGES = (
    STRAIGHT_IMAGES
    + LEFT_IMAGES
    + RIGHT_IMAGES
    + UP_IMAGES
    + DOWN_IMAGES
    + SMILE_IMAGES
    + CLOSE_IMAGES
    + MEDIUM_IMAGES
    + FAR_IMAGES
)


# =====================================================
# CAPTURE SETTINGS
# =====================================================

CAPTURE_DELAY = 0.80

MIN_FACE_RATIO = 0.012
MAX_FACE_RATIO = 0.55

CLOSE_MIN_RATIO = 0.18
CLOSE_MAX_RATIO = 0.32

MEDIUM_MIN_RATIO = 0.07
MEDIUM_MAX_RATIO = 0.17

FAR_MIN_RATIO = 0.012
FAR_MAX_RATIO = 0.075

# The face-geometry and image-quality thresholds that used to sit here are
# now ENROLMENT_PROFILE and ENROLMENT_QUALITY in vision/ (MA-4). The values
# are unchanged; what changed is that recognition's differing values are now
# visible beside them instead of 1,400 lines away in another file.
MIN_FACE_WIDTH = ENROLMENT_PROFILE.min_face_width
MIN_FACE_HEIGHT = ENROLMENT_PROFILE.min_face_height

CENTER_TOLERANCE = 180

POSE_HOLD_FRAMES = 6
DISTANCE_HOLD_FRAMES = 3


# =====================================================
# MEDIAPIPE LANDMARK INDICES
#
# From vision/landmarks.py, which is now the only place a mesh index is
# written down. The two aliases keep the pose and expression code below
# reading the way it did; LEFT_EYE here has always meant the outer corner.
# =====================================================

LEFT_EYE = LEFT_EYE_OUTER
RIGHT_EYE = RIGHT_EYE_OUTER


# =====================================================
# CAPTURE STATE
# =====================================================

count = 0
last_capture = 0

last_pose = "UNKNOWN"
pose_frame_count = 0


# =====================================================
# FACE BOX AND VALIDATION (MA-4)
#
# Both functions used to be defined here in full, and again - with different
# thresholds - in recognize_face.py. They now come from vision/, which both
# modules import. The wrappers bind the enrolment profile once and keep the
# (x, y, width, height) shape the capture loop below expects.
#
# ENROLMENT_PROFILE is deliberately more pose-tolerant than the recognition
# profile: 50 of the MAX_IMAGES captured here are LEFT/RIGHT/UP/DOWN by
# design, and recognition's frontal-only nose rule would reject all of them.
# See vision/validation.py.
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
        ENROLMENT_PROFILE
    )


# =====================================================
# SHARED FACE ALIGNMENT
# =====================================================

def prepare_dataset_face(
    frame,
    face_landmarks
):
    """
    Saves an aligned grayscale face.

    CLAHE is intentionally not applied here.
    train_model.py and recognize_face.py will apply
    the shared LBPH preprocessing exactly once.
    """

    aligned_face = align_face(
        frame,
        face_landmarks
    )

    if (
        aligned_face is None
        or aligned_face.size == 0
    ):
        raise ValueError(
            "Aligned face is empty."
        )

    if len(
        aligned_face.shape
    ) == 3:

        aligned_face = cv2.cvtColor(
            aligned_face,
            cv2.COLOR_BGR2GRAY
        )

    if (
        aligned_face.dtype
        != np.uint8
    ):

        aligned_face = np.clip(
            aligned_face,
            0,
            255
        ).astype(
            np.uint8
        )

    return aligned_face


# =====================================================
# IMAGE QUALITY
# =====================================================

def get_image_quality_issue(face):
    # Was a full second copy of the recognition quality gate, four thresholds
    # adrift from it (MA-4). ENROLMENT_QUALITY carries this stage's numbers
    # and its exact on-screen wording, so the overlay reads as it always did.
    return quality_issue(
        face,
        ENROLMENT_QUALITY
    )


# =====================================================
# FACE SIZE AND POSITION
# =====================================================

def calculate_face_ratio(
    frame_width,
    frame_height,
    width,
    height
):

    return (
        width * height
    ) / (
        frame_width
        * frame_height
    )


def good_distance(
    frame_width,
    frame_height,
    width,
    height
):

    face_ratio = calculate_face_ratio(
        frame_width,
        frame_height,
        width,
        height
    )

    return (
        MIN_FACE_RATIO
        <= face_ratio
        <= MAX_FACE_RATIO
    )


def face_centered(
    frame_width,
    frame_height,
    x,
    y,
    width,
    height
):

    face_center_x = (
        x + width // 2
    )

    face_center_y = (
        y + height // 2
    )

    frame_center_x = (
        frame_width // 2
    )

    frame_center_y = (
        frame_height // 2
    )

    return (
        abs(
            face_center_x
            - frame_center_x
        )
        <= CENTER_TOLERANCE
        and
        abs(
            face_center_y
            - frame_center_y
        )
        <= CENTER_TOLERANCE
    )


# =====================================================
# CAPTURE STAGE
# =====================================================

def required_pose(
    image_count
):

    straight_end = (
        STRAIGHT_IMAGES
    )

    left_end = (
        straight_end
        + LEFT_IMAGES
    )

    right_end = (
        left_end
        + RIGHT_IMAGES
    )

    up_end = (
        right_end
        + UP_IMAGES
    )

    down_end = (
        up_end
        + DOWN_IMAGES
    )

    smile_end = (
        down_end
        + SMILE_IMAGES
    )

    close_end = (
        smile_end
        + CLOSE_IMAGES
    )

    medium_end = (
        close_end
        + MEDIUM_IMAGES
    )

    if image_count < straight_end:
        return "STRAIGHT"

    if image_count < left_end:
        return "LEFT"

    if image_count < right_end:
        return "RIGHT"

    if image_count < up_end:
        return "UP"

    if image_count < down_end:
        return "DOWN"

    if image_count < smile_end:
        return "SMILE"

    if image_count < close_end:
        return "CLOSE"

    if image_count < medium_end:
        return "MEDIUM"

    return "FAR"


def get_instruction(
    image_count
):

    pose = required_pose(
        image_count
    )

    instructions = {
        "STRAIGHT": "LOOK STRAIGHT",
        "LEFT": "TURN SLIGHTLY LEFT",
        "RIGHT": "TURN SLIGHTLY RIGHT",
        "UP": "LOOK SLIGHTLY UP",
        "DOWN": "LOOK SLIGHTLY DOWN",
        "SMILE": "SMILE",
        "CLOSE": "MOVE CLOSER",
        "MEDIUM": (
            "MOVE TO MEDIUM DISTANCE"
        ),
        "FAR": "MOVE FARTHER"
    }

    return instructions.get(
        pose,
        "LOOK AT THE CAMERA"
    )


# =====================================================
# HEAD POSE
# =====================================================

def get_head_pose(
    face_landmarks
):

    nose = (
        face_landmarks.landmark[
            NOSE_TIP
        ]
    )

    left_eye = (
        face_landmarks.landmark[
            LEFT_EYE
        ]
    )

    right_eye = (
        face_landmarks.landmark[
            RIGHT_EYE
        ]
    )

    eye_center_x = (
        left_eye.x
        + right_eye.x
    ) / 2

    eye_center_y = (
        left_eye.y
        + right_eye.y
    ) / 2

    yaw = (
        nose.x
        - eye_center_x
    )

    pitch = (
        nose.y
        - eye_center_y
    )

    if yaw > 0.015:
        return "LEFT", yaw, pitch

    if yaw < -0.015:
        return "RIGHT", yaw, pitch

    if pitch >= 0.110:
        return "DOWN", yaw, pitch

    if pitch <= 0.095:
        return "UP", yaw, pitch

    return "STRAIGHT", yaw, pitch


# =====================================================
# SMILE DETECTION
# =====================================================

def detect_expression(
    face_landmarks
):

    left_corner = (
        face_landmarks.landmark[
            LEFT_MOUTH
        ]
    )

    right_corner = (
        face_landmarks.landmark[
            RIGHT_MOUTH
        ]
    )

    upper_lip = (
        face_landmarks.landmark[
            UPPER_LIP
        ]
    )

    lower_lip = (
        face_landmarks.landmark[
            LOWER_LIP
        ]
    )

    left_cheek = (
        face_landmarks.landmark[
            LEFT_UPPER_CHEEK
        ]
    )

    right_cheek = (
        face_landmarks.landmark[
            RIGHT_UPPER_CHEEK
        ]
    )

    mouth_width = abs(
        right_corner.x
        - left_corner.x
    )

    mouth_open = abs(
        lower_lip.y
        - upper_lip.y
    )

    left_lift = (
        left_corner.y
        - left_cheek.y
    )

    right_lift = (
        right_corner.y
        - right_cheek.y
    )

    smile_lift = (
        left_lift
        + right_lift
    ) / 2

    smile_score = (
        mouth_width
        / (
            mouth_open
            + 0.001
        )
    )

    if (
        smile_score > 4.8
        and smile_lift < 0.34
    ):
        return "SMILE"

    return "NEUTRAL"


# =====================================================
# HOLD COUNTER
# =====================================================

def reset_hold_counter():

    global last_pose
    global pose_frame_count

    last_pose = "UNKNOWN"
    pose_frame_count = 0


def update_hold_counter(
    pose_name
):

    global last_pose
    global pose_frame_count

    if last_pose == pose_name:
        pose_frame_count += 1

    else:
        last_pose = pose_name
        pose_frame_count = 1

    return pose_frame_count


# =====================================================
# SAVE ALIGNED FACE
# =====================================================

def save_face_image(face):

    global count
    global last_capture

    current_time = time.time()

    if (
        current_time
        - last_capture
        < CAPTURE_DELAY
    ):
        return False

    if count >= MAX_IMAGES:
        return False

    next_count = (
        count + 1
    )

    image_path = os.path.join(
        dataset_path,
        f"{next_count}.jpg"
    )

    saved = cv2.imwrite(
        image_path,
        face,
        [
            int(
                cv2.IMWRITE_JPEG_QUALITY
            ),
            95
        ]
    )

    if not saved:
        logger.error("Could not save image: %s", image_path)

        return False

    count = next_count
    last_capture = current_time

    reset_hold_counter()

    # DEBUG: one line per captured frame, 100 per enrolment.
    logger.debug("Captured %d/%d: %s", count, MAX_IMAGES, image_path)

    return True


# =====================================================
# MAIN CAPTURE LOOP
# =====================================================

# Initialised up front rather than only inside the loop, so the exit-code
# decision below never has to ask whether the name exists yet.
cancelled = False
capture_error = False

try:

    while True:

        ret, frame = cap.read()

        if not ret:
            # Part of FS-12. This break used to fall through to a status-0
            # exit, so a camera unplugged half way through enrolment was
            # reported as a completed capture - with however many images
            # happened to be on disk at that moment.
            logger.error("Camera read error")
            capture_error = True
            break

        display = frame.copy()

        frame_height, frame_width = (
            frame.shape[:2]
        )

        rgb = cv2.cvtColor(
            frame,
            cv2.COLOR_BGR2RGB
        )

        results = face_mesh.process(
            rgb
        )

        faces = []

        status_message = (
            "Searching for face..."
        )

        current_pose = "UNKNOWN"
        yaw = 0.0
        pitch = 0.0
        face_ratio = 0.0

        aligned_face = None

        if results.multi_face_landmarks:

            for face_landmarks in (
                results.multi_face_landmarks
            ):

                face_box = get_face_box(
                    face_landmarks,
                    frame_width,
                    frame_height
                )

                (
                    x,
                    y,
                    width,
                    height
                ) = box_to_xywh(face_box)

                if face_passes_geometry_gate(
                    face_landmarks,
                    frame_width,
                    frame_height,
                    face_box
                ):

                    faces.append(
                        (
                            x,
                            y,
                            width,
                            height,
                            face_landmarks
                        )
                    )

        # =============================================
        # NO VALID FACE
        # =============================================

        if len(faces) == 0:

            reset_hold_counter()

            status_message = (
                "No valid face detected."
            )

        # =============================================
        # MORE THAN ONE FACE
        # =============================================

        elif len(faces) > 1:

            reset_hold_counter()

            status_message = (
                "Only one face is allowed."
            )

            for (
                face_x,
                face_y,
                face_width,
                face_height,
                _
            ) in faces:

                cv2.rectangle(
                    display,
                    (
                        face_x,
                        face_y
                    ),
                    (
                        face_x
                        + face_width,
                        face_y
                        + face_height
                    ),
                    (0, 0, 255),
                    2
                )

        # =============================================
        # ONE VALID FACE
        # =============================================

        else:

            (
                x,
                y,
                width,
                height,
                selected_landmarks
            ) = faces[0]

            face_ratio = (
                calculate_face_ratio(
                    frame_width,
                    frame_height,
                    width,
                    height
                )
            )

            cv2.rectangle(
                display,
                (x, y),
                (
                    x + width,
                    y + height
                ),
                (0, 255, 0),
                2
            )

            mp_draw.draw_landmarks(
                image=display,
                landmark_list=(
                    selected_landmarks
                ),
                connections=(
                    mp_face_mesh
                    .FACEMESH_CONTOURS
                ),
                landmark_drawing_spec=None,
                connection_drawing_spec=(
                    mp_draw.DrawingSpec(
                        color=(
                            255,
                            255,
                            255
                        ),
                        thickness=1,
                        circle_radius=1
                    )
                )
            )

            if not face_centered(
                frame_width,
                frame_height,
                x,
                y,
                width,
                height
            ):

                reset_hold_counter()

                status_message = (
                    "Center your face."
                )

            elif not good_distance(
                frame_width,
                frame_height,
                width,
                height
            ):

                reset_hold_counter()

                if (
                    face_ratio
                    < MIN_FACE_RATIO
                ):

                    status_message = (
                        "Move closer."
                    )

                else:

                    status_message = (
                        "Move farther."
                    )

            else:

                try:

                    aligned_face = (
                        prepare_dataset_face(
                            frame,
                            selected_landmarks
                        )
                    )

                except Exception as error:

                    reset_hold_counter()

                    aligned_face = None

                    status_message = (
                        "Face alignment failed: "
                        f"{error}"
                    )

                if aligned_face is not None:

                    quality_issue = (
                        get_image_quality_issue(
                            aligned_face
                        )
                    )

                    if quality_issue:

                        reset_hold_counter()

                        status_message = (
                            quality_issue
                        )

                    else:

                        needed_pose = (
                            required_pose(
                                count
                            )
                        )

                        if needed_pose == "SMILE":

                            current_pose = (
                                detect_expression(
                                    selected_landmarks
                                )
                            )

                        else:

                            (
                                current_pose,
                                yaw,
                                pitch
                            ) = get_head_pose(
                                selected_landmarks
                            )

                        # =================================
                        # DISTANCE CAPTURE
                        # =================================

                        if needed_pose in (
                            "CLOSE",
                            "MEDIUM",
                            "FAR"
                        ):

                            if needed_pose == "CLOSE":

                                minimum_ratio = (
                                    CLOSE_MIN_RATIO
                                )

                                maximum_ratio = (
                                    CLOSE_MAX_RATIO
                                )

                            elif (
                                needed_pose
                                == "MEDIUM"
                            ):

                                minimum_ratio = (
                                    MEDIUM_MIN_RATIO
                                )

                                maximum_ratio = (
                                    MEDIUM_MAX_RATIO
                                )

                            else:

                                minimum_ratio = (
                                    FAR_MIN_RATIO
                                )

                                maximum_ratio = (
                                    FAR_MAX_RATIO
                                )

                            if (
                                face_ratio
                                < minimum_ratio
                            ):

                                reset_hold_counter()

                                status_message = (
                                    "Move closer."
                                )

                            elif (
                                face_ratio
                                > maximum_ratio
                            ):

                                reset_hold_counter()

                                status_message = (
                                    "Move farther."
                                )

                            elif (
                                abs(yaw) > 0.045
                                or pitch > 0.25
                                or pitch < 0.055
                            ):

                                reset_hold_counter()

                                status_message = (
                                    "Keep your head "
                                    "roughly straight."
                                )

                            else:

                                held_frames = (
                                    update_hold_counter(
                                        needed_pose
                                    )
                                )

                                if (
                                    held_frames
                                    <
                                    DISTANCE_HOLD_FRAMES
                                ):

                                    status_message = (
                                        "Hold distance: "
                                        f"{held_frames}/"
                                        f"{DISTANCE_HOLD_FRAMES}"
                                    )

                                elif save_face_image(
                                    aligned_face
                                ):

                                    status_message = (
                                        f"Captured "
                                        f"{count}/"
                                        f"{MAX_IMAGES}"
                                    )

                                else:

                                    status_message = (
                                        "Hold still..."
                                    )

                        # =================================
                        # SMILE CAPTURE
                        # =================================

                        elif needed_pose == "SMILE":

                            if (
                                current_pose
                                != "SMILE"
                            ):

                                reset_hold_counter()

                                status_message = (
                                    "Please smile."
                                )

                            else:

                                held_frames = (
                                    update_hold_counter(
                                        "SMILE"
                                    )
                                )

                                if (
                                    held_frames
                                    <
                                    POSE_HOLD_FRAMES
                                ):

                                    status_message = (
                                        "Hold smile: "
                                        f"{held_frames}/"
                                        f"{POSE_HOLD_FRAMES}"
                                    )

                                elif save_face_image(
                                    aligned_face
                                ):

                                    status_message = (
                                        f"Captured "
                                        f"{count}/"
                                        f"{MAX_IMAGES}"
                                    )

                                else:

                                    status_message = (
                                        "Hold your smile..."
                                    )

                        # =================================
                        # HEAD-POSE CAPTURE
                        # =================================

                        elif (
                            current_pose
                            != needed_pose
                        ):

                            reset_hold_counter()

                            status_message = (
                                f"Required: "
                                f"{needed_pose} | "
                                f"Detected: "
                                f"{current_pose}"
                            )

                        else:

                            held_frames = (
                                update_hold_counter(
                                    current_pose
                                )
                            )

                            if (
                                held_frames
                                < POSE_HOLD_FRAMES
                            ):

                                status_message = (
                                    f"Hold "
                                    f"{current_pose}: "
                                    f"{held_frames}/"
                                    f"{POSE_HOLD_FRAMES}"
                                )

                            elif save_face_image(
                                aligned_face
                            ):

                                status_message = (
                                    f"Captured "
                                    f"{count}/"
                                    f"{MAX_IMAGES}"
                                )

                            else:

                                status_message = (
                                    "Hold still..."
                                )

        # =================================================
        # CENTER GUIDE
        # =================================================

        center_x = (
            frame_width // 2
        )

        center_y = (
            frame_height // 2
        )

        cv2.rectangle(
            display,
            (
                center_x
                - CENTER_TOLERANCE,
                center_y
                - CENTER_TOLERANCE
            ),
            (
                center_x
                + CENTER_TOLERANCE,
                center_y
                + CENTER_TOLERANCE
            ),
            (255, 255, 0),
            2
        )

        # =================================================
        # ALIGNED FACE PREVIEW
        # =================================================

        if aligned_face is not None:

            preview_size = 160

            preview = cv2.resize(
                aligned_face,
                (
                    preview_size,
                    preview_size
                )
            )

            preview = cv2.cvtColor(
                preview,
                cv2.COLOR_GRAY2BGR
            )

            preview_x1 = max(
                frame_width
                - preview_size
                - 25,
                0
            )

            preview_y1 = 25

            preview_x2 = (
                preview_x1
                + preview_size
            )

            preview_y2 = (
                preview_y1
                + preview_size
            )

            if (
                preview_x2
                <= frame_width
                and preview_y2
                <= frame_height
            ):

                display[
                    preview_y1:preview_y2,
                    preview_x1:preview_x2
                ] = preview

                cv2.rectangle(
                    display,
                    (
                        preview_x1,
                        preview_y1
                    ),
                    (
                        preview_x2,
                        preview_y2
                    ),
                    (0, 255, 255),
                    2
                )

                cv2.putText(
                    display,
                    "Aligned Face",
                    (
                        preview_x1,
                        preview_y2 + 25
                    ),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.60,
                    (0, 255, 255),
                    2
                )

        # =================================================
        # TEXT INFORMATION
        # =================================================

        instruction = get_instruction(
            count
        )

        cv2.putText(
            display,
            instruction,
            (25, 60),
            cv2.FONT_HERSHEY_DUPLEX,
            1.3,
            (0, 255, 255),
            3
        )

        cv2.putText(
            display,
            (
                f"Captured: "
                f"{count}/"
                f"{MAX_IMAGES}"
            ),
            (20, 95),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (0, 255, 0),
            2
        )

        if "Captured" in status_message:

            status_color = (
                0,
                255,
                0
            )

        elif (
            "Hold" in status_message
        ):

            status_color = (
                0,
                255,
                255
            )

        else:

            status_color = (
                0,
                0,
                255
            )

        cv2.putText(
            display,
            status_message,
            (20, 130),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.75,
            status_color,
            2
        )

        requirements = [
            "One real face only",
            "Center your face",
            "Good lighting",
            "Follow the pose",
            "Correct distance",
            "Sharp aligned image"
        ]

        cv2.putText(
            display,
            "Requirements:",
            (20, 175),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (255, 255, 255),
            2
        )

        requirements_y = 210

        for requirement in requirements:

            cv2.putText(
                display,
                "- " + requirement,
                (
                    40,
                    requirements_y
                ),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                (255, 255, 255),
                1
            )

            requirements_y += 28

        cv2.putText(
            display,
            f"Cam {current_camera_idx} | Press 'C' to switch cam",
            (20, 360),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.60,
            (255, 165, 0),
            2
        )

        cv2.putText(
            display,
            f"Yaw: {yaw:.3f}",
            (20, 390),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.70,
            (0, 255, 255),
            2
        )

        cv2.putText(
            display,
            f"Pitch: {pitch:.3f}",
            (20, 420),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.70,
            (0, 255, 255),
            2
        )

        cv2.putText(
            display,
            (
                f"Detected: "
                f"{current_pose}"
            ),
            (20, 450),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.70,
            (255, 255, 0),
            2
        )

        cv2.putText(
            display,
            (
                f"Face Ratio: "
                f"{face_ratio:.3f}"
            ),
            (20, 480),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.70,
            (255, 255, 0),
            2
        )

        # =================================================
        # PROGRESS BAR
        # =================================================

        bar_x = 20
        bar_y = max(
            frame_height - 50,
            10
        )

        bar_width = 500
        bar_height = 20

        cv2.rectangle(
            display,
            (bar_x, bar_y),
            (
                bar_x + bar_width,
                bar_y + bar_height
            ),
            (255, 255, 255),
            2
        )

        progress = int(
            (
                count
                / MAX_IMAGES
            )
            * bar_width
        )

        cv2.rectangle(
            display,
            (bar_x, bar_y),
            (
                bar_x + progress,
                bar_y + bar_height
            ),
            (0, 255, 0),
            -1
        )

        cv2.imshow(
            WINDOW_NAME,
            display
        )

        if count >= MAX_IMAGES:

            logger.info(
                "Dataset capture complete: %s, %d images, saved to %s",
                student_name,
                count,
                dataset_path
            )

            cv2.waitKey(1000)
            break

        # Check if window 'X' close button was clicked
        if cv2.getWindowProperty(WINDOW_NAME, cv2.WND_PROP_VISIBLE) < 1:
            logger.info("Capture window closed by user")
            cancelled = True
            break

        key = (
            cv2.waitKey(1)
            & 0xFF
        )

        if key == 27:

            logger.info("Capture cancelled by ESC key")
            cancelled = True

            break

        elif key in (ord('c'), ord('C')):

            logger.info(
                "Camera switch requested, current camera %s. Searching for "
                "the next one.",
                current_camera_idx
            )
            available_cams = get_available_cameras()
            if len(available_cams) > 1:
                # Get next index
                try:
                    curr_pos = available_cams.index(current_camera_idx)
                    next_idx = available_cams[(curr_pos + 1) % len(available_cams)]
                except ValueError:
                    next_idx = available_cams[0]

                logger.info("Switching to camera index %s", next_idx)
                cap.release()
                new_cap = open_camera_by_index(next_idx)
                if new_cap is not None:
                    cap = new_cap
                    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
                    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
                    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                    current_camera_idx = next_idx
                    save_camera_index(next_idx)
                    logger.info("Successfully switched to camera %s", next_idx)
                else:
                    # Reopen previous
                    cap = open_camera_by_index(current_camera_idx)
            else:
                logger.info("Only one working camera detected on this system")


# =====================================================
# CLEANUP & EXIT CODES
# =====================================================

except Exception:
    # Without this the traceback was lost: sys.exit() inside the finally
    # below replaces any in-flight exception, so an unexpected crash exited
    # silently. Log it here, while the exception is still live.
    logger.exception("Dataset capture failed with an unexpected error")
    capture_error = True

finally:

    if cap is not None:
        cap.release()

    face_mesh.close()
    cv2.destroyAllWindows()

    logger.info("Camera released")


# ---------------------------------------------------------------------------
# The exit code is the only thing app.py sees, so it is decided in one place
# rather than scattered through the loop. Deciding it here - after the finally
# rather than inside it - keeps cleanup and reporting separate, and means a
# SystemExit raised here can never pre-empt cleanup.
#
# FS-12: the rule is that only a *complete* capture reports success. Anything
# else must be non-zero, or app.py retrains on a dataset that is not there.
# ---------------------------------------------------------------------------

if cancelled:
    logger.info("Enrolment cancelled by the operator after %d image(s)", count)
    sys.exit(EXIT_CANCELLED)

if capture_error or count < MAX_IMAGES:
    logger.error(
        "Enrolment incomplete for %s: captured %d of %d images. The dataset "
        "folder is unusable and the model was not retrained.",
        student_id,
        count,
        MAX_IMAGES,
    )
    sys.exit(EXIT_FAILURE)

logger.info(
    "Enrolment complete for %s: %d images captured", student_id, count
)
sys.exit(EXIT_SUCCESS)
