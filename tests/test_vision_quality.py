"""
Tests for the shared image-quality gate (MA-4).

MA-4 names the geometry functions, but the quality check was duplicated the
same way - `recognize_face.get_face_quality_issue()` against
`capture_dataset.get_image_quality_issue()` - with four thresholds that had
drifted apart by a point or two each and four differently-worded messages.

The wording is asserted verbatim here because both strings are surfaces a
user reads: the recognition wording is drawn onto the video feed and
`recognize_face.status_color()` keys off substrings of it, and the enrolment
wording appears on the capture window. A refactor is not licensed to reword
either.
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from vision.quality import (
    ENROLMENT_QUALITY,
    RECOGNITION_QUALITY,
    ImageQualityProfile,
    quality_issue,
)

ALL_PROFILES = [RECOGNITION_QUALITY, ENROLMENT_QUALITY]


def make_face(
    *,
    brightness: float = 128.0,
    contrast: float = 40.0,
    sharp: bool = True,
    size: int = 200,
) -> np.ndarray:
    """
    A grayscale crop with controlled statistics.

    Sharpness comes from a checkerboard, which has a large Laplacian
    variance; blurring it collapses that variance without moving the mean.
    """
    rows, cols = np.indices((size, size))
    checker = ((rows // 2 + cols // 2) % 2).astype(np.float64)

    image = brightness + (checker - 0.5) * 2.0 * contrast
    image = np.clip(image, 0, 255).astype(np.uint8)

    if not sharp:
        image = cv2.GaussianBlur(image, (21, 21), 0)

    return image


@pytest.mark.parametrize("profile", ALL_PROFILES, ids=lambda p: p.name)
def test_a_good_crop_has_no_issue(profile):
    assert quality_issue(make_face(), profile) is None


@pytest.mark.parametrize("profile", ALL_PROFILES, ids=lambda p: p.name)
def test_none_is_reported_as_alignment_failure(profile):
    """
    The enrolment original had no such guard and raised inside cv2.Laplacian.

    Failing the frame rather than the run is the safer behaviour and is what
    the recognition original already did.
    """
    assert quality_issue(None, profile) == "Face alignment failed"


@pytest.mark.parametrize("profile", ALL_PROFILES, ids=lambda p: p.name)
def test_empty_array_is_reported_as_alignment_failure(profile):
    assert quality_issue(np.array([], dtype=np.uint8), profile) == (
        "Face alignment failed"
    )


@pytest.mark.parametrize("profile", ALL_PROFILES, ids=lambda p: p.name)
def test_blurred_crop_is_rejected(profile):
    issue = quality_issue(make_face(sharp=False), profile)
    assert issue == profile.blurry_message


@pytest.mark.parametrize("profile", ALL_PROFILES, ids=lambda p: p.name)
def test_dark_crop_is_rejected(profile):
    issue = quality_issue(make_face(brightness=20.0, contrast=8.0), profile)
    assert issue == profile.too_dark_message


@pytest.mark.parametrize("profile", ALL_PROFILES, ids=lambda p: p.name)
def test_bright_crop_is_rejected(profile):
    issue = quality_issue(make_face(brightness=245.0, contrast=6.0), profile)
    assert issue == profile.too_bright_message


@pytest.mark.parametrize("profile", ALL_PROFILES, ids=lambda p: p.name)
def test_flat_crop_is_rejected_for_contrast(profile):
    issue = quality_issue(make_face(brightness=128.0, contrast=3.0), profile)
    assert issue == profile.low_contrast_message


def test_the_two_profiles_keep_their_own_wording():
    """
    Both surfaces read differently, and both were preserved exactly.

    `recognize_face.status_color()` matches on the lowercase substrings
    "blurry", "dark", "bright" and "contrast"; every message in both profiles
    still contains its substring, so the colour coding survives.
    """
    assert RECOGNITION_QUALITY.blurry_message == "Face is blurry"
    assert RECOGNITION_QUALITY.too_dark_message == "Face is too dark"
    assert RECOGNITION_QUALITY.too_bright_message == "Face is too bright"
    assert RECOGNITION_QUALITY.low_contrast_message == "Low face contrast"

    assert ENROLMENT_QUALITY.blurry_message == "Image is blurry."
    assert ENROLMENT_QUALITY.too_dark_message == "The face is too dark."
    assert ENROLMENT_QUALITY.too_bright_message == "The face is too bright."
    assert ENROLMENT_QUALITY.low_contrast_message == (
        "Improve the lighting or contrast."
    )

    for profile in ALL_PROFILES:
        assert "blurry" in profile.blurry_message.lower()
        assert "dark" in profile.too_dark_message.lower()
        assert "bright" in profile.too_bright_message.lower()
        assert "contrast" in profile.low_contrast_message.lower()


def test_enrolment_is_the_stricter_of_the_two():
    """
    Documented drift, not design.

    Nobody chose to enrol at blur 55 and recognise at blur 50; the numbers
    drifted apart because they were written twice. They are preserved rather
    than reconciled because picking a winner changes which images a capture
    session saves, and that cannot be verified without a camera. Phase 6
    calibration is where these get derived. This test exists so the drift is
    visible rather than latent.
    """
    assert ENROLMENT_QUALITY.min_blur_variance > RECOGNITION_QUALITY.min_blur_variance
    assert ENROLMENT_QUALITY.min_brightness > RECOGNITION_QUALITY.min_brightness
    assert ENROLMENT_QUALITY.max_brightness < RECOGNITION_QUALITY.max_brightness
    assert ENROLMENT_QUALITY.min_contrast > RECOGNITION_QUALITY.min_contrast


def test_thresholds_are_the_pre_extraction_values():
    """Transcribed from the originals; this pins them against silent edits."""
    expected_recognition = ImageQualityProfile(
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

    assert expected_recognition == RECOGNITION_QUALITY

    assert ENROLMENT_QUALITY.min_blur_variance == 55.0
    assert ENROLMENT_QUALITY.min_brightness == 45.0
    assert ENROLMENT_QUALITY.max_brightness == 220.0
    assert ENROLMENT_QUALITY.min_contrast == 18.0
