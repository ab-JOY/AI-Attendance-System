"""
MediaPipe FaceMesh landmark indices, named once.

Both `recognize_face.py` and `capture_dataset.py` carried their own copy of
these numbers. They agreed - but they agreed by coincidence, and a mesh index
typed twice is a mesh index that can drift once.

The two modules did not name the same *set*: recognition averaged four
landmarks per eye, enrolment used the outer corner alone. That difference is
real and is expressed by the profiles in `vision.validation`, not hidden here.
"""

from __future__ import annotations

NOSE_TIP = 1

LEFT_EYE_OUTER = 33
LEFT_EYE_INNER = 133
LEFT_EYE_UPPER = 159
LEFT_EYE_LOWER = 145

RIGHT_EYE_INNER = 362
RIGHT_EYE_OUTER = 263
RIGHT_EYE_UPPER = 386
RIGHT_EYE_LOWER = 374

LEFT_MOUTH = 61
RIGHT_MOUTH = 291
UPPER_LIP = 13
LOWER_LIP = 14

# Used only by the enrolment smile detector, but kept here so this module is
# the one place a MediaPipe mesh index appears.
LEFT_UPPER_CHEEK = 205
RIGHT_UPPER_CHEEK = 425

# The four-point eye centres used by the recognition gate. Averaging the
# corners with the lid midpoints puts the point near the pupil and makes the
# eye-distance measurement much steadier under blinking than a single corner.
LEFT_EYE_QUAD = (
    LEFT_EYE_OUTER,
    LEFT_EYE_INNER,
    LEFT_EYE_UPPER,
    LEFT_EYE_LOWER,
)

RIGHT_EYE_QUAD = (
    RIGHT_EYE_INNER,
    RIGHT_EYE_OUTER,
    RIGHT_EYE_UPPER,
    RIGHT_EYE_LOWER,
)

# The single-point eye definition used by the enrolment gate: outer corners
# only. Outer-corner separation is systematically *wider* than the four-point
# centre separation, which is why the enrolment eye-distance band sits a
# little higher than the recognition one. The bands are not directly
# comparable; see vision/validation.py.
LEFT_EYE_SINGLE = (LEFT_EYE_OUTER,)
RIGHT_EYE_SINGLE = (RIGHT_EYE_OUTER,)
