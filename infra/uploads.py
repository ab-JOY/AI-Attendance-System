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

CANONICAL_ASPECT = CANONICAL_FRAME_WIDTH / CANONICAL_FRAME_HEIGHT


class UploadRejected(ValueError):
    """
    The uploaded frame cannot be used, with a reason safe to show a caller.

    The messages are deliberately about *the picture*, not about the server:
    they tell an operator what to change, and reveal nothing about how the
    decoding works.
    """


def decode_frame(body: bytes | None, content_type: str | None):
    """
    Validate, decode and canonicalise one uploaded frame.

    Returns a BGR array at exactly CANONICAL_FRAME_WIDTH x
    CANONICAL_FRAME_HEIGHT. Raises UploadRejected for anything unusable.
    """
    _check_declared_type(content_type)
    _check_magic(body)

    frame = _decode(body)
    _check_decoded_size(frame)

    height, width = frame.shape[:2]

    if width < MIN_NATIVE_FRAME_WIDTH:
        # Upscaling further than this costs enough detail that
        # ENROLMENT_QUALITY's blur floor starts rejecting perfectly good
        # captures, and "Image is blurry." would be a misleading thing to show
        # someone whose only problem is a low-resolution webcam.
        raise UploadRejected(
            f"The camera is only {width} pixels wide. Enrolment needs at "
            f"least {MIN_NATIVE_FRAME_WIDTH}; choose a higher-resolution "
            "camera or raise its quality setting."
        )

    return canonicalise(frame)


def canonicalise(frame):
    """
    Centre-crop to the canonical aspect ratio, then scale to the canonical size.

    Cropping rather than squashing: see the module docstring. The crop is
    centred because the framing gate asks the subject to centre themselves, so
    the middle of the frame is where the face is meant to be anyway.
    """
    height, width = frame.shape[:2]

    if (width, height) == (CANONICAL_FRAME_WIDTH, CANONICAL_FRAME_HEIGHT):
        return frame

    aspect = width / height

    if aspect > CANONICAL_ASPECT:
        # Too wide: trim the sides.
        target_width = int(round(height * CANONICAL_ASPECT))
        offset = (width - target_width) // 2
        frame = frame[:, offset : offset + target_width]
    elif aspect < CANONICAL_ASPECT:
        # Too tall: trim top and bottom.
        target_height = int(round(width / CANONICAL_ASPECT))
        offset = (height - target_height) // 2
        frame = frame[offset : offset + target_height, :]

    # INTER_AREA downscales without the aliasing INTER_LINEAR introduces, and
    # aliasing would show up as false detail in the Laplacian blur measure -
    # a sharpness score the picture had not earned.
    interpolation = (
        cv2.INTER_AREA
        if frame.shape[1] > CANONICAL_FRAME_WIDTH
        else cv2.INTER_LINEAR
    )

    return cv2.resize(
        frame,
        (CANONICAL_FRAME_WIDTH, CANONICAL_FRAME_HEIGHT),
        interpolation=interpolation,
    )


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
