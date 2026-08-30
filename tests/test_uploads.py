"""
Upload validation and frame canonicalisation.

Browser enrolment is the first request body this application accepts, so all
of this is new surface. Two things are being tested: that nothing but a small
JPEG gets through, and that whatever does get through comes out at exactly the
size every threshold in vision/pose.py was tuned at.
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from infra.uploads import (
    JPEG_MAGIC,
    MAX_DECODED_HEIGHT,
    MAX_DECODED_WIDTH,
    UploadRejected,
    canonicalise,
    decode_frame,
)
from vision.pose import (
    CANONICAL_FRAME_HEIGHT,
    CANONICAL_FRAME_WIDTH,
    MIN_NATIVE_FRAME_WIDTH,
)

CANONICAL = (CANONICAL_FRAME_HEIGHT, CANONICAL_FRAME_WIDTH)


def jpeg(width=1920, height=1080):
    """A real encoded JPEG of the given size, with content so it is not flat."""
    rng = np.random.default_rng(11)
    frame = rng.integers(0, 255, size=(height, width, 3), dtype=np.uint8)

    ok, buffer = cv2.imencode(".jpg", frame)
    assert ok

    return buffer.tobytes()


# ==============================
# What gets refused
# ==============================


@pytest.mark.parametrize(
    "content_type",
    ["text/plain", "image/png", "application/octet-stream", "", None],
)
def test_a_non_jpeg_content_type_is_refused(content_type):
    with pytest.raises(UploadRejected, match="image/jpeg"):
        decode_frame(jpeg(), content_type)


@pytest.mark.parametrize(
    "content_type",
    ["image/jpeg", "image/jpg", "IMAGE/JPEG", "image/jpeg; charset=binary"],
)
def test_accepted_content_types(content_type):
    assert decode_frame(jpeg(), content_type).shape[:2] == CANONICAL


def test_an_empty_body_is_refused():
    for body in (b"", None):
        with pytest.raises(UploadRejected, match="empty"):
            decode_frame(body, "image/jpeg")


def test_a_body_that_is_not_a_jpeg_is_refused_on_its_magic_bytes():
    """
    The declared content type is a claim by the caller. This is the file
    saying what it is, and it is checked before any decoder sees it.
    """
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64

    with pytest.raises(UploadRejected, match="not a JPEG"):
        decode_frame(png, "image/jpeg")


def test_something_with_jpeg_magic_but_no_image_is_refused():
    with pytest.raises(UploadRejected, match="could not be read"):
        decode_frame(JPEG_MAGIC + b"\x00" * 128, "image/jpeg")


def test_a_camera_below_the_minimum_width_is_refused_with_a_useful_reason():
    """
    "Image is blurry." would be a misleading thing to show someone whose only
    problem is a 640-wide webcam, so the size is refused up front and names
    the actual cause.
    """
    with pytest.raises(UploadRejected, match="at least"):
        decode_frame(jpeg(640, 480), "image/jpeg")


def test_the_minimum_width_is_itself_accepted():
    body = jpeg(MIN_NATIVE_FRAME_WIDTH, int(MIN_NATIVE_FRAME_WIDTH * 9 / 16))

    assert decode_frame(body, "image/jpeg").shape[:2] == CANONICAL


def test_an_absurdly_large_frame_is_refused():
    frame = np.zeros((64, MAX_DECODED_WIDTH + 8, 3), dtype=np.uint8)
    ok, buffer = cv2.imencode(".jpg", frame)
    assert ok

    with pytest.raises(UploadRejected, match="too large"):
        decode_frame(buffer.tobytes(), "image/jpeg")


def test_the_decoded_size_cap_is_below_anything_a_camera_produces():
    assert MAX_DECODED_WIDTH >= 7680
    assert MAX_DECODED_HEIGHT >= 4320


def test_a_refusal_never_describes_the_server():
    """
    SE-10. Every message here is about the picture, so none of them can leak
    a path, a library or a version.
    """
    bodies = [
        (b"", "image/jpeg"),
        (b"\x89PNG", "image/jpeg"),
        (jpeg(), "text/plain"),
        (jpeg(640, 480), "image/jpeg"),
    ]

    for body, content_type in bodies:
        with pytest.raises(UploadRejected) as caught:
            decode_frame(body, content_type)

        message = str(caught.value).lower()

        # Bare "/" would be a false alarm - "image/jpeg" is a media type, not
        # a path - so the patterns below are path-shaped and library-shaped
        # rather than punctuation.
        for leak in (
            "traceback",
            "opencv",
            "cv2",
            "numpy",
            "site-packages",
            ".py",
            "c:\\",
            "\\users",
            "/users/",
            "0x",
        ):
            assert leak not in message, f"{message!r} leaks {leak!r}"


# ==============================
# Canonicalisation
# ==============================


def test_a_canonical_frame_is_returned_untouched():
    frame = np.zeros(CANONICAL + (3,), dtype=np.uint8)

    assert canonicalise(frame) is frame


@pytest.mark.parametrize(
    "size",
    [(1280, 720), (1920, 1080), (2560, 1440), (3840, 2160), (1600, 900)],
)
def test_sixteen_by_nine_sources_scale_to_canonical(size):
    width, height = size
    frame = np.zeros((height, width, 3), dtype=np.uint8)

    assert canonicalise(frame).shape[:2] == CANONICAL


@pytest.mark.parametrize("size", [(1024, 768), (1280, 960), (1600, 1200)])
def test_four_by_three_sources_are_cropped_not_squashed(size):
    """
    ⚠️ The gate that would catch a squashed frame is the aspect-ratio rule
    (0.55-1.15) and the eye-separation rule, both fractions of face width.
    Stretching a 4:3 frame to 16:9 widens a face by a third and fails them for
    a reason that has nothing to do with the person in front of the camera.

    A centred circle is used because a circle stays a circle under cropping
    and becomes an ellipse under squashing, so the assertion is about shape
    rather than about the resize call.
    """
    width, height = size
    frame = np.zeros((height, width, 3), dtype=np.uint8)

    radius = height // 6
    cv2.circle(frame, (width // 2, height // 2), radius, (255, 255, 255), -1)

    out = canonicalise(frame)

    assert out.shape[:2] == CANONICAL

    mask = out[:, :, 0] > 127
    rows = np.where(mask.any(axis=1))[0]
    columns = np.where(mask.any(axis=0))[0]

    drawn_height = rows[-1] - rows[0]
    drawn_width = columns[-1] - columns[0]

    # Both axes scale by the same factor under a crop-then-resize, so the
    # circle stays round. Squashing would make this ratio about 1.33.
    assert drawn_width / drawn_height == pytest.approx(1.0, abs=0.06)


def test_a_taller_than_canonical_source_is_cropped_top_and_bottom():
    frame = np.zeros((1600, 1200, 3), dtype=np.uint8)

    # Mark the extreme top and bottom rows; both should be cropped away.
    frame[0, :] = 255
    frame[-1, :] = 255

    out = canonicalise(frame)

    assert out.shape[:2] == CANONICAL
    assert out[0, :, 0].max() == 0
    assert out[-1, :, 0].max() == 0


def test_a_wider_than_canonical_source_is_cropped_at_the_sides():
    frame = np.zeros((1080, 3000, 3), dtype=np.uint8)
    frame[:, 0] = 255
    frame[:, -1] = 255

    out = canonicalise(frame)

    assert out.shape[:2] == CANONICAL
    assert out[:, 0, 0].max() == 0
    assert out[:, -1, 0].max() == 0


def test_the_result_is_always_exactly_what_the_session_demands():
    """
    vision.enrolment.offer_frame() refuses anything else outright, so this is
    the contract between the two modules rather than a nicety.
    """
    for width, height in [(960, 540), (1024, 768), (1920, 1080), (4000, 2000)]:
        frame = np.zeros((height, width, 3), dtype=np.uint8)

        assert canonicalise(frame).shape[:2] == CANONICAL
