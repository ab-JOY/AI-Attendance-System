"""
The image-quality gate: is this crop sharp, well-lit and contrasty enough?

MA-4 names `get_face_box()` and `is_valid_face_candidate()`, but the quality
check was duplicated the same way and had drifted the same way -
`recognize_face.get_face_quality_issue()` against
`capture_dataset.get_image_quality_issue()`, with four thresholds that
disagreed by a point or two each. Fixing one pair of duplicates and leaving
the other in place would have left the finding half-closed.

The threshold differences here are small and, unlike the geometry profiles,
have no design intent behind them - nobody chose to enrol at blur 55 and
recognise at blur 50. They are preserved rather than reconciled because this
sprint is a refactor: picking a winner would change which images get saved
during capture, and that cannot be verified without a camera. Phase 6
calibration is where these get a derivation instead of a value.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass(frozen=True)
class ImageQualityProfile:
    """Sharpness, exposure and contrast tolerances for one stage."""

    name: str

    # Variance of the Laplacian. Low means out of focus or motion-blurred.
    min_blur_variance: float

    # Mean and standard deviation of the grayscale crop.
    min_brightness: float
    max_brightness: float
    min_contrast: float

    # The two stages word these differently on screen, and the wording is
    # carried here rather than normalised because the capture overlay and the
    # video-feed overlay are different surfaces that a user reads in different
    # contexts. `recognize_face.status_color()` keys off the substrings
    # "blurry" / "dark" / "bright" / "contrast", which both wordings contain.
    blurry_message: str
    too_dark_message: str
    too_bright_message: str
    low_contrast_message: str


RECOGNITION_QUALITY = ImageQualityProfile(
    name="recognition",
    min_blur_variance=50.0,
    min_brightness=40.0,
    max_brightness=222.0,
    min_contrast=17.0,
    blurry_message="Face is blurry",
    too_dark_message="Face is too dark",
    too_bright_message="Face is too bright",
    low_contrast_message="Low face contrast",
)


ENROLMENT_QUALITY = ImageQualityProfile(
    name="enrolment",
    min_blur_variance=55.0,
    min_brightness=45.0,
    max_brightness=220.0,
    min_contrast=18.0,
    blurry_message="Image is blurry.",
    too_dark_message="The face is too dark.",
    too_bright_message="The face is too bright.",
    low_contrast_message="Improve the lighting or contrast.",
)


def quality_issue(
    face_image: np.ndarray | None,
    profile: ImageQualityProfile,
) -> str | None:
    """
    A human-readable reason this crop is unusable, or `None` if it is fine.

    The strings are shown to the operator over the video feed, so they are
    phrased as something a person can act on.
    """
    if face_image is None or face_image.size == 0:
        # The enrolment original omitted this guard and would raise inside
        # cv2.Laplacian on a failed alignment. Adding it makes the enrolment
        # path fail the frame instead of the run, which is strictly safer and
        # matches what the recognition original already did.
        return "Face alignment failed"

    blur_variance = cv2.Laplacian(face_image, cv2.CV_64F).var()

    if blur_variance < profile.min_blur_variance:
        return profile.blurry_message

    brightness = float(face_image.mean())

    if brightness < profile.min_brightness:
        return profile.too_dark_message

    if brightness > profile.max_brightness:
        return profile.too_bright_message

    contrast = float(face_image.std())

    if contrast < profile.min_contrast:
        return profile.low_contrast_message

    return None
