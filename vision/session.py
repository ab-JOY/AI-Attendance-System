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

import atexit
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from vision.tracking import DEFAULT_TRACK_CONFIG, FaceTracker, TrackConfig

logger = logging.getLogger(__name__)

# How long a viewer may hold the single stream slot without producing a frame
# before another request may take it over.
#
# ⚠️ This exists because `generate_frames()` releases the slot in a `finally`,
# and a generator's `finally` runs only when the generator is closed or
# collected - which, for an MJPEG response under the development server, is not
# prompt after a browser navigates away. Until it happened, `acquire_viewer()`
# raised `SessionBusy` and the operator was told the stream was "already open
# in another window" when no such window existed. Logging out and back in was
# the reported symptom; nothing about the fresh login could clear it, because
# the slot belongs to the process, not to the login.
#
# Generous on purpose: a live stream heartbeats on every frame, and the loop
# runs at ~5 fps in the worst case measured. Anything that has produced no
# frame for ten seconds is not streaming.
VIEWER_STALE_SECONDS = 10.0


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

    # (subject) -> {student_id: recorded status} for everyone the register
    # already holds for this subject today (FS-18). Optional: without it a
    # session starts believing nobody has been recorded, which is what put a
    # student who was already Present through the whole confirmation window
    # and a fresh liveness challenge every time attendance was restarted.
    #
    # ⚠️ It reaches the database, which is why it is a hook rather than a
    # query written here - this package has no database in it, and keeping it
    # that way is what makes its tests run in milliseconds. It must not raise;
    # see `_seed_recognized`.
    already_recorded: Callable[[Any], dict[str, str]] | None = None


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

    ⚠️ **`recognized` is a mapping, not a set** (FS-18). It was a set of ids;
    it is now `{student_id: recorded status}`, because seeding it from the
    register at session start means the status is known, and a **Late** student
    who steps back into shot must not be drawn "Present". Membership tests and
    `set(recognized)` read the same either way, so the two call sites that
    changed are the write in `process_confirmed_track()` and the lookup in
    `resolve_track_identity()`.
    """

    running: bool
    subject: Any
    reader: Any
    detector: Any
    recognizer: Any
    label_map: dict[int, dict[str, str]]
    tracker: FaceTracker
    recognized: dict[str, str | None]


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
        clock: Callable[[], float] = time.monotonic,
        register_exit_hook: Callable[[Callable[[], None]], Any] | None = None,
    ) -> None:
        self._hooks = hooks
        self._lock = threading.RLock()
        self._clock = clock
        self._register_exit_hook = register_exit_hook
        self._exit_hook_registered = False

        self._tracker = FaceTracker(config=config)

        self._capture: Any = None
        self._reader: Any = None
        self._detector: Any = None

        self._recognizer: Any = None
        self._label_map: dict[int, dict[str, str]] = {}
        self._model_signature: Any = None

        self._running = False
        self._subject: Any = None
        self._recognized: dict[str, str | None] = {}

        self._viewer: int | None = None
        self._next_viewer_token = 1
        # When the current viewer last produced a frame. See
        # VIEWER_STALE_SECONDS.
        self._viewer_heartbeat_at = 0.0

    # -- introspection -----------------------------------------------------

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._running

    @property
    def subject(self) -> Any:
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

    def recorded_status_of(self, student_id: str) -> str | None:
        """What the register holds for this student, if anything (FS-18)."""
        with self._lock:
            return self._recognized.get(student_id)

    # -- the model (PE-4) --------------------------------------------------

    def _refresh_model(self) -> bool:
        """
        Load the model if the one on disk is not the one in memory.

        Returns False when there is no usable model, which is the one condition
        `start()` cannot recover from - there is nothing to recognise against.
        """
        signature = self._hooks.model_signature()

        if signature is None:
            logger.error("No trained model available, cannot start attendance")
            return False

        if signature == self._model_signature and self._recognizer is not None:
            logger.info(
                "Model unchanged since it was last loaded, reusing it (%d identities)",
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

        logger.info("Model loaded: %d student identities", len(label_map))
        return True

    # -- lifecycle ---------------------------------------------------------

    # `subject_code` is `Any` rather than `str`: since Phase 4 the caller
    # passes an ActiveSubject carrying subject_id and session_id, because a
    # subjects row is a section offering and two sections can share a code.
    # Nothing here interprets it - it is compared for equality, logged, and
    # handed back in the snapshot - so this module still knows nothing about
    # the schema.
    def start(self, subject_code: Any) -> bool:
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
                    logger.info("Attendance already running for %s", subject_code)
                    return True

                logger.error(
                    "Refusing to start %s: a session for %s is still running. End it first.",
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
                self._ensure_released_at_exit()

            if self._detector is None:
                self._detector = self._hooks.make_detector()

            self._subject = subject_code
            self._recognized = self._seed_recognized(subject_code)
            self._tracker.reset()
            self._running = True

            logger.info("Attendance started for %s", subject_code)
            return True

    def _seed_recognized(self, subject_code: Any) -> dict[str, str | None]:
        """
        Who the register already holds for this subject today (FS-18).

        This used to be `set()`, unconditionally, so the session began every
        time believing nobody had been recorded. `adopt_completed_identity()`
        reads this set and exists precisely to stop a student re-passing
        liveness for a mark they already have - and it could not fire across a
        restart, because the only thing that ever populated the set was a write
        this process had made. Stop and start attendance and a student who was
        already Present redid the 20-frame confirmation window and a fresh
        liveness challenge, for a write the UNIQUE index then discarded.

        ⚠️ **A failed read must not stop the session starting.** Losing this is
        losing an optimisation - the student passes liveness again, exactly as
        before - whereas refusing to start is losing the class. So every
        failure is logged and swallowed, and the session begins empty.
        """
        if self._hooks.already_recorded is None:
            return {}

        try:
            seeded = dict(self._hooks.already_recorded(subject_code))

        except Exception:
            logger.warning(
                "Could not read who is already recorded for %s. Starting with "
                "an empty list: anyone already marked will be asked to pass "
                "liveness again, which is a delay rather than a wrong result.",
                subject_code,
                exc_info=True,
            )
            return {}

        if seeded:
            logger.info(
                "%d student(s) are already recorded for %s today and will not "
                "be asked to verify again.",
                len(seeded),
                subject_code,
            )

        return seeded

    def _ensure_released_at_exit(self) -> None:
        """
        Release the camera when the process ends, however it ends.

        ⚠️ **This is the fix for the wedged camera.** Nothing released the
        device on the way out: there was no `atexit` hook, no signal handler
        and no shutdown path anywhere in `app.py`, and `logout()` is
        `session.clear()`. So a session that was running when the process
        stopped left `cv2.VideoCapture` open and the OS holding a claim on the
        device. The next thing to ask for the camera - our app, the Windows
        Camera app, anything - got a device that reported healthy and would not
        open. Windows renders that as
        `0xA00F429F<WindowShowFailed> (0x8007001F)`, which reads as a hardware
        fault and is not one.

        Registered on the first camera open rather than at construction, so a
        session that never touches hardware never installs a hook, and
        registered once - `atexit` keeps every registration it is given.

        **What this does not cover, stated honestly:** `atexit` runs on a
        normal exit and on Ctrl+C (which raises `KeyboardInterrupt` and unwinds
        normally). It does **not** run when the process is killed outright -
        `Stop-Process` without `-Force` already terminates rather than signals
        on Windows, and `taskkill /F` never gives the process a say. Nothing in
        userspace can cover that case; the OS is supposed to reclaim the
        device, and a camera that stays wedged after a hard kill is a driver
        problem, not this one.
        """
        if self._exit_hook_registered:
            return

        # Resolved here rather than as a parameter default, so that patching
        # `atexit.register` actually takes effect - a default is bound once, at
        # class-definition time, and would ignore the patch. The test suite
        # relies on this: a fake session left running would otherwise install a
        # real process-exit hook that logs after pytest has closed its capture
        # streams.
        register = self._register_exit_hook or atexit.register
        register(self._release_at_exit)
        self._exit_hook_registered = True

    def _release_at_exit(self) -> None:
        """
        The `atexit` callback. Never raises - the interpreter is shutting down.

        Deliberately quiet when there is nothing to release, because it runs on
        every exit of any process that ever opened a camera, including a test
        run that finished tidily.
        """
        try:
            with self._lock:
                if self._capture is None and self._reader is None:
                    return

            logger.info("Releasing the camera on process exit - a session was still running.")
            self.stop()

        except Exception:
            # Logging may already be torn down at this point, so this must not
            # be allowed to turn a clean exit into a traceback.
            pass

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
                raise SessionNotRunning("no attendance session is running")

            if self._viewer is not None:
                idle = self._clock() - self._viewer_heartbeat_at

                # A slot whose holder has produced no frame for this long is
                # an abandoned generator, not a live stream. Taking it over is
                # safe *because* it is not streaming: the single-viewer
                # invariant is about two generators advancing the tracker at
                # once, and one that has stopped iterating advances nothing.
                # `release_viewer()` already ignores a token that is no longer
                # current, so the old generator cannot evict its replacement
                # when it is finally collected.
                if idle < VIEWER_STALE_SECONDS:
                    raise SessionBusy("the camera stream is already open in another window")

                logger.warning(
                    "Taking over the stream slot from viewer %s: no frame for "
                    "%.1f s, so it is an abandoned stream rather than a live "
                    "one.",
                    self._viewer,
                    idle,
                )

            self._viewer = self._next_viewer_token
            self._next_viewer_token += 1
            self._viewer_heartbeat_at = self._clock()

            logger.info("Video stream opened (viewer %s)", self._viewer)
            return self._viewer

    def note_viewer_frame(self, token: int) -> None:
        """
        Record that this viewer is still streaming. Called once per frame.

        Cheap and lock-guarded; the alternative is a slot that can only be
        freed by garbage collection.
        """
        with self._lock:
            if self._viewer == token:
                self._viewer_heartbeat_at = self._clock()

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

    def mark_recognized(self, student_id: str, status: str | None = None) -> None:
        """Record that this student's attendance has been written."""
        with self._lock:
            self._recognized[student_id] = status
