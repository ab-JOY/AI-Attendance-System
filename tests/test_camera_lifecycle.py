"""
The camera is released on the way out, and an abandoned stream slot is
reclaimable.

Three defects, reported from UAT as "camera crashes after ~3 min" and "system
can't pick up camera again after a relogin", and one of them left the device
unusable by *any* application until a reboot:

1. **Nothing released the camera when the process ended.** No `atexit` hook, no
   signal handler, no shutdown path in `app.py`, and `logout()` is
   `session.clear()`. A session running at exit left `cv2.VideoCapture` open,
   and Windows then reported the device as healthy while refusing to open it -
   `0xA00F429F<WindowShowFailed> (0x8007001F)`, which reads like a hardware
   fault and is not one.
2. **The single stream slot leaked.** `generate_frames()` releases it in a
   `finally`, which for an MJPEG response runs only when the generator is
   closed or collected - not promptly, after a browser navigates away. Until
   then every `/video_feed` was refused with "already open in another window",
   and logging out and back in could not clear it, because the slot belongs to
   the process rather than to the login.
3. **A camera that stopped delivering frames said nothing.** `_read_failures`
   was counted from the day `CameraReader` was written and **no code ever read
   it**.

These run against fakes - no camera, no MediaPipe, no model - in milliseconds.
"""

from __future__ import annotations

import logging
import time

import pytest

