"""
Where enrolment images are written, and when they become a student's dataset.

**This is FS-9, and RE-7 with it.** `/capture_face` inserted the student row
*before* the capture ran, so cancelling the OpenCV window left a student in the
database with no images - which then failed every retrain, because
`train_model()` used to refuse the whole run over one under-populated folder.
That is exactly how the Phase 0 outage was created, and `dataset/12345_test
student/` is still on disk as a specimen: an empty folder from a capture that
recorded 0 of 100 images.

The shape here makes that unrepresentable rather than unlikely:

    capture -> dataset_staging/{folder}/   (partial, gitignored, disposable)
    complete -> dataset/{folder}/          (atomic move)
    only then -> INSERT INTO students

An abandoned capture leaves a directory under `dataset_staging/` and nothing
else. Nothing reads that directory, `train_model()` never looks at it, and
`discard()` or the next attempt removes it.

⚠️ **`dataset_staging/` holds face images and is gitignored**, beside
`dataset/` and for the same reason. A partial capture is no less biometric data
than a finished one, and git history is no less permanent for it. See
`.gitignore` and CLAUDE.md.

**Recapture keeps the old dataset until the new one is complete.** FS-15: the
old route deleted the student's images and *then* captured replacements, so any
failure left them with nothing. Here the existing folder is moved aside to
`.previous` and only removed once the new one is in place - the same
tmp-write / rotate / promote shape as `write_model_atomically()` in
train_model.py, for the same reason.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
from pathlib import Path

import cv2

from config.settings import settings
from security.paths import (
    student_dataset_path,
    student_image_path,
    student_staging_path,
)

logger = logging.getLogger(__name__)

# Kept in step with what train_model.py reads and what capture_dataset.py
# wrote: JPEG at quality 95, single-channel, 200x200.
JPEG_QUALITY = 95

PREVIOUS_SUFFIX = ".previous"


class DatasetStoreError(RuntimeError):
    """A capture folder could not be written, promoted or removed."""


def _remove_readonly_and_retry(func, path, _exc_info):
    """
    shutil.rmtree onerror hook for Windows read-only files.

    A file marked read-only makes rmtree raise PermissionError rather than
    delete it.

    ⚠️ `app.py` carried a byte-identical copy of this beside the one call it
    had left, which is the duplicated-logic shape MA-4 exists to delete. Phase
    5 removed it; `remove_folder()` below is what that caller uses now.
    """
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _rmtree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, onerror=_remove_readonly_and_retry)


def remove_folder(path: Path) -> None:
    """
    Delete a student's dataset folder, read-only files included.

    Takes an already-validated path rather than a student ID: the caller has
    usually just proven containment with `security.paths.student_dataset_path`
    and re-deriving it here would be a second place for the two to disagree.

    `is_dir()` rather than `exists()` so a stray *file* at that path is left
    alone to be looked at rather than silently removed.
    """
    if path.is_dir():
        _rmtree(path)


def begin(student_id: str) -> Path:
    """
    Create an empty staging folder for a capture, and return it.

    Any previous staging folder for the same student is removed first: it is
    the residue of an abandoned attempt, and mixing its images into a new
    capture would produce a dataset that is neither.
    """
    staging = student_staging_path(student_id)

    _rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)

    logger.info("Enrolment staging opened at %s", staging)

    return staging


def save_image(staging: Path, index: int, total: int, image) -> Path:
    """
    Write one aligned crop as `{index}.jpg`.

    The index comes from the session's own counter, never from the request -
    see `security.paths.student_image_path`.

    ⚠️ The image is written exactly as `capture_dataset.py` wrote it: aligned
    grayscale 200x200 with **no CLAHE**. `preprocess_for_lbph()` applies CLAHE
    once, in training and in recognition. Applying it here too would put every
    dataset image through it twice and quietly change what the model learns.
    """
    path = student_image_path(staging, index, total)

    if not cv2.imwrite(str(path), image, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY]):
        raise DatasetStoreError(f"Could not write {path}")

    return path


def count_images(folder: Path) -> int:
    """How many images are in a folder. Zero if it does not exist."""
    if not folder.is_dir():
        return 0

    return sum(1 for entry in folder.iterdir() if entry.suffix.lower() == ".jpg")


def promote(student_id: str, expected: int) -> Path:
    """
    Move a completed staging folder into `dataset/`, and return its new path.

    `expected` is checked against what is actually on disk rather than trusted
    from the session's counter. The counter says what the protocol believes;
    this says what a retrain will find. They can differ if a write failed
    silently, and the difference is the whole reason FS-2 was possible.

    An existing dataset folder is rotated to `.previous` first and removed only
    after the promotion succeeds, so a recapture that fails part-way leaves the
    student's original images intact (FS-15).
    """
    staging = student_staging_path(student_id)
    final = student_dataset_path(student_id)

    actual = count_images(staging)

    if actual != expected:
        raise DatasetStoreError(
            f"Refusing to promote {staging}: {actual} images on disk, "
            f"{expected} expected."
        )

    previous = final.with_name(final.name + PREVIOUS_SUFFIX)
    _rmtree(previous)

    rotated = False

    if final.exists():
        os.rename(final, previous)
        rotated = True

    try:
        os.rename(staging, final)

    except OSError as error:
        if rotated:
            # Put the original back. The student keeps the dataset they had.
            os.rename(previous, final)

        raise DatasetStoreError(
            f"Could not promote {staging} to {final}"
        ) from error

    _rmtree(previous)

    logger.info(
        "Enrolment promoted: %d image(s) now at %s", actual, final
    )

    return final


def discard(student_id: str) -> None:
    """
    Throw away an in-progress capture. Safe to call when there is none.

    This is what makes cancelling free: nothing was ever written outside
    `dataset_staging/`, and no student row exists yet.
    """
    staging = student_staging_path(student_id)

    if staging.exists():
        _rmtree(staging)
        logger.info("Enrolment staging discarded: %s", staging)


def sweep(older_than_seconds: float | None = None) -> int:
    """
    Remove abandoned staging folders. Returns how many were removed.

    A closed tab leaves images on disk. They are gitignored and unreadable by
    the rest of the system, but they are still face images of a student who
    may never have completed enrolment, and `docs/data_privacy.md` is a
    document this system is supposed to be able to honour. Called on start-up.
    """
    root = settings.dataset_staging_dir

    if not root.is_dir():
        return 0

    cutoff = (
        settings.enrolment_idle_timeout_seconds
        if older_than_seconds is None
        else older_than_seconds
    )

    import time

    now = time.time()
    removed = 0

    for entry in root.iterdir():
        if not entry.is_dir():
            continue

        try:
            idle = now - entry.stat().st_mtime
        except OSError:
            continue

        if idle < cutoff:
            continue

        try:
            _rmtree(entry)
            removed += 1
        except OSError:
            logger.exception("Could not remove abandoned staging folder %s", entry)

    if removed:
        logger.info("Removed %d abandoned enrolment staging folder(s)", removed)

    return removed
