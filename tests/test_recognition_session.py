"""
`RecognitionSession` - the lifecycle, the locking and the single-viewer rule.

RE-2 was "recognition state is module-level mutable globals mutated from the
Flask worker thread and the camera thread with no locking". The fix is only
worth anything if the lifecycle it introduces is actually correct, and that is
what this file checks: what happens on a second start, on a stop that was never
started, on a second viewer, and on two threads doing all of it at once.

Every test here runs against fake hooks - no camera, no MediaPipe, no model, no
database - so the whole file runs in milliseconds and needs neither hardware nor
the gitignored `dataset/`. That is the point of `SessionHooks`: the concurrency
logic is the part with the bugs in it, and it is now the part that is cheap to
test.

The end-to-end counterpart, with a real model and real MediaPipe over real face
pixels, is `tests/test_recognition_loop_smoke.py`.
"""

from __future__ import annotations

import threading

import pytest

from vision.session import (
    RecognitionSession,
    SessionBusy,
    SessionHooks,
    SessionNotRunning,
)
from vision.tracking import TrackConfig

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeCapture:
    def __init__(self, opened=True):
        self._opened = opened
        self.released = False

    def isOpened(self):  # noqa: N802 - matches the cv2 API being faked
        return self._opened and not self.released

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
    def __init__(self):
        self.closed = False

    def process(self, _image):
        return None

    def close(self):
        self.closed = True


class Recorder:
    """Counts how often each hook was called, which is what PE-4 is about."""

    def __init__(self, signature=("sig", 1)):
        self.signature = signature
        self.captures = []
        self.readers = []
        self.detectors = []
        self.load_calls = 0
        self.signature_calls = 0
        self.configure_calls = 0
        self.open_camera_calls = 0

    # -- hooks ---------------------------------------------------------

    def open_camera(self):
        self.open_camera_calls += 1
        capture = FakeCapture()
        self.captures.append(capture)
        return capture

    def model_signature(self):
        self.signature_calls += 1
        return self.signature

    def load_model(self):
        self.load_calls += 1
        return object(), {0: {"student_id": "SEC-TEST-NOBODY", "name": "Nobody"}}

    def make_detector(self):
        detector = FakeDetector()
        self.detectors.append(detector)
        return detector

    def make_reader(self, capture):
        reader = FakeReader(capture)
        self.readers.append(reader)
        return reader

    def configure_camera(self, _capture):
        self.configure_calls += 1

    def hooks(self, **overrides):
        fields = {
            "open_camera": self.open_camera,
            "load_model": self.load_model,
            "model_signature": self.model_signature,
            "make_detector": self.make_detector,
            "make_reader": self.make_reader,
            "configure_camera": self.configure_camera,
        }
        fields.update(overrides)
        return SessionHooks(**fields)


@pytest.fixture
def recorder():
    return Recorder()


@pytest.fixture
def session(recorder):
    return RecognitionSession(hooks=recorder.hooks(), config=TrackConfig())


# ---------------------------------------------------------------------------
# Starting
# ---------------------------------------------------------------------------


def test_a_fresh_session_is_not_running(session):
    assert not session.is_running
    assert session.subject is None
    assert not session.has_viewer
    assert session.recognized_ids() == set()


def test_start_opens_the_camera_and_loads_the_model(session, recorder):
    assert session.start("CS401") is True

    assert session.is_running
    assert session.subject == "CS401"
    assert recorder.open_camera_calls == 1
    assert recorder.load_calls == 1
    assert recorder.configure_calls == 1
    assert len(recorder.readers) == 1
    assert len(recorder.detectors) == 1


def test_start_refuses_when_there_is_no_model(recorder):
    """
    A missing model is the one thing `start()` cannot work around, and Phase 0
    is the story of what it looks like when this fails quietly instead: the
    system reported a working session and recognised nobody.
    """
    session = RecognitionSession(
        hooks=recorder.hooks(model_signature=lambda: None)
    )

    assert session.start("CS401") is False
    assert not session.is_running
    assert recorder.open_camera_calls == 0, "opened a camera with no model to use"


def test_start_refuses_when_the_model_will_not_load(recorder):
    def explode():
        raise RuntimeError("trainer.yml is truncated")

    session = RecognitionSession(hooks=recorder.hooks(load_model=explode))

    assert session.start("CS401") is False
    assert not session.is_running


def test_start_refuses_when_the_model_has_no_identities(recorder):
    session = RecognitionSession(
        hooks=recorder.hooks(load_model=lambda: (object(), {}))
    )

    assert session.start("CS401") is False
    assert not session.is_running


def test_start_refuses_when_the_camera_will_not_open(recorder):
    session = RecognitionSession(hooks=recorder.hooks(open_camera=lambda: None))

    assert session.start("CS401") is False
    assert not session.is_running


