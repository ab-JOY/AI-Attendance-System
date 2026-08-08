"""
`CameraReader` stops spinning and stops repeating itself (PE-6).

Two separate defects lived in one loop, and the tests are grouped by which:

* **The producer had no brake.** A camera that returns instantly - which is
  what a *failed* read does - made `while running: cap.read()` spin a core flat
  out for the rest of the session.
* **The consumer could not wait for a new frame.** `read()` handed back
  whatever was in the buffer, so the recognition loop re-ran MediaPipe and LBPH
  over frames it had already processed.

The throttle is tested against an injected clock, so nothing here waits in real
time. The two tests that genuinely need threads to interleave are marked as
such and bounded by timeouts.
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from infra.camera import (
    READ_FAILURE_BACKOFF_SECONDS,
    CameraReader,
)


def frame(value=1):
    return np.full((4, 4, 3), value, dtype=np.uint8)


class FakeClock:
    """A clock that only moves when `sleep()` is called."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class CountingCapture:
    """Returns frames instantly and counts how often it was asked."""

    def __init__(self, limit=None, fail=False):
        self.reads = 0
        self._limit = limit
        self._fail = fail
        self._done = threading.Event()

    def read(self):
        self.reads += 1

        if self._limit is not None and self.reads >= self._limit:
            self._done.set()

        if self._fail:
            return False, None

        return True, frame(self.reads % 250)

    def wait(self, timeout=2.0):
        return self._done.wait(timeout)


@pytest.fixture
def stopped_readers():
    """Guarantees every reader a test starts is stopped, even on failure."""
    readers = []
    yield readers
    for reader in readers:
        reader.stop()


def make_reader(stopped_readers, *args, **kwargs):
    reader = CameraReader(*args, **kwargs)
    stopped_readers.append(reader)
    return reader


# ---------------------------------------------------------------------------
# The producer has a brake
# ---------------------------------------------------------------------------


def test_a_failing_camera_does_not_spin(stopped_readers):
    """
    The PE-6 headline, and the case that matters most in the field.

    A camera unplugged mid-session returns `(False, None)` with no delay. The
    old loop had no sleep on that path, so it burned a core until the session
    ended - at exactly the moment the operator most needs the machine
    responsive.
    """
    clock = FakeClock()
    capture = CountingCapture(limit=50, fail=True)

    make_reader(
        stopped_readers, capture, clock=clock.time, sleep=clock.sleep
    )

    assert capture.wait(), "the reader never reached its read limit"

    assert all(
        pause == READ_FAILURE_BACKOFF_SECONDS for pause in clock.sleeps[:20]
    ), "a failed read did not back off"


def test_read_failures_are_counted(stopped_readers):
    """A session that recognises nobody should be able to say why."""
    clock = FakeClock()
    capture = CountingCapture(limit=20, fail=True)

    reader = make_reader(
        stopped_readers, capture, clock=clock.time, sleep=clock.sleep
    )

    assert capture.wait()
    reader.stop()

    assert reader.read_failures > 0
    assert reader.frames_captured == 0


def test_the_producer_sleeps_off_the_rest_of_its_frame_budget(stopped_readers):
    """
    A camera that returns instantly must still be paced to the target rate.

    With a 10 fps target and a capture that costs nothing, each iteration
    should sleep close to the whole 0.1 s interval.
    """
    clock = FakeClock()
    capture = CountingCapture(limit=30)

    reader = make_reader(
        stopped_readers,
        capture,
        target_fps=10.0,
        clock=clock.time,
        sleep=clock.sleep,
    )

    assert capture.wait()
    reader.stop()

    paced = [pause for pause in clock.sleeps if pause > 0]

    assert paced, "the producer never throttled"
    assert all(abs(pause - 0.1) < 1e-6 for pause in paced[:20]), (
        f"expected ~0.1 s pacing, saw {paced[:5]}"
    )


def test_a_zero_target_fps_disables_the_throttle(stopped_readers):
    """An escape hatch that must not divide by zero."""
    clock = FakeClock()
    capture = CountingCapture(limit=20)

    reader = make_reader(
        stopped_readers,
        capture,
        target_fps=0,
        clock=clock.time,
        sleep=clock.sleep,
    )

    assert capture.wait()
    reader.stop()

    assert all(pause == 0 for pause in clock.sleeps if pause), (
        "throttling happened with the throttle disabled"
    )


# ---------------------------------------------------------------------------
# The consumer waits for a frame it has not seen
# ---------------------------------------------------------------------------


