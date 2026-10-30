"""
`SessionRegistry` - several classes at once (todo.md §19 step 9).

Until this, the server was one classroom: a second instructor pressing Start
was told "An attendance session is already running". The registry holds one
`RecognitionSession` per running subject. What has to be true:

* two subjects run side by side, each with its own tracker and register;
* the server's own camera serves one session - other rooms bring their own
  browser camera;
* past the configured limit a new Start is refused, never squeezed in;
* two Starts racing cannot both slip past the limit.

Fake hooks, as in test_recognition_session.py: no camera, model or database.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from tests.test_recognition_session import FakeCapture, Recorder
from vision.session import (
    RecognitionSession,
    ServerCameraBusy,
    SessionLimitReached,
    SessionRegistry,
    subject_key,
)


class BrowserCamera(FakeCapture):
    """Stands in for infra.camera.BrowserCamera: what makes a session 'browser'."""

    def push(self, _frame):
        return True


def subject(subject_id, session_id=1):
    return SimpleNamespace(subject_id=subject_id, session_id=session_id,
                           subject_code=f"S{subject_id}")


@pytest.fixture
def recorder():
    return Recorder()


@pytest.fixture
def registry(recorder):
    return SessionRegistry(
        make_session=lambda: RecognitionSession(hooks=recorder.hooks()),
        max_sessions=3,
    )


def test_two_classes_run_side_by_side_with_their_own_state(registry):
    a, b = subject(1), subject(2)

    assert registry.start(a, camera=BrowserCamera())
    assert registry.start(b, camera=BrowserCamera())

    first, second = registry.get(1), registry.get(2)
    assert first is not second
    assert first.subject == a and second.subject == b
    assert first.tracker is not second.tracker

    first.mark_recognized("23-1-1-0918", "Present")
    assert second.recognized_ids() == set(), "one class's register leaked into another"


def test_the_same_subject_again_is_the_same_session(registry):
    a = subject(1)
    camera = BrowserCamera()

    assert registry.start(a, camera=camera)
    assert registry.start(a, camera=camera)
    assert len(registry.running()) == 1


def test_only_one_session_may_use_the_servers_camera(registry):
    assert registry.start(subject(1))  # the server's own camera

    with pytest.raises(ServerCameraBusy):
        registry.start(subject(2))

    # Another classroom with its own browser camera is fine.
    assert registry.start(subject(2), camera=BrowserCamera())
    assert registry.server_camera_in_use()


def test_past_the_limit_a_start_is_refused(registry):
    for n in (1, 2, 3):
        assert registry.start(subject(n), camera=BrowserCamera())

    with pytest.raises(SessionLimitReached):
        registry.start(subject(4), camera=BrowserCamera())

    assert len(registry.running()) == 3


def test_stopping_one_class_leaves_the_others_running(registry):
    for n in (1, 2):
        registry.start(subject(n), camera=BrowserCamera())

    assert registry.stop(1)

    assert registry.get(1) is None
    assert registry.get(2).is_running
    assert not registry.stop(1), "stopping twice is harmless and says so"


def test_a_stopped_session_frees_its_place_under_the_limit(registry):
    for n in (1, 2, 3):
        registry.start(subject(n), camera=BrowserCamera())

    registry.get(2).stop()  # e.g. its camera went away

    assert registry.start(subject(4), camera=BrowserCamera())


def test_racing_starts_cannot_pass_the_limit_together(recorder):
    registry = SessionRegistry(
        make_session=lambda: RecognitionSession(hooks=recorder.hooks()),
        max_sessions=2,
    )
    barrier = threading.Barrier(6)
    outcomes = []

    def start(n):
        barrier.wait()
        try:
            outcomes.append(registry.start(subject(n), camera=BrowserCamera()))
        except SessionLimitReached:
            outcomes.append("refused")

    threads = [threading.Thread(target=start, args=(n,)) for n in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert outcomes.count(True) == 2
    assert outcomes.count("refused") == 4
    assert len(registry.running()) == 2


def test_a_failed_start_is_not_kept(recorder):
    registry = SessionRegistry(
        make_session=lambda: RecognitionSession(
            hooks=recorder.hooks(model_signature=lambda: None)
        ),
    )

    assert registry.start(subject(1), camera=BrowserCamera()) is False
    assert registry.running() == []


def test_find_and_the_subject_key():
    assert subject_key(subject(7)) == 7
    assert subject_key("CS401") == "CS401"


def test_find_picks_a_running_session(registry):
    registry.start(subject(1), camera=BrowserCamera())
    registry.start(subject(2), camera=BrowserCamera())

    assert registry.find(lambda s: s.subject.subject_id == 2) is registry.get(2)
    assert registry.find(lambda s: False) is None
