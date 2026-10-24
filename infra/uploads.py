"""
Turning an uploaded JPEG into a frame the enrolment gates can judge.

Browser enrolment is the first thing in this application to accept a request
body at all, so every check here is new rather than moved. The rules Phase 2
set for the rest of the application apply: validate before you trust, never
let a request decide a path, and never return the reason for a refusal in a
form that describes the server (SE-10).

**Two jobs, and the second one is the subtle one.**

1. *Refuse anything that is not a small JPEG.* Content type, magic bytes, and
   a decoded-size cap that is checked before the pixels are trusted.
2. *Make the frame canonical.* Every threshold in `vision/pose.py` was tuned
   at 1920x1080 and two of them are absolute pixels, so a frame that arrives
   at any other size has to be converted rather than judged as it is. A
   browser has no fixed frame size - the same page delivers 640x480 on one
   laptop and 1920x1080 on another - which is exactly why this is a hazard
   here and was not one when a camera was configured once at start-up.

⚠️ **Aspect ratio is preserved by cropping, never by squashing.** Resizing a
4:3 frame straight to 16:9 stretches the face horizontally by a third, and the
geometry gate judges aspect ratio (0.55-1.15) and eye separation as fractions
of face width. A squashed frame would fail gates for a reason that has nothing
to do with the person in front of the camera.
"""

from __future__ import annotations

import logging
import math

import cv2
import numpy as np

from vision.pose import (
    CANONICAL_FRAME_HEIGHT,
    CANONICAL_FRAME_WIDTH,
    MIN_NATIVE_FRAME_WIDTH,
)

logger = logging.getLogger(__name__)

ACCEPTED_CONTENT_TYPES = frozenset({"image/jpeg", "image/jpg"})

# Start of Image, then the start of the first marker. Every JPEG begins with
# these three bytes whatever produced it.
JPEG_MAGIC = b"\xff\xd8\xff"

# A decoded frame larger than this is refused before anything else touches it.
# 8K is far past any webcam and keeps a decoded buffer under ~100 MB.
MAX_DECODED_WIDTH = 7680
MAX_DECODED_HEIGHT = 4320


class UploadRejected(ValueError):
    """
    The uploaded frame cannot be used, with a reason safe to show a caller.

    The messages are deliberately about *the picture*, not about the server:
    they tell an operator what to change, and reveal nothing about how the
    decoding works.
    """


def decode_frame(
    body: bytes | None,
    content_type: str | None,
    size: tuple[int, int] = (CANONICAL_FRAME_WIDTH, CANONICAL_FRAME_HEIGHT),
):
    """
    Validate, decode and canonicalise one uploaded frame.

    Returns a BGR array at exactly `size` (width, height) - enrolment's
    canonical frame unless the caller says otherwise. Attendance passes
    RECOGNITION_FRAME_WIDTH x RECOGNITION_FRAME_HEIGHT, the resolution its
    gates were tuned at (see vision/pose.py). Raises UploadRejected for
    anything unusable.
    """
    return canonicalise(decode_native(body, content_type), size)


def decode_native(body: bytes | None, content_type: str | None):
    """
    Validate and decode one uploaded frame, at the size it arrived.

    `decode_frame()` without the canonicalising step, for a caller that needs
    to know what the browser actually sent - enrolment records the upload
    widths of each capture, since the browser steps down on a slow link.
    """
    frame = _decode_checked(body, content_type)

    height, width = frame.shape[:2]

    if width < MIN_NATIVE_FRAME_WIDTH:
        # Upscaling further than this costs enough detail that
        # ENROLMENT_QUALITY's blur floor starts rejecting perfectly good
        # captures, and "Image is blurry." would be a misleading thing to show
        # someone whose only problem is a low-resolution webcam.
        raise UploadRejected(
            f"The camera is only {width} pixels wide. It needs to be at "
            f"least {MIN_NATIVE_FRAME_WIDTH}; choose a higher-resolution "
            "camera or raise its quality setting."
        )

    return frame


def decode_letterboxed(
    body: bytes | None,
    content_type: str | None,
    size: tuple[int, int],
):
    """
    Validate, decode and `letterbox()` one phone frame into `size`.

    ⚠️ **Not `decode_native()`'s 960-wide minimum.** That rule exists so a 16:9
    frame is not enlarged too far on its way to the canonical size. A phone
    sends a *square*, and a square is fitted by its **height**: into 1280x720 a
    768 square is shrunk to 720, losing nothing, yet 768 < 960 refused it -
    measured on the user's phone, 2026-10-24, with every verification stopped
    before the first frame. The rule that matters here is the same one, stated
    for this fit: the frame may not have to be **enlarged** to fill `size`.
    """
    frame = _decode_checked(body, content_type)

    height, width = frame.shape[:2]
    target_width, target_height = size

    if min(target_width / width, target_height / height) > 1:
        needed = min(target_height, target_width)
        raise UploadRejected(
            f"The camera picture is only {width}x{height}. It needs to be at "
            f"least {needed} pixels high; raise the camera's quality setting."
        )

    return letterbox(frame, size)


def _decode_checked(body: bytes | None, content_type: str | None):
    """Type, magic bytes, decode and decoded-size checks - no size policy."""
    _check_declared_type(content_type)
    _check_magic(body)

    frame = _decode(body)
    _check_decoded_size(frame)

    return frame


