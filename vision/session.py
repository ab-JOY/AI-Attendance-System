"""
The one recognition session, and the lock around it (RE-2, RE-10, PE-4).

RE-2: "Recognition state is module-level mutable globals mutated from the Flask
worker thread and the camera thread with no locking. Two `/video_feed` requests
share and corrupt one state machine; whichever generator exits first calls
`cap.release()` and kills the other."

MA-12 moved three of those globals (`tracks`, `track_verification`,
`next_track_id`) into a `FaceTracker`, but left the instance itself at module
scope and still unlocked. Seven more remained: `cap`, `camera_reader`,
`attendance_running`, `current_subject`, `recognized`, `recognizer` and
`label_map`. This module owns all of them.

**Why the heavy things are injected rather than imported.** Everything under
`vision/` is cheap to import - no camera, no MediaPipe, no database - which is
why its tests run in about two seconds. A session needs a camera, a MediaPipe
FaceMesh and a 55 MB LBPH model, so importing those here would end that
property for the whole package. Instead `SessionHooks` carries four callables
that `recognize_face.py` supplies, exactly as `FaceTracker` takes its clock.
The lifecycle and the locking - the part with the concurrency bugs in it - is
then testable with fakes and no hardware.

**What the lock does and does not protect, stated plainly.**

`start()`, `stop()`, `acquire_viewer()` and `release_viewer()` hold the lock for
their whole body, so a lifecycle transition is atomic: the frame loop can never
observe a session that is half torn down, which is the corruption RE-2
describes. `snapshot()` takes the lock to hand out a consistent set of
collaborators.

The lock is *not* held while a frame is processed. It cannot be - a `/video_feed`
response lives for as long as the operator leaves the page open, and holding a
lock across that would mean `stop()` never returns. So per-frame mutation of the
tracker is not serialised by the lock. What makes that safe is a different
guarantee:

**The single-viewer invariant.** At most one stream exists at a time. The
tracker accumulates identity votes toward an attendance decision, so two
concurrent generators walking it would double-count votes and race on
confirmation. `acquire_viewer()` therefore refuses a second stream with
`SessionBusy`, and `/video_feed` answers 409 rather than quietly serving a
second generator. One classroom camera, one session, one stream. That is what
"one session at a time, enforced" in the plan means, and it is what makes the
unlocked per-frame path correct rather than merely fast.

**RE-10** is the other half. The old generator called `cap.release()` when it
exited, so one browser tab closing ended the session for everybody. A viewer
now releases only its own slot; the camera belongs to the session and only
`stop()` releases it.

**PE-4**: the model is loaded at session start and cached against a signature
of the file on disk, so a retrain is picked up on the next start without
re-reading 55 MB of YAML on every one. It is no longer loaded at import.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from vision.tracking import DEFAULT_TRACK_CONFIG, FaceTracker, TrackConfig

logger = logging.getLogger(__name__)


class SessionBusy(RuntimeError):
    """
    A second viewer asked for a stream while one was already running.

    Raised by `acquire_viewer()`. `/video_feed` turns this into a 409 - see the
    single-viewer invariant in the module docstring.
    """


class SessionNotRunning(RuntimeError):
    """A stream was requested when no attendance session is active."""


@dataclass(frozen=True)
class SessionHooks:
    """
    The hardware and the model, supplied by the caller.

    Keeping these as callables is what lets `vision/` stay free of MediaPipe,
    the camera stack and the LBPH model while still owning the lifecycle that
    manages them.
    """

    # () -> a cv2.VideoCapture, or None if no usable camera was found.
    open_camera: Callable[[], Any]

    # () -> (recognizer, label_map). Expensive: reads the model from disk.
    load_model: Callable[[], tuple[Any, dict[int, dict[str, str]]]]

    # () -> anything comparable that changes when the model on disk changes,
    # or None when there is no model. Must be *cheap* - it is what lets
    # load_model be skipped (PE-4).
    model_signature: Callable[[], Any]

    # () -> a face detector for this session.
    make_detector: Callable[[], Any]

    # (capture) -> a threaded frame reader for this session.
    make_reader: Callable[[Any], Any]

    # (capture) -> None. Resolution and buffering, applied once on open.
    configure_camera: Callable[[Any], None] | None = None


@dataclass(frozen=True)
class SessionSnapshot:
    """
    One consistent view of the session, for one pass of the frame loop.

    This is what replaces reading seven module globals from inside the loop.
    Everything the per-frame code needs arrives as one argument, so no function
    below `generate_frames()` reaches for state at a distance any more.

    `tracker` and `recognized` are the live objects, not copies: the loop's job
    is to advance them. That is safe because of the single-viewer invariant, not
    because of the lock - see the module docstring.
    """

    running: bool
    subject: str | None
    reader: Any
    detector: Any
    recognizer: Any
    label_map: dict[int, dict[str, str]]
    tracker: FaceTracker
    recognized: set[str]


class RecognitionSession:
    """
    One attendance session: a camera, a model, a tracker and who was seen.

    Not a singleton by construction - `recognize_face` holds the one instance
    the application uses, and tests build their own with fake hooks.
    """

    def __init__(
        self,
        hooks: SessionHooks,
        config: TrackConfig = DEFAULT_TRACK_CONFIG,
    ) -> None:
        self._hooks = hooks
        self._lock = threading.RLock()

        self._tracker = FaceTracker(config=config)

        self._capture: Any = None
        self._reader: Any = None
        self._detector: Any = None

        self._recognizer: Any = None
        self._label_map: dict[int, dict[str, str]] = {}
        self._model_signature: Any = None

        self._running = False
        self._subject: str | None = None
        self._recognized: set[str] = set()

        self._viewer: int | None = None
        self._next_viewer_token = 1

    # -- introspection -----------------------------------------------------

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._running

    @property
    def subject(self) -> str | None:
        with self._lock:
            return self._subject

    @property
    def has_viewer(self) -> bool:
        with self._lock:
            return self._viewer is not None

    @property
    def tracker(self) -> FaceTracker:
        """The live tracker. Exposed for assertions, not for mutation."""
        return self._tracker

    def recognized_ids(self) -> set[str]:
        """A copy, so a caller on another thread cannot see a partial set."""
        with self._lock:
            return set(self._recognized)

    # -- the model (PE-4) --------------------------------------------------

    def _refresh_model(self) -> bool:
        """
        Load the model if the one on disk is not the one in memory.

        Returns False when there is no usable model, which is the one condition
        `start()` cannot recover from - there is nothing to recognise against.
        """
        signature = self._hooks.model_signature()

        if signature is None:
            logger.error(
                "No trained model available, cannot start attendance"
            )
            return False

        if signature == self._model_signature and self._recognizer is not None:
            logger.info(
                "Model unchanged since it was last loaded, reusing it "
                "(%d identities)",
                len(self._label_map),
            )
            return True

        try:
            recognizer, label_map = self._hooks.load_model()
        except Exception:
            logger.exception("Model load failed, cannot start attendance")
            return False

        if recognizer is None or not label_map:
            logger.error("Model loaded but contains no identities")
            return False

        self._recognizer = recognizer
        self._label_map = label_map
        self._model_signature = signature

        logger.info(
            "Model loaded: %d student identities", len(label_map)
        )
        return True

    # -- lifecycle ---------------------------------------------------------

    def start(self, subject_code: str) -> bool:
        """
        Begin a session for one subject. True if recognition can now run.

        Refuses to switch subject while a session is running. The old code
        simply overwrote `current_subject`, which silently attributed frames
        already being counted toward one subject to another one; the operator
        has to end the session first.
        """
        with self._lock:
            if self._running:
                if self._subject == subject_code:
                    logger.info(
                        "Attendance already running for %s", subject_code
                    )
                    return True

                logger.error(
                    "Refusing to start %s: a session for %s is still "
                    "running. End it first.",
                    subject_code,
                    self._subject,
                )
                return False

            if not self._refresh_model():
                return False

            if self._capture is None:
                capture = self._hooks.open_camera()

                if capture is None or not capture.isOpened():
                    logger.error("Cannot open camera")
                    return False

                if self._hooks.configure_camera is not None:
                    self._hooks.configure_camera(capture)

                self._capture = capture
                self._reader = self._hooks.make_reader(capture)

            if self._detector is None:
                self._detector = self._hooks.make_detector()

            self._subject = subject_code
            self._recognized = set()
            self._tracker.reset()
            self._running = True

            logger.info("Attendance started for %s", subject_code)
            return True

    def stop(self) -> None:
        """
        End the session and release the camera. Safe to call when not running.

        This is the *only* thing that releases the camera. A viewer going away
        does not (RE-10).
        """
        with self._lock:
            was_running = self._running

            self._running = False
            self._subject = None
            self._viewer = None
            self._tracker.reset()

            if self._reader is not None:
                try:
                    self._reader.stop()
                except Exception:
                    logger.warning("Camera reader did not stop cleanly", exc_info=True)
                self._reader = None

            if self._capture is not None:
                try:
                    self._capture.release()
                except Exception:
                    logger.warning("Camera did not release cleanly", exc_info=True)
                self._capture = None

            if self._detector is not None:
                close = getattr(self._detector, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        logger.warning("Detector did not close cleanly", exc_info=True)
                self._detector = None

            if was_running:
                logger.info("Attendance ended")

    # -- the single viewer (RE-2, RE-10) -----------------------------------

    def acquire_viewer(self) -> int:
        """
        Claim the one stream slot. Returns a token for `release_viewer()`.

        Raises `SessionNotRunning` if there is nothing to stream, and
        `SessionBusy` if another stream already holds the slot.
        """
        with self._lock:
            if not self._running:
                raise SessionNotRunning(
                    "no attendance session is running"
                )

            if self._viewer is not None:
                raise SessionBusy(
                    "the camera stream is already open in another window"
                )

            self._viewer = self._next_viewer_token
            self._next_viewer_token += 1

            logger.info("Video stream opened (viewer %s)", self._viewer)
            return self._viewer

    def release_viewer(self, token: int) -> None:
        """
        Give up a stream slot. Deliberately does not touch the camera (RE-10).

        Ignores a token that is not the current one, so a generator finishing
        late cannot evict the stream that replaced it.
        """
        with self._lock:
            if self._viewer != token:
                return

            self._viewer = None
            logger.info("Video stream closed (viewer %s)", token)

    # -- per-frame ---------------------------------------------------------

    def snapshot(self) -> SessionSnapshot:
        """One consistent view of the session for one frame."""
        with self._lock:
            return SessionSnapshot(
                running=self._running,
                subject=self._subject,
                reader=self._reader,
                detector=self._detector,
                recognizer=self._recognizer,
                label_map=self._label_map,
                tracker=self._tracker,
                recognized=self._recognized,
            )

    def mark_recognized(self, student_id: str) -> None:
        """Record that this student's attendance has been written."""
        with self._lock:
            self._recognized.add(student_id)
