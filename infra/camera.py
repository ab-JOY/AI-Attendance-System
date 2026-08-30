"""
The threaded camera reader (PE-6).

PE-6: "`CameraReader._reader()` is a tight unthrottled loop with no sleep and
no frame-ready signalling - it spins a core at 100% and re-copies frames the
consumer never reads."

Both halves of that were real, and they are different problems:

**The producer had no brake.** `while self._running: ret, frame = cap.read()`.
On a healthy camera `read()` blocks at the sensor's frame rate, so the loop is
paced by hardware and the waste is invisible. On a camera that returns
instantly it is not - and "returns instantly" is exactly what a *failing*
camera does. A device that has been unplugged mid-session returns
`(False, None)` with no delay, so the old loop span a core flat out for as long
as the session lasted, which is the worst possible moment to be burning CPU.

**The consumer had no way to wait for a new frame.** `read()` returned whatever
was in the buffer, so the recognition loop could pull the same frame several
times and run MediaPipe and LBPH over each copy - PE-3's 314M float operations
per face, spent on a frame that had already been processed. There was no
signal, so the loop's only alternative was to poll and sleep.

The fix is one `threading.Condition` doing both jobs: the producer publishes
and notifies, the consumer waits for a frame it has not seen, and the producer
sleeps out the remainder of its frame budget rather than immediately looping.

**Measured**, with the previous implementation lifted out of git with `ast` so
the comparison is against the code that shipped rather than a retyping of it
(lessons.md L7):

| | before | after |
|---|---:|---:|
| failing camera, CPU | **94.7% of a core** | 0.0% |
| failing camera, reads/s | 642,216 | 47 |
| instant camera, reads/s | 578,590 | 30 |

PE-6's "spins a core at 100%" was literal.

Redundant work in the consumer depends on how far ahead of the recogniser the
camera is, so it is a range rather than a number. Camera at 15 fps:

| recognition cost per frame | frames re-processed, before | after |
|---|---:|---:|
| ~45 ms (1280x720, MediaPipe + LBPH) | **30%** | 0% |
| ~10 ms (fast machine, or no face in shot) | **81%** | 0% |

`clock` and `sleep` are injected so the throttle can be tested without the test
actually waiting.
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

# The camera is configured for 1280x720 and the recognition loop cannot keep up
# with much more than this anyway - MediaPipe plus LBPH is tens of milliseconds
# per frame. Capturing faster only produces frames that are dropped.
DEFAULT_TARGET_FPS = 30.0

# How long to pause after a failed read. Long enough that an unplugged camera
# cannot spin a core, short enough that a momentary glitch is invisible.
READ_FAILURE_BACKOFF_SECONDS = 0.02

# How long `read()` waits for a new frame before giving up and letting the
# caller re-check whether the session is still running. This bounds how long
# `stop()` can appear to hang; the condition is also notified on stop, so the
# usual case is immediate.
DEFAULT_READ_TIMEOUT_SECONDS = 1.0

# How many consecutive failed reads mean the camera has stopped delivering
# rather than glitched. At READ_FAILURE_BACKOFF_SECONDS this is about two
# seconds.
#
# ⚠️ `_read_failures` was counted from the day this class was written and
# **nothing ever read it** - the property below had no caller anywhere in the
# codebase. A camera that died mid-session therefore produced a silent,
# permanent stall: `read()` returned `(False, None)`, the recognition loop
# `continue`d, and not one line was written anywhere. The operator saw a frozen
# picture and had nothing to go on. That is the same "code that looks like it
# works" shape as CAM-1 and PE-0, so the counter now has a threshold and says
# so once.
READ_FAILURE_WARN_AFTER = 100


class CameraReader:
    """
    Reads camera frames in a background thread.

    The processing loop always gets the freshest frame without blocking on
    USB or camera latency - and, since PE-6, never gets the same frame twice
    and never spins when the camera stops producing.
    """

    def __init__(
        self,
        cap,
        target_fps: float = DEFAULT_TARGET_FPS,
        clock=time.monotonic,
        sleep=time.sleep,
    ) -> None:
        self._cap = cap
        self._clock = clock
        self._sleep = sleep
        self._min_interval = (1.0 / target_fps) if target_fps and target_fps > 0 else 0.0

        self._condition = threading.Condition()
        self._frame = None
        self._sequence = 0
        self._last_delivered = 0
        self._running = True

        # Counters, for the benchmark and for diagnosing a slow session. A gap
        # between these two is frames the consumer never saw, which is normal
        # and healthy - it means the camera is ahead of the recogniser.
        self._frames_captured = 0
        self._frames_delivered = 0
        self._read_failures = 0

        # Consecutive failures, as opposed to the lifetime total above. A
        # camera that drops one frame an hour is healthy; one that has failed
        # the last 100 reads in a row is gone.
        self._consecutive_read_failures = 0
        self._stall_reported = False

        self._thread = threading.Thread(
            target=self._reader,
            name="camera-reader",
            daemon=True,
        )
        self._thread.start()

    # -- producer ----------------------------------------------------------

    def _reader(self) -> None:
        while self._running:
            started_at = self._clock()

            ret, frame = self._cap.read()

            if not ret or frame is None:
                with self._condition:
                    self._read_failures += 1
                    self._consecutive_read_failures += 1
                    # Logged outside the lock, and only on the transition -
                    # this loop runs 50 times a second while the camera is
                    # down, and a line per iteration would bury the one that
                    # matters.
                    report_stall = (
                        not self._stall_reported
                        and self._consecutive_read_failures >= READ_FAILURE_WARN_AFTER
                    )

                    if report_stall:
                        self._stall_reported = True
                        failures = self._consecutive_read_failures

                if report_stall:
                    logger.error(
                        "The camera has stopped delivering frames: %d "
                        "consecutive failed reads. It was unplugged, taken by "
                        "another application, or its driver has stopped "
                        "responding. Recognition will show a frozen picture "
                        "until it returns or the session is ended.",
                        failures,
                    )

                # The brake that PE-6 is really about. Without it an
                # unplugged camera burns a core for the rest of the session.
                self._sleep(READ_FAILURE_BACKOFF_SECONDS)
                continue

            with self._condition:
                recovered = self._stall_reported
                self._stall_reported = False
                self._consecutive_read_failures = 0
                self._frame = frame
                self._sequence += 1
                self._frames_captured += 1
                self._condition.notify_all()

            if recovered:
                logger.info("The camera is delivering frames again.")

            remaining = self._min_interval - (self._clock() - started_at)

            if remaining > 0:
                self._sleep(remaining)

    # -- consumer ----------------------------------------------------------

    def read(self, timeout: float = DEFAULT_READ_TIMEOUT_SECONDS):
        """
        Wait for a frame this caller has not seen. `(ok, frame)`.

        Returns `(False, None)` if the reader stopped or nothing new arrived
        within `timeout`, which is how the recognition loop gets a chance to
        notice the session has ended.
        """
        with self._condition:
            if self._sequence == self._last_delivered and self._running:
                self._condition.wait_for(
                    lambda: self._sequence != self._last_delivered or not self._running,
                    timeout=timeout,
                )

            if self._frame is None or self._sequence == self._last_delivered:
                return False, None

            self._last_delivered = self._sequence
            self._frames_delivered += 1

            # Copied under the lock: the producer overwrites `_frame` whole
            # rather than in place, but the consumer then goes on to draw
            # overlays onto what it is handed.
            return True, self._frame.copy()

    # -- lifecycle ---------------------------------------------------------

    def stop(self) -> None:
        """Stop the thread. Safe to call more than once."""
        with self._condition:
            self._running = False
            self._condition.notify_all()

        if self._thread.is_alive():
            self._thread.join(timeout=2.0)

            if self._thread.is_alive():
                logger.warning(
                    "Camera reader thread did not stop within 2 s. It is a "
                    "daemon, so it will not hold the process open, but the "
                    "camera may not release cleanly."
                )

        logger.debug(
            "Camera reader stopped: %d captured, %d delivered, %d read failures",
            self._frames_captured,
            self._frames_delivered,
            self._read_failures,
        )

    # -- introspection -----------------------------------------------------

    @property
    def frames_captured(self) -> int:
        with self._condition:
            return self._frames_captured

    @property
    def frames_delivered(self) -> int:
        with self._condition:
            return self._frames_delivered

    @property
    def read_failures(self) -> int:
        with self._condition:
            return self._read_failures

    @property
    def consecutive_read_failures(self) -> int:
        with self._condition:
            return self._consecutive_read_failures

    @property
    def is_delivering(self) -> bool:
        """
        False once the camera has missed `READ_FAILURE_WARN_AFTER` reads in a
        row - i.e. it has stopped producing pictures rather than glitched.

        Exposed so a caller can tell "no face in shot" from "no camera", which
        are the same thing on screen and nothing alike in cause.
        """
        with self._condition:
            return self._consecutive_read_failures < READ_FAILURE_WARN_AFTER

    @property
    def is_running(self) -> bool:
        with self._condition:
            return self._running