def test_start_refuses_a_camera_that_reports_itself_closed(recorder):
    session = RecognitionSession(
        hooks=recorder.hooks(open_camera=lambda: FakeCapture(opened=False))
    )

    assert session.start("CS401") is False
    assert not session.is_running


def test_starting_the_same_subject_twice_is_idempotent(session, recorder):
    assert session.start("CS401") is True
    assert session.start("CS401") is True

    assert recorder.open_camera_calls == 1, "reopened the camera mid-session"
    assert session.subject == "CS401"


def test_switching_subject_mid_session_is_refused(session):
    """
    The old code just overwrote `current_subject`.

    Frames already accumulating votes toward an attendance mark for CS401 would
    then be written against CS402, which is a wrong attendance record rather
    than a crash - so it would have been found by a student disputing a report,
    not by a log. The operator has to end the session first.
    """
    assert session.start("CS401") is True
    assert session.start("CS402") is False

    assert session.subject == "CS401", "the running subject was overwritten"
    assert session.is_running


def test_start_resets_what_the_previous_session_saw(session):
    assert session.start("CS401") is True
    session.mark_recognized("SEC-TEST-NOBODY")
    assert session.recognized_ids() == {"SEC-TEST-NOBODY"}

    session.stop()

    assert session.start("CS402") is True
    assert session.recognized_ids() == set(), (
        "a new session inherited the previous session's attendance list"
    )


# ---------------------------------------------------------------------------
# The model cache (PE-4)
# ---------------------------------------------------------------------------


def test_an_unchanged_model_is_not_re_read(session, recorder):
    """
    PE-4: the model was read at import *and* on every start_camera(). An LBPH
    read is 4.8 s of the old 9.2 s startup, so a second session used to pay it
    again for a file that had not changed.
    """
    session.start("CS401")
    session.stop()
    session.start("CS402")

    assert recorder.load_calls == 1, (
        f"re-read an unchanged model {recorder.load_calls} times"
    )
    assert recorder.signature_calls == 2, "the cheap check was skipped"


def test_a_retrained_model_is_picked_up(session, recorder):
    """The other half - the cache must not outlive the file it describes."""
    session.start("CS401")
    session.stop()

    recorder.signature = ("sig", 2)  # a retrain
    session.start("CS402")

    assert recorder.load_calls == 2, "a retrained model was not reloaded"


# ---------------------------------------------------------------------------
# Stopping
# ---------------------------------------------------------------------------


def test_stop_releases_everything(session, recorder):
    session.start("CS401")

    session.stop()

    assert not session.is_running
    assert session.subject is None
    assert recorder.captures[0].released
    assert recorder.readers[0].stopped
    assert recorder.detectors[0].closed


def test_stop_is_safe_when_nothing_was_started(session):
    session.stop()
    session.stop()

    assert not session.is_running


def test_stop_survives_hardware_that_fails_on_teardown(recorder):
    """
    A camera that raises on release must not leave the session stuck running -
    otherwise a failed teardown makes the app unusable until it is restarted,
    and the operator's only recourse is the one thing they cannot do mid-class.
    """

    class AngryCapture(FakeCapture):
        def release(self):
            raise OSError("device disappeared")

    class AngryReader(FakeReader):
        def stop(self):
            raise OSError("thread wedged")

    session = RecognitionSession(
        hooks=recorder.hooks(
            open_camera=AngryCapture,
            make_reader=AngryReader,
        )
    )

    assert session.start("CS401") is True

    session.stop()

    assert not session.is_running
    assert session.subject is None

    # And the session is reusable afterwards.
    assert session.start("CS402") is True


def test_a_session_can_be_restarted_after_stopping(session, recorder):
    session.start("CS401")
    session.stop()

    assert session.start("CS402") is True
    assert recorder.open_camera_calls == 2
    assert session.subject == "CS402"


# ---------------------------------------------------------------------------
# The single viewer (RE-2, RE-10)
# ---------------------------------------------------------------------------


def test_no_viewer_without_a_session(session):
    with pytest.raises(SessionNotRunning):
        session.acquire_viewer()


def test_one_viewer_is_allowed(session):
    session.start("CS401")

    token = session.acquire_viewer()

    assert session.has_viewer
    assert token is not None


def test_a_second_viewer_is_refused(session):
    """
    The invariant the unlocked per-frame path depends on. Two generators
    walking one tracker would both add votes to the same prediction history and
    both race to confirm an identity from it.
    """
    session.start("CS401")
    session.acquire_viewer()

    with pytest.raises(SessionBusy):
        session.acquire_viewer()


def test_releasing_a_viewer_frees_the_slot(session):
    session.start("CS401")
    first = session.acquire_viewer()

    session.release_viewer(first)

    assert not session.has_viewer

    second = session.acquire_viewer()
    assert second != first, "viewer tokens were reused"