from infra.camera import READ_FAILURE_WARN_AFTER, CameraReader
from vision.session import (
    VIEWER_STALE_SECONDS,
    RecognitionSession,
    SessionBusy,
    SessionHooks,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeCapture:
    def __init__(self):
        self.released = False

    def isOpened(self):  # noqa: N802 - matches the cv2 API being faked
        return not self.released

    def release(self):
        self.released = True


class FakeReader:
    def __init__(self, capture):
        self.capture = capture
        self.stopped = False

    def read(self):
        return False, None

    def stop(self):
        self.stopped = True


class FakeDetector:
    def process(self, _image):
        return None

    def close(self):
        pass


class FakeClock:
    """A clock the test moves by hand, so no test ever waits."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Hooks:
    def __init__(self):
        self.captures = []
        self.readers = []

    def open_camera(self):
        capture = FakeCapture()
        self.captures.append(capture)
        return capture

    def model_signature(self):
        return ("sig", 1)

    def load_model(self):
        return object(), {0: {"student_id": "SEC-TEST-NOBODY", "name": "Nobody"}}

    def make_detector(self):
        return FakeDetector()

    def make_reader(self, capture):
        reader = FakeReader(capture)
        self.readers.append(reader)
        return reader

    def as_hooks(self):
        return SessionHooks(
            open_camera=self.open_camera,
            load_model=self.load_model,
            model_signature=self.model_signature,
            make_detector=self.make_detector,
            make_reader=self.make_reader,
        )


class ExitHooks:
    """Stands in for `atexit.register`, so the test can fire the hooks."""

    def __init__(self):
        self.registered = []

    def register(self, function):
        self.registered.append(function)
        return function

    def run_all(self):
        for function in self.registered:
            function()


@pytest.fixture
def hooks():
    return Hooks()


@pytest.fixture
def exit_hooks():
    return ExitHooks()


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def session(hooks, exit_hooks, clock):
    return RecognitionSession(
        hooks.as_hooks(),
        clock=clock,
        register_exit_hook=exit_hooks.register,
    )


# ---------------------------------------------------------------------------
# 1. The camera is released when the process exits
# ---------------------------------------------------------------------------


def test_a_running_session_releases_the_camera_at_exit(session, hooks, exit_hooks):
    """The wedged-camera defect, directly."""
    assert session.start("SUBJ-1")
    capture = hooks.captures[0]
    assert not capture.released

    exit_hooks.run_all()

    assert capture.released, "the capture must be released on process exit"
    assert hooks.readers[0].stopped
    assert not session.is_running


def test_no_camera_no_exit_hook(hooks, exit_hooks, clock):
    """A session that never opens hardware installs nothing."""
    RecognitionSession(hooks.as_hooks(), clock=clock, register_exit_hook=exit_hooks.register)

    assert exit_hooks.registered == []


def test_the_exit_hook_is_registered_once(session, exit_hooks):
    """`atexit` keeps every registration it is handed."""
    for _ in range(3):
        session.start("SUBJ-1")
        session.stop()

    assert len(exit_hooks.registered) == 1


def test_the_exit_hook_is_quiet_when_nothing_is_open(session, hooks, exit_hooks):
    """It runs on every exit of any process that ever opened a camera."""
    assert session.start("SUBJ-1")
    session.stop()
    assert hooks.captures[0].released

    exit_hooks.run_all()  # must not raise, must not log alarm


def test_the_exit_hook_never_raises(session, hooks, exit_hooks):
    """The interpreter is shutting down; a traceback here helps nobody."""
    assert session.start("SUBJ-1")

    def explode():
        raise OSError("the device went away")

    hooks.captures[0].release = explode
    hooks.readers[0].stop = explode

    exit_hooks.run_all()  # must not propagate


# ---------------------------------------------------------------------------
# 2. The stream slot can be reclaimed
# ---------------------------------------------------------------------------


def test_a_live_stream_still_blocks_a_second_viewer(session, clock):
    """The single-viewer invariant is not weakened - only the leak is fixed."""
    session.start("SUBJ-1")
    token = session.acquire_viewer()

    clock.advance(VIEWER_STALE_SECONDS - 0.1)
    session.note_viewer_frame(token)

    with pytest.raises(SessionBusy):
        session.acquire_viewer()


def test_an_abandoned_slot_is_taken_over(session, clock):
    """The relogin lockout: nobody is streaming, but the slot is still held."""
    session.start("SUBJ-1")
    first = session.acquire_viewer()

    clock.advance(VIEWER_STALE_SECONDS + 0.1)

    second = session.acquire_viewer()
    assert second != first


def test_a_heartbeat_keeps_the_slot(session, clock):
    """A slow but live stream is never evicted."""
    session.start("SUBJ-1")
    token = session.acquire_viewer()

    for _ in range(10):
        clock.advance(VIEWER_STALE_SECONDS - 0.5)
        session.note_viewer_frame(token)

    with pytest.raises(SessionBusy):
        session.acquire_viewer()


def test_a_reclaimed_generator_cannot_evict_its_replacement(session, clock):
    """
    The old generator is collected *later*, and calls `release_viewer()` then.

    If that took the slot from whoever replaced it, this fix would trade a
    stuck stream for a stream that dies at random.
    """
    session.start("SUBJ-1")
    first = session.acquire_viewer()

    clock.advance(VIEWER_STALE_SECONDS + 0.1)
    second = session.acquire_viewer()

    session.release_viewer(first)  # the abandoned generator, finally collected

    assert session.has_viewer
    session.note_viewer_frame(second)

    with pytest.raises(SessionBusy):
        session.acquire_viewer()


def test_a_heartbeat_from_a_stale_token_is_ignored(session, clock):
    """A late frame from the evicted generator must not revive its claim."""
    session.start("SUBJ-1")
    first = session.acquire_viewer()

    clock.advance(VIEWER_STALE_SECONDS + 0.1)
    second = session.acquire_viewer()

    clock.advance(VIEWER_STALE_SECONDS + 0.1)
    session.note_viewer_frame(first)  # not the current viewer

    # `second` has gone quiet too, so the slot is reclaimable - the stale
    # heartbeat did not extend it.
    third = session.acquire_viewer()
    assert third not in (first, second)


# ---------------------------------------------------------------------------
# 3. A camera that stops delivering says so
# ---------------------------------------------------------------------------


class DeadCapture:
    """Reads fail instantly, exactly as an unplugged camera does."""

    def __init__(self, fail_forever=True):
        self.fail_forever = fail_forever
        self.reads = 0

    def read(self):
        self.reads += 1
        if self.fail_forever or self.reads <= READ_FAILURE_WARN_AFTER + 5:
            return False, None
        return True, object()


def _wait_for_reads(capture, count, timeout=10.0):
    """
    Block until the reader thread has attempted `count` reads.

    Bounded: a bug that stops the thread must fail this test rather than hang
    the suite, and there is no pytest-timeout plugin installed here.
    """
    deadline = time.monotonic() + timeout

    while capture.reads < count:
        if time.monotonic() > deadline:
            pytest.fail(
                f"the reader thread stopped after {capture.reads} reads; expected at least {count}"
            )
        time.sleep(0.005)


def test_a_dead_camera_is_reported_once(caplog):
    capture = DeadCapture()
    sleeps = []

    reader = CameraReader(
        capture,
        target_fps=0,
        clock=lambda: 0.0,
        sleep=sleeps.append,
    )

    try:
        with caplog.at_level(logging.ERROR, logger="infra.camera"):
            _wait_for_reads(capture, READ_FAILURE_WARN_AFTER * 3)

        stalls = [
            record for record in caplog.records if "stopped delivering frames" in record.message
        ]

        assert len(stalls) == 1, (
            "the stall must be reported on the transition, not once per "
            f"failed read (got {len(stalls)})"
        )
        assert not reader.is_delivering
        assert reader.consecutive_read_failures >= READ_FAILURE_WARN_AFTER

    finally:
        reader.stop()


def test_a_healthy_camera_reports_nothing(caplog):
    class LiveCapture:
        def __init__(self):
            self.reads = 0

        def read(self):
            self.reads += 1
            return True, object()

    capture = LiveCapture()
    reader = CameraReader(capture, target_fps=0, clock=lambda: 0.0, sleep=lambda _s: None)

    try:
        with caplog.at_level(logging.WARNING, logger="infra.camera"):
            _wait_for_reads(capture, READ_FAILURE_WARN_AFTER * 3)

        assert reader.is_delivering
        assert reader.consecutive_read_failures == 0
        assert not [record for record in caplog.records if "stopped delivering" in record.message]

    finally:
        reader.stop()
