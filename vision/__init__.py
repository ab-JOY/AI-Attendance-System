"""
Face-geometry, quality and tracking logic shared by enrolment and recognition.

This package exists because MA-4 found `get_face_box()` and
`is_valid_face_candidate()` implemented twice - once in `recognize_face.py`
and once in `capture_dataset.py` - with different thresholds, so the two
stages of the pipeline accepted different populations of faces with nothing
in the codebase saying so.

Nothing here imports OpenCV's camera stack, MediaPipe, or the database, so it
is cheap to import and testable without hardware. `vision.quality` needs
`cv2` only for `Laplacian`.
"""
