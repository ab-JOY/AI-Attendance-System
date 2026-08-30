"""
The single in-progress browser capture, and everything attached to it.

A capture session is three resources that have to be created and released
together: the `EnrolmentSession` state machine from `vision/enrolment.py`, a
MediaPipe detector, and a staging folder under `dataset_staging/`. In `app.py`
they were created in one route and released by two module-level helpers that
each had to remember all three - `_abandon()` closed the detector, discarded
the folder and freed the slot; `_finish_session()` closed the detector and
freed the slot but deliberately kept the folder. The difference between them
was one line in the middle of a 2,500-line file.

`EnrolmentSlot` is those two operations named: `abandon()` throws the images
away, `release()` keeps them. Nothing else has to know that a detector exists.

⚠️ **`current()` sweeps an abandoned capture.** A closed browser tab would
otherwise hold the single slot until the server restarts, and the next operator
would be refused with no way to find out why.
"""

from __future__ import annotations

import contextlib
import logging

import cv2

from config.settings import settings
from face_preprocessing import align_face
from infra import dataset_store
from infra.facemesh import make_enrolment_detector
from vision.enrolment import (
    MAX_IMAGES,
    EnrolmentHooks,
    EnrolmentRegistry,
    EnrolmentSession,
)

logger = logging.getLogger(__name__)


def _hooks_for(staging):
    """Wire a session to MediaPipe, the aligner and the staging folder."""
    detector = make_enrolment_detector()

    def detect(frame):
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        found = detector.process(rgb)
        return list(found.multi_face_landmarks or [])

    def save(index, image):
        dataset_store.save_image(staging, index, MAX_IMAGES, image)

    return EnrolmentHooks(detect=detect, align=align_face, save=save), detector


class EnrolmentSlot:
    """One capture at a time, with its detector and staging folder."""

    def __init__(self, registry=None, idle_timeout_seconds=None):
        self._registry = registry if registry is not None else EnrolmentRegistry()
        self._idle_timeout = idle_timeout_seconds

    @property
    def idle_timeout_seconds(self):
        # Read at use rather than at construction so a test can move the
        # configured value without rebuilding the slot.
        if self._idle_timeout is not None:
            return self._idle_timeout

        return settings.enrolment_idle_timeout_seconds

    @property
    def registry(self):
        """The underlying registry, for tests that need to inspect it."""
        return self._registry

    def current(self):
        """The running session, or None. Sweeps one that has been abandoned."""
        session_in_progress = self._registry.current

        if session_in_progress is None:
            return None

        if session_in_progress.age_seconds > self.idle_timeout_seconds:
            logger.warning(
                "Discarding an abandoned enrolment for %s after %.0f s",
                session_in_progress.student_id,
                session_in_progress.age_seconds,
            )
            self.abandon()
            return None

        return session_in_progress

    def start(self, *, student_id, student_name, started_by, record):
        """
        Open a staging folder and a session. Returns it, or None if the slot
        is taken.

        Raises `UnsafeStudentPathError` or `OSError` if the staging folder
        cannot be opened - the caller turns that into a 500, because a capture
        with nowhere to write is not something the operator can act on.
        """
        staging = dataset_store.begin(student_id)

        hooks, detector = _hooks_for(staging)

        capture_session = EnrolmentSession(
            student_id=student_id,
            student_name=student_name,
            hooks=hooks,
            started_by=started_by,
            record=record,
        )
        capture_session.detector = detector

        if not self._registry.start(capture_session):
            detector.close()
            return None

        return capture_session

    def abandon(self):
        """Close the detector, discard the images, free the slot."""
        session_in_progress = self._registry.current

        if session_in_progress is None:
            return

        self._close_detector(session_in_progress)
        dataset_store.discard(session_in_progress.student_id)
        self._registry.finish()

    def release(self):
        """
        Close the detector and free the slot, **leaving the images alone**.

        Called once the staging folder has been promoted, or once it has been
        left on disk deliberately for a retry - see `enrol_finish`'s
        database-failure path, where the images are already in `dataset/` and
        throwing them away would lose a completed capture over a failed INSERT.
        """
        session_in_progress = self._registry.current

        if session_in_progress is None:
            return

        self._close_detector(session_in_progress)
        self._registry.finish()

    @staticmethod
    def _close_detector(session_in_progress):
        detector = getattr(session_in_progress, "detector", None)

        if detector is not None:
            with contextlib.suppress(Exception):
                detector.close()
