import cv2
import numpy as np


# =====================================================
# SHARED FACE-PREPROCESSING SETTINGS
# =====================================================

FACE_SIZE = (200, 200)

# Several landmarks are averaged to estimate each eye
# center more reliably than using one eye corner.
LEFT_EYE_INDICES = [
    33,
    133,
    159,
    145
]

RIGHT_EYE_INDICES = [
    362,
    263,
    386,
    374
]

CLAHE = cv2.createCLAHE(
    clipLimit=2.0,
    tileGridSize=(8, 8)
)


# =====================================================
# LANDMARK HELPERS
# =====================================================

def landmark_to_pixel(
    face_landmarks,
    landmark_index,
    frame_width,
    frame_height
):

    landmark = face_landmarks.landmark[
        landmark_index
    ]

    x = float(
        landmark.x * frame_width
    )

    y = float(
        landmark.y * frame_height
    )

    return np.array(
        [x, y],
        dtype=np.float32
    )


def get_average_landmark_position(
    face_landmarks,
    landmark_indices,
    frame_width,
    frame_height
):

    points = []

    for landmark_index in landmark_indices:

        point = landmark_to_pixel(
            face_landmarks,
            landmark_index,
            frame_width,
            frame_height
        )

        points.append(
            point
        )

    return np.mean(
        points,
        axis=0
    )


# =====================================================
# ALIGN FACE USING A SIMILARITY TRANSFORM
# Rotation + scaling + translation only
# No shearing or shape distortion
# =====================================================

def align_face(
    frame,
    face_landmarks
):

    if frame is None:

        raise ValueError(
            "The source frame is empty."
        )

    if face_landmarks is None:

        raise ValueError(
            "Face landmarks are missing."
        )

    frame_height, frame_width = (
        frame.shape[:2]
    )

    eye_point_1 = (
        get_average_landmark_position(
            face_landmarks,
            LEFT_EYE_INDICES,
            frame_width,
            frame_height
        )
    )

    eye_point_2 = (
        get_average_landmark_position(
            face_landmarks,
            RIGHT_EYE_INDICES,
            frame_width,
            frame_height
        )
    )

    # Sort by screen position so this also works
    # with mirrored webcam previews.
    if eye_point_1[0] <= eye_point_2[0]:

        left_eye = eye_point_1
        right_eye = eye_point_2

    else:

        left_eye = eye_point_2
        right_eye = eye_point_1

    eye_difference = (
        right_eye - left_eye
    )

    source_eye_distance = float(
        np.linalg.norm(
            eye_difference
        )
    )

    if source_eye_distance < 12:

        raise ValueError(
            "Eye distance is too small "
            "for reliable alignment."
        )

    # Reject extreme eye tilt caused by incorrect
    # landmarks or false face detection.
    vertical_eye_difference = abs(
        float(
            right_eye[1]
            - left_eye[1]
        )
    )

    if vertical_eye_difference > (
        source_eye_distance * 0.55
    ):

        raise ValueError(
            "Eye alignment is too extreme."
        )

    output_width = FACE_SIZE[0]
    output_height = FACE_SIZE[1]

    desired_left_eye = np.array(
        [
            output_width * 0.32,
            output_height * 0.38
        ],
        dtype=np.float32
    )

    desired_right_eye = np.array(
        [
            output_width * 0.68,
            output_height * 0.38
        ],
        dtype=np.float32
    )

    desired_eye_distance = float(
        desired_right_eye[0]
        - desired_left_eye[0]
    )

    scale = (
        desired_eye_distance
        / source_eye_distance
    )

    # Reject abnormal scaling rather than producing
    # a stretched or magnified image.
    if not 0.20 <= scale <= 4.00:

        raise ValueError(
            "Required alignment scale "
            "is outside the safe range."
        )

    angle_degrees = float(
        np.degrees(
            np.arctan2(
                eye_difference[1],
                eye_difference[0]
            )
        )
    )

    eye_center = (
        left_eye
        + right_eye
    ) / 2.0

    desired_eye_center = (
        desired_left_eye
        + desired_right_eye
    ) / 2.0

    transformation = (
        cv2.getRotationMatrix2D(
            (
                float(eye_center[0]),
                float(eye_center[1])
            ),
            angle_degrees,
            scale
        )
    )

    transformation[0, 2] += (
        desired_eye_center[0]
        - eye_center[0]
    )

    transformation[1, 2] += (
        desired_eye_center[1]
        - eye_center[1]
    )

    if len(frame.shape) == 3:

        gray = cv2.cvtColor(
            frame,
            cv2.COLOR_BGR2GRAY
        )

    else:

        gray = frame.copy()

    aligned_face = cv2.warpAffine(
        gray,
        transformation,
        FACE_SIZE,
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0
    )

    if aligned_face is None:

        raise ValueError(
            "Face alignment failed."
        )

    if aligned_face.size == 0:

        raise ValueError(
            "Aligned face is empty."
        )

    # Reject outputs containing too much empty
    # border instead of saving a damaged face.
    black_pixel_ratio = float(
        np.mean(
            aligned_face <= 3
        )
    )

    if black_pixel_ratio > 0.28:

        raise ValueError(
            "Face is too close to the frame edge."
        )

    return aligned_face


# =====================================================
# LBPH PREPROCESSING
# Apply exactly once during training and recognition
# =====================================================

def preprocess_for_lbph(
    face
):

    if face is None:

        raise ValueError(
            "Face image is empty."
        )

    if face.size == 0:

        raise ValueError(
            "Face image contains no pixels."
        )

    if len(face.shape) == 3:

        face = cv2.cvtColor(
            face,
            cv2.COLOR_BGR2GRAY
        )

    face = cv2.resize(
        face,
        FACE_SIZE,
        interpolation=cv2.INTER_AREA
    )

    face = CLAHE.apply(
        face
    )

    return face


# =====================================================
# ALIGN AND PREPROCESS LIVE FACE
# =====================================================

def align_and_preprocess_face(
    frame,
    face_landmarks
):

    aligned_face = align_face(
        frame,
        face_landmarks
    )

    processed_face = (
        preprocess_for_lbph(
            aligned_face
        )
    )

    return processed_face