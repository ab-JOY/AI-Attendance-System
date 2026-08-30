"""
One place that constructs a MediaPipe FaceMesh.

Recognition and enrolment both need one, with different settings, and before
this each built its own - `recognize_face.make_detector()` and a module-scope
`mp_face_mesh.FaceMesh(...)` in `capture_dataset.py`. Two copies of a
construction is how MA-4 started, and the `AttributeError` fallback below is
exactly the kind of detail that gets fixed in one copy and not the other.

⚠️ **`import mediapipe` is 4.29 s** - measured, and more than reading the
55 MB model. That is why it is inside the function rather than at module
scope: importing this module must stay free, or PE-4's cold-start work is
undone the moment anything imports it. Constructing the FaceMesh itself is
0.04 s, so a session can have its own.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Recognition watches a room; enrolment watches one person and needs to notice
# a second face so it can refuse the frame. The confidences differ because the
# two were tuned separately and are preserved as they were.
RECOGNITION_MESH = {
    "static_image_mode": False,
    "max_num_faces": 10,
    "refine_landmarks": True,
    "min_detection_confidence": 0.60,
    "min_tracking_confidence": 0.55,
}

ENROLMENT_MESH = {
    "static_image_mode": False,
    "max_num_faces": 2,
    "refine_landmarks": True,
    "min_detection_confidence": 0.55,
    "min_tracking_confidence": 0.50,
}


def make_face_mesh(**settings: Any):
    """
    A MediaPipe FaceMesh built with `settings`.

    `static_image_mode=False` is deliberate for both callers, including the
    browser one where the frames arrive as separate HTTP requests: the frames
    are still a continuous view of one person, so letting the detector track
    between them is both faster and more stable than re-detecting each time.
    It does mean the detector carries state, which is why each session gets
    its own and closes it afterwards.
    """
    import mediapipe as mp

    try:
        mp_face_mesh = mp.solutions.face_mesh
    except AttributeError:
        import mediapipe.python.solutions.face_mesh as mp_face_mesh

    detector = mp_face_mesh.FaceMesh(**settings)

    logger.info("MediaPipe Face Mesh loaded (%s)", settings)

    return detector


def make_recognition_detector():
    """A detector for one attendance session."""
    return make_face_mesh(**RECOGNITION_MESH)


def make_enrolment_detector():
    """A detector for one enrolment capture."""
    return make_face_mesh(**ENROLMENT_MESH)