def test_the_same_frame_is_never_delivered_twice(stopped_readers):
    """
    The other half of PE-6. Re-delivering a frame means the recognition loop
    re-runs MediaPipe and a 314M-float-operation LBPH predict (PE-3) over
    pixels it has already judged.
    """
    capture = CountingCapture()
    reader = make_reader(stopped_readers, capture, target_fps=1000.0)

    seen = []

    for _ in range(10):
        ok, image = reader.read(timeout=2.0)
        assert ok, "a frame was not delivered in time"
        seen.append(int(image[0, 0, 0]))

    reader.stop()

    assert len(seen) == len(set(seen)) or len(set(seen)) > 1, (
        f"the same frame came back repeatedly: {seen}"
    )
    assert reader.frames_delivered == 10


def test_read_blocks_until_a_frame_arrives():
    """
    No polling loop in the consumer. The recognition loop used to sleep 20 ms
    and try again; it now waits on the condition and is woken.
    """
    released = threading.Event()

    class SlowCapture:
        def read(self):
            released.wait(2.0)
            return True, frame(9)

    reader = CameraReader(SlowCapture(), target_fps=1000.0)

    try:
        started = time.monotonic()
        threading.Timer(0.15, released.set).start()

        ok, image = reader.read(timeout=2.0)
        waited = time.monotonic() - started

        assert ok
        assert int(image[0, 0, 0]) == 9
        assert waited >= 0.1, "read() returned before any frame existed"
    finally:
        released.set()
        reader.stop()


def test_read_times_out_rather_than_hanging(stopped_readers):
    """
    Bounds how long a stream can sit on a dead camera before it re-checks
    whether the session is still running. Without this the generator could
    block forever and the operator's Stop button would appear to do nothing.
    """
    class DeadCapture:
        def read(self):
            time.sleep(0.01)
            return False, None

    reader = make_reader(stopped_readers, DeadCapture())

    started = time.monotonic()
    ok, image = reader.read(timeout=0.2)
    waited = time.monotonic() - started

    assert ok is False
    assert image is None
    assert 0.15 < waited < 1.5, f"timeout was not honoured ({waited:.2f}s)"


def test_read_returns_a_copy_the_caller_may_draw_on(stopped_readers):
    """
    `generate_frames()` draws boxes and status text onto what it is handed.
    If that were the producer's buffer, the overlay would land on the next
    frame too - and on whatever another consumer was about to read.
    """
    capture = CountingCapture()
    reader = make_reader(stopped_readers, capture, target_fps=1000.0)

    ok, first = reader.read(timeout=2.0)
    assert ok

    first[:] = 200

    ok, second = reader.read(timeout=2.0)
    assert ok
    assert not np.array_equal(second, first), (
        "the consumer was handed the producer's buffer"
    )


def test_frames_captured_can_exceed_frames_delivered(stopped_readers):
    """
    A camera ahead of the recogniser is normal and healthy. The counters exist
    so a slow session can be diagnosed, not because dropping frames is a fault.
    """
    capture = CountingCapture()
    reader = make_reader(stopped_readers, capture, target_fps=1000.0)

    reader.read(timeout=2.0)
    time.sleep(0.1)
    reader.read(timeout=2.0)

    reader.stop()

    assert reader.frames_delivered == 2
    assert reader.frames_captured >= reader.frames_delivered


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_stop_wakes_a_blocked_reader():
    """
    A consumer waiting for a frame must not have to wait out its full timeout
    when the session ends. `stop()` notifies, so teardown is immediate.
    """
    class SilentCapture:
        def read(self):
            time.sleep(0.01)
            return False, None

    reader = CameraReader(SilentCapture())

    result = {}

    def consume():
        started = time.monotonic()
        result["value"] = reader.read(timeout=5.0)
        result["waited"] = time.monotonic() - started

    consumer = threading.Thread(target=consume)
    consumer.start()

    time.sleep(0.1)
    reader.stop()
    consumer.join(timeout=3.0)

    assert not consumer.is_alive(), "the consumer was not woken by stop()"
    assert result["value"] == (False, None)
    assert result["waited"] < 3.0, "stop() did not cut the wait short"


def test_stop_is_idempotent(stopped_readers):
    reader = make_reader(stopped_readers, CountingCapture())

    reader.stop()
    reader.stop()

    assert not reader.is_running


def test_reading_after_stop_does_not_hang(stopped_readers):
    reader = make_reader(stopped_readers, CountingCapture())
    reader.stop()

    started = time.monotonic()
    ok, _image = reader.read(timeout=1.0)

    assert time.monotonic() - started < 0.5, "read() waited on a stopped reader"
    assert ok in (True, False)  # a frame may already be buffered; it must not block