def test_releasing_a_stale_token_does_not_evict_the_current_viewer(session):
    """
    A generator can be garbage-collected long after its slot was reassigned.
    If a late `finally` could clear the slot, the operator's working stream
    would be silently displaced by the teardown of one they already closed.
    """
    session.start("CS401")
    first = session.acquire_viewer()
    session.release_viewer(first)

    second = session.acquire_viewer()
    session.release_viewer(first)  # stale, arrives late

    assert session.has_viewer, "a stale token evicted the live viewer"

    session.release_viewer(second)
    assert not session.has_viewer


def test_releasing_a_viewer_does_not_release_the_camera(session, recorder):
    """RE-10, stated at the level of the session rather than the generator."""
    session.start("CS401")
    token = session.acquire_viewer()

    session.release_viewer(token)

    assert not recorder.captures[0].released
    assert session.is_running


def test_stopping_the_session_clears_the_viewer(session):
    session.start("CS401")
    session.acquire_viewer()

    session.stop()

    assert not session.has_viewer


# ---------------------------------------------------------------------------
# The snapshot
# ---------------------------------------------------------------------------


def test_the_snapshot_carries_what_the_frame_loop_needs(session, recorder):
    session.start("CS401")

    snapshot = session.snapshot()

    assert snapshot.running is True
    assert snapshot.subject == "CS401"
    assert snapshot.reader is recorder.readers[0]
    assert snapshot.detector is recorder.detectors[0]
    assert snapshot.recognizer is not None
    assert snapshot.label_map
    assert snapshot.tracker is session.tracker


def test_a_snapshot_taken_before_stop_reports_the_stop(session):
    """
    How the frame loop learns to exit. It re-snapshots every frame rather than
    caching one, so `stop()` on another thread ends the stream within a frame.
    """
    session.start("CS401")
    before = session.snapshot()

    session.stop()
    after = session.snapshot()

    assert before.running is True
    assert after.running is False


def test_recognized_ids_returns_a_copy(session):
    session.start("CS401")
    session.mark_recognized("SEC-TEST-NOBODY")

    ids = session.recognized_ids()
    ids.add("SEC-TEST-NOONE")

    assert session.recognized_ids() == {"SEC-TEST-NOBODY"}


# ---------------------------------------------------------------------------
# Under threads, which is the whole reason RE-2 was filed
# ---------------------------------------------------------------------------


def test_only_one_of_many_racing_viewers_wins(session):
    """
    RE-2 named two `/video_feed` requests sharing one state machine. Flask
    serves them on different threads, so "one at a time" has to hold against a
    race and not merely against a sequence.
    """
    session.start("CS401")

    acquired = []
    refused = []
    ready = threading.Barrier(8)

    def contend():
        ready.wait()
        try:
            acquired.append(session.acquire_viewer())
        except SessionBusy:
            refused.append(True)

    threads = [threading.Thread(target=contend) for _ in range(8)]

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join()

    assert len(acquired) == 1, f"{len(acquired)} viewers got in at once"
    assert len(refused) == 7


def test_racing_starts_open_exactly_one_camera(recorder):
    """
    Without the lock, two threads both see `cap is None` and both open a
    camera; one of them is then leaked, holding the device until the process
    exits - and on Windows the second open of a DirectShow device usually just
    fails, so the visible symptom is an unexplained failure to start.
    """
    session = RecognitionSession(hooks=recorder.hooks())

    results = []
    ready = threading.Barrier(8)

    def contend():
        ready.wait()
        results.append(session.start("CS401"))

    threads = [threading.Thread(target=contend) for _ in range(8)]

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join()

    assert all(results), "a concurrent start was refused for the same subject"
    assert recorder.open_camera_calls == 1, (
        f"opened the camera {recorder.open_camera_calls} times"
    )
    assert recorder.load_calls == 1


def test_stop_during_contention_leaves_a_consistent_session(recorder):
    """
    Starts and stops interleaved on many threads must not leave the session
    half torn down - running with no reader, or stopped with the camera still
    held. Every camera handed out is either the live one or released.
    """
    session = RecognitionSession(hooks=recorder.hooks())

    ready = threading.Barrier(10)

    def churn(index):
        ready.wait()
        for _ in range(20):
            if index % 2:
                session.start("CS401")
            else:
                session.stop()

    threads = [threading.Thread(target=churn, args=(i,)) for i in range(10)]

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join()

    snapshot = session.snapshot()

    if snapshot.running:
        assert snapshot.reader is not None
        assert snapshot.detector is not None
        assert snapshot.recognizer is not None
    else:
        assert snapshot.reader is None
        assert snapshot.detector is None

    session.stop()

    assert all(capture.released for capture in recorder.captures), (
        "a camera was opened and never released"
    )