def canonicalise(
    frame,
    size: tuple[int, int] = (CANONICAL_FRAME_WIDTH, CANONICAL_FRAME_HEIGHT),
):
    """
    Centre-crop to the aspect ratio of `size`, then scale to `size`.

    Cropping rather than squashing: see the module docstring. The crop is
    centred because the framing gate asks the subject to centre themselves, so
    the middle of the frame is where the face is meant to be anyway.
    """
    target_width, target_height = size
    target_aspect = target_width / target_height

    height, width = frame.shape[:2]

    if (width, height) == size:
        return frame

    aspect = width / height

    if aspect > target_aspect:
        # Too wide: trim the sides.
        cropped_width = int(round(height * target_aspect))
        offset = (width - cropped_width) // 2
        frame = frame[:, offset : offset + cropped_width]
    elif aspect < target_aspect:
        # Too tall: trim top and bottom.
        cropped_height = int(round(width / target_aspect))
        offset = (height - cropped_height) // 2
        frame = frame[offset : offset + cropped_height, :]

    # INTER_AREA downscales without the aliasing INTER_LINEAR introduces, and
    # aliasing would show up as false detail in the Laplacian blur measure -
    # a sharpness score the picture had not earned.
    interpolation = (
        cv2.INTER_AREA
        if frame.shape[1] > target_width
        else cv2.INTER_LINEAR
    )

    return cv2.resize(frame, size, interpolation=interpolation)


# The colour letterbox() pads with. Neutral, so it adds no edge or texture the
# detector could take for anything.
LETTERBOX_GREY = 127


def letterbox(frame, size: tuple[int, int]):
    """
    Scale to fit inside `size`, then pad the rest - nothing is cropped away.

    For phone attendance (`/api/attendance/frame`). A phone is held upright,
    and `canonicalise()` keeps only a 16:9 band across the middle of an
    upright picture, so the student had to hold the phone at arm's length to
    fit a whole face into that band. The app sends a **square** instead - its
    preview is that square, so what the student frames is still what is
    judged (lessons.md L33) - and this puts the square, whole, into the
    1280x720 frame the recognition gates were tuned at. A square in a 16:9
    frame is 1.78x the band's height, so the face can be that much closer.

    Aspect ratio is preserved, as in `canonicalise()`: nothing is squashed.
    """
    target_width, target_height = size
    height, width = frame.shape[:2]

    if (width, height) == size:
        return frame

    scale = min(target_width / width, target_height / height)
    scaled_width = max(int(round(width * scale)), 1)
    scaled_height = max(int(round(height * scale)), 1)

    interpolation = cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR
    scaled = cv2.resize(frame, (scaled_width, scaled_height), interpolation=interpolation)

    canvas = np.full((target_height, target_width, 3), LETTERBOX_GREY, dtype=frame.dtype)
    x = (target_width - scaled_width) // 2
    y = (target_height - scaled_height) // 2
    canvas[y : y + scaled_height, x : x + scaled_width] = scaled

    return canvas


def _check_declared_type(content_type: str | None) -> None:
    # "image/jpeg; charset=binary" and similar are legitimate.
    declared = (content_type or "").split(";")[0].strip().lower()

    if declared not in ACCEPTED_CONTENT_TYPES:
        raise UploadRejected("Frames must be uploaded as image/jpeg.")


def _check_magic(body: bytes | None) -> None:
    if not body:
        raise UploadRejected("The uploaded frame was empty.")

    # The declared content type is a claim by the caller; this is the file
    # saying what it is. Checked before handing anything to a decoder.
    if not body.startswith(JPEG_MAGIC):
        raise UploadRejected("That upload is not a JPEG image.")


def _decode(body: bytes):
    frame = cv2.imdecode(np.frombuffer(body, dtype=np.uint8), cv2.IMREAD_COLOR)

    if frame is None or frame.size == 0:
        raise UploadRejected("That image could not be read.")

    return frame


def _check_decoded_size(frame) -> None:
    height, width = frame.shape[:2]

    if width > MAX_DECODED_WIDTH or height > MAX_DECODED_HEIGHT:
        logger.warning("Refused an oversized upload: %dx%d", width, height)
        raise UploadRejected("That image is too large.")


def canonical_region(
    region: tuple[int, int, int, int],
    native_size: tuple[int, int],
    size: tuple[int, int] = (CANONICAL_FRAME_WIDTH, CANONICAL_FRAME_HEIGHT),
) -> tuple[int, int, int, int] | None:
    """
    Map `(x, y, width, height)` in an upload's pixels onto its canonical frame.

    The same centre-crop and scale `canonicalise()` applies, rounded
    **inward**, plus one pixel: the region is a promise about which pixels are
    real, so where scaling blurs its edge the edge must not count. None if
    nothing of it survives.
    """
    native_width, native_height = native_size
    target_width, target_height = size
    target_aspect = target_width / target_height

    cropped_width = min(native_width, native_height * target_aspect)
    cropped_height = min(native_height, native_width / target_aspect)
    offset_x = (native_width - cropped_width) / 2
    offset_y = (native_height - cropped_height) / 2
    scale = target_width / cropped_width

    x, y, width, height = region

    left = math.ceil((x - offset_x) * scale) + 1
    top = math.ceil((y - offset_y) * scale) + 1
    right = math.floor((x + width - offset_x) * scale) - 1
    bottom = math.floor((y + height - offset_y) * scale) - 1

    left, top = max(0, left), max(0, top)
    right, bottom = min(target_width, right), min(target_height, bottom)

    if right <= left or bottom <= top:
        return None

    return left, top, right - left, bottom - top
