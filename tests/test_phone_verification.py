"""
services/phone_verification.py with every heavy collaborator faked.

What these pin: each phone gets its own detector and tracker; the pipeline is
only ever offered the signed-in student's labels; a recorded or refused track
ends the phone and tells the session; idle phones, phones from an earlier
session and phones past the cap are handled. The real pipeline on real faces
is tests/test_phone_verification_real.py.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from services.phone_verification import (
    LOOKING_FOR_FACE,
    PhonesBusy,
    PhoneVerifications,
)
from vision.session import SessionSnapshot

JEFF = "23-1-1-0001"
FRIEND = "23-1-1-0002"
SUBJECT = SimpleNamespace(subject_id=3, session_id=40, subject_code="CS101")


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Detector:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def track(student_id=None, saved=False, status=None, refused=None):
    return SimpleNamespace(
        student_id=student_id,
        attendance_saved=saved,
        recorded_status=status or "Present",
        attendance_refused=refused,
        confirmed=student_id is not None,
        liveness=None,
        history=[],
        config=SimpleNamespace(prediction_history_size=12),
    )


def overlay(track_id, status, width=100, height=100):
    return SimpleNamespace(
        track_id=track_id,
        status=status,
        box=SimpleNamespace(width=width, height=height),
    )


class Pipeline:
    """Stands in for recognise_frame(): records what it was given."""

    def __init__(self):
        self.contexts = []
        self.next_overlays = []
        self.next_states = {}

    def __call__(self, context, frame):
        self.contexts.append(context)
        context.tracker.states.update(self.next_states)
        return list(self.next_overlays)


def snapshot(recognized=None, subject=SUBJECT):
    return SessionSnapshot(
        running=True,
        subject=subject,
        reader=None,
        detector="the classroom's detector",
        recognizer="the model",
        label_map={
            0: {"student_id": JEFF, "name": "Jeff"},
            1: {"student_id": FRIEND, "name": "Friend"},
            2: {"student_id": JEFF, "name": "Jeff"},
        },
        tracker="the classroom's tracker",
        recognized=recognized if recognized is not None else {},
    )


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def pipeline():
    return Pipeline()


@pytest.fixture
def detectors():
    return []


@pytest.fixture
def phones(clock, pipeline, detectors):
    def make_detector():
        detector = Detector()
        detectors.append(detector)
        return detector

    return PhoneVerifications(
        make_detector=make_detector,
        make_tracker=lambda: SimpleNamespace(states={}),
        recognise=pipeline,
        clock=clock,
        idle_seconds=30.0,
        max_phones=2,
    )


def marks():
    calls = []
    return calls, lambda student_id, status: calls.append((student_id, status))


def test_the_pipeline_is_offered_only_the_signed_in_students_labels(phones, pipeline):
    """A friend's face scores nearest the friend's label, which is not here."""
    _calls, mark = marks()

    phones.verify(JEFF, snapshot(), "frame", mark)

    context = pipeline.contexts[0]
    assert sorted(context.label_map) == [0, 2]
    assert {entry["student_id"] for entry in context.label_map.values()} == {JEFF}
    # Everything else is the session's own: same model, same subject.
    assert context.recognizer == "the model"
    assert context.subject is SUBJECT


def test_a_phone_never_uses_the_classrooms_detector_or_tracker(phones, pipeline):
    _calls, mark = marks()

    phones.verify(JEFF, snapshot(), "frame", mark)

    context = pipeline.contexts[0]
    assert isinstance(context.detector, Detector)
    assert context.tracker != "the classroom's tracker"


def test_each_student_keeps_their_own_detector_and_tracker(phones, pipeline):
    _calls, mark = marks()

    phones.verify(JEFF, snapshot(), "frame", mark)
    phones.verify(FRIEND, snapshot(), "frame", mark)
    phones.verify(JEFF, snapshot(), "frame", mark)

    jeff_first, friend, jeff_again = pipeline.contexts
    assert jeff_first.detector is jeff_again.detector
    assert jeff_first.tracker is jeff_again.tracker
    assert friend.detector is not jeff_first.detector
    assert friend.tracker is not jeff_first.tracker


def test_the_sessions_recognized_mapping_is_not_written_by_the_pipeline(phones, pipeline):
    recognized = {}
    _calls, mark = marks()

    phones.verify(JEFF, snapshot(recognized), "frame", mark)
    pipeline.contexts[0].recognized[JEFF] = "Present"

    assert recognized == {}


def test_progress_reports_the_students_own_track_status(phones, pipeline):
    _calls, mark = marks()
    pipeline.next_states = {5: track(JEFF), 6: track(None)}
    pipeline.next_overlays = [
        overlay(6, "Unknown", width=400, height=400),  # bigger, but not theirs
        overlay(5, "Turn LEFT"),
    ]

    result = phones.verify(JEFF, snapshot(), "frame", mark)

    assert result.done is False
    assert result.message == "Turn LEFT"


def test_before_a_track_is_theirs_the_largest_face_speaks(phones, pipeline):
    _calls, mark = marks()
    pipeline.next_overlays = [
        overlay(1, "Move closer", width=80, height=80),
        overlay(2, "Verifying 3/20", width=300, height=300),
    ]

    result = phones.verify(JEFF, snapshot(), "frame", mark)

    assert result.message == "Verifying 3/20"


def test_no_face_yet_says_what_to_do(phones):
    _calls, mark = marks()

    result = phones.verify(JEFF, snapshot(), "frame", mark)

    assert result.message == LOOKING_FOR_FACE


def test_a_recorded_track_tells_the_session_and_ends_the_phone(
    phones, pipeline, detectors
):
    calls, mark = marks()
    pipeline.next_states = {5: track(JEFF, saved=True, status="Late")}

    result = phones.verify(JEFF, snapshot(), "frame", mark)

    assert result.done is True
    assert "Late" in result.message
    assert calls == [(JEFF, "Late")]
    assert len(phones) == 0
    assert detectors[0].closed


def test_another_students_saved_track_does_not_count(phones, pipeline):
    """Defensive: only the signed-in student's track can finish their phone."""
    calls, mark = marks()
    pipeline.next_states = {5: track(FRIEND, saved=True)}

    result = phones.verify(JEFF, snapshot(), "frame", mark)

    assert result.done is False
    assert calls == []


def test_a_permanent_refusal_ends_the_phone_without_marking(
    phones, pipeline, detectors
):
    calls, mark = marks()
    pipeline.next_states = {5: track(JEFF, refused="Not in this class")}

    result = phones.verify(JEFF, snapshot(), "frame", mark)

    assert result.refused is True
    assert result.message == "Not in this class"
    assert calls == []
    assert detectors[0].closed


def test_an_already_recorded_student_is_done_without_a_detector(phones, detectors):
    calls, mark = marks()

    result = phones.verify(JEFF, snapshot({JEFF: "Present"}), "frame", mark)

    assert result.done is True
    assert detectors == []
    assert calls == []


def test_a_phone_left_idle_is_released(phones, clock, detectors):
    _calls, mark = marks()
    phones.verify(JEFF, snapshot(), "frame", mark)

    clock.now += 31
    phones.verify(FRIEND, snapshot(), "frame", mark)

    assert detectors[0].closed
    assert len(phones) == 1


def test_a_new_session_starts_every_phone_again(phones, pipeline, detectors):
    _calls, mark = marks()
    phones.verify(JEFF, snapshot(), "frame", mark)

    next_session = SimpleNamespace(subject_id=3, session_id=41, subject_code="CS101")
    phones.verify(JEFF, snapshot(subject=next_session), "frame", mark)

    first, second = pipeline.contexts
    assert detectors[0].closed
    assert second.tracker is not first.tracker


def test_phones_past_the_cap_are_turned_away(phones):
    _calls, mark = marks()
    phones.verify(JEFF, snapshot(), "frame", mark)
    phones.verify(FRIEND, snapshot(), "frame", mark)

    with pytest.raises(PhonesBusy):
        phones.verify("23-1-1-0003", snapshot(), "frame", mark)

    # Someone already verifying is not turned away.
    phones.verify(JEFF, snapshot(), "frame", mark)


def test_discard_all_releases_every_detector(phones, detectors):
    _calls, mark = marks()
    phones.verify(JEFF, snapshot(), "frame", mark)
    phones.verify(FRIEND, snapshot(), "frame", mark)

    phones.discard_all()

    assert len(phones) == 0
    assert all(detector.closed for detector in detectors)


def test_a_detector_is_not_closed_while_its_frame_is_inside_it(clock, detectors):
    """A sweep from another request waits for the phone's frame to finish."""
    inside = threading.Event()
    release = threading.Event()
    closed_while_inside = []

    def slow_pipeline(context, frame):
        inside.set()
        release.wait(5)
        closed_while_inside.append(context.detector.closed)
        return []

    def make_detector():
        detector = Detector()
        detectors.append(detector)
        return detector

    phones = PhoneVerifications(
        make_detector=make_detector,
        make_tracker=lambda: SimpleNamespace(states={}),
        recognise=slow_pipeline,
        clock=clock,
    )
    _calls, mark = marks()

    worker = threading.Thread(
        target=phones.verify, args=(JEFF, snapshot(), "frame", mark)
    )
    worker.start()
    assert inside.wait(5)

    closer = threading.Thread(target=phones.discard_all)
    closer.start()
    closer.join(0.2)

    release.set()
    worker.join(5)
    closer.join(5)

    assert closed_while_inside == [False]
    assert detectors[0].closed


# ---------------------------------------------------------------------------
# The phone's own steadiness rules (2026-10-22: too slow, "Unknown" mid-count)
# ---------------------------------------------------------------------------


def test_the_phone_relaxes_steadiness_and_nothing_about_identity():
    """
    Every identity- and liveness-related setting is the classroom's exactly.
    Only the fields in PHONE_TRACK_OVERRIDES differ, and none of them is about
    who the face is.
    """
    from services.phone_verification import PHONE_TRACK_OVERRIDES, phone_track_config
    from vision.tracking import TrackConfig

    classroom = TrackConfig(confirmation_confidence=58.0)
    phone = phone_track_config(classroom)

    for name in (
        "confirmation_confidence",   # the 58.0 bar on the window's average
        "min_label_agreement",       # 85% of votes for one student
        "max_confirmed_mismatch_frames",
        "liveness_reset_mismatch_frames",
        "max_unreadable_frames_before_clear",
    ):
        assert getattr(phone, name) == getattr(classroom, name), name

    assert set(PHONE_TRACK_OVERRIDES) == {
        "timeout_seconds",
        "min_iou",
        "max_distance",
        "min_size_similarity",
        "max_weak_frames_before_clear",
        "real_face_confirm_frames",
        "prediction_history_size",
        "min_consecutive_identity_frames",
    }


def _counting_tracker(votes, window=12):
    state = SimpleNamespace(
        student_id=None,
        history=[object()] * votes,
        config=SimpleNamespace(prediction_history_size=window),
        attendance_saved=False,
        confirmed=False,
        liveness=None,
    )
    return SimpleNamespace(states={7: state})


def test_a_miss_mid_count_says_where_the_count_stands_not_unknown(clock, pipeline):
    phones = PhoneVerifications(
        make_detector=Detector,
        make_tracker=lambda: _counting_tracker(votes=6),
        recognise=pipeline,
        clock=clock,
    )
    pipeline.next_overlays = [overlay(7, "Unknown")]
    _calls, mark = marks()

    result = phones.verify(JEFF, snapshot(), "frame", mark)

    assert result.message == "Hold still - verifying 6/12"


def test_a_miss_before_any_vote_says_what_to_do(clock, pipeline):
    from services.phone_verification import NOT_MATCHED_YET

    phones = PhoneVerifications(
        make_detector=Detector,
        make_tracker=lambda: _counting_tracker(votes=0),
        recognise=pipeline,
        clock=clock,
    )
    pipeline.next_overlays = [overlay(7, "Unknown")]
    _calls, mark = marks()

    result = phones.verify(JEFF, snapshot(), "frame", mark)

    assert result.message == NOT_MATCHED_YET


# ---------------------------------------------------------------------------
# progress_of() - what the app draws: stage, head-turn step, one bar
# ---------------------------------------------------------------------------


def _real_state(clock):
    from services.phone_verification import phone_track_config
    from vision.tracking import TrackConfig, TrackState

    return TrackState(phone_track_config(TrackConfig()))


def _challenge(clock, sequence=("LEFT", "CENTER")):
    from vision.liveness import LivenessChallenge

    return LivenessChallenge(sequence=sequence, clock=clock)


def test_progress_follows_a_real_track_through_every_stage(clock):
    """
    The bar only ever moves forward through the four stages, and the step is
    the challenge's own current step - read from the real TrackState and
    LivenessChallenge, not a copy of their rules.
    """
    from services.phone_verification import progress_of

    state = _real_state(clock)
    seen = [progress_of(None), progress_of(state)]

    for _ in range(6):
        state.history.append({"label": 0})
    seen.append(progress_of(state))

    state.confirmed = True
    state.student_id = JEFF
    state.liveness = _challenge(clock)
    seen.append(progress_of(state))

    state.liveness.update(0.2)  # turned left
    seen.append(progress_of(state))

    state.liveness.update(0.0)  # back to centre: passed
    seen.append(progress_of(state))

    for _ in range(4):
        state.liveness.note_identity_match()
    seen.append(progress_of(state))

    state.attendance_saved = True
    seen.append(progress_of(state))

    stages = [stage for stage, _step, _value in seen]
    assert stages == [
        "finding", "matching", "matching",
        "liveness", "liveness",
        "recheck", "recheck", "done",
    ]
    assert [step for _stage, step, _value in seen][3:5] == ["LEFT", "CENTER"]

    values = [value for _stage, _step, value in seen]
    assert values == sorted(values)
    assert values[0] == 0.0 and values[-1] == 1.0
    assert values[2] == pytest.approx(0.55 * 6 / 12)


def test_a_result_carries_the_step_the_student_is_asked_for(phones, pipeline, clock):
    state = _real_state(clock)
    state.confirmed = True
    state.student_id = JEFF
    state.liveness = _challenge(clock, ("RIGHT", "LEFT"))
    pipeline.next_states = {5: state}
    pipeline.next_overlays = [overlay(5, "Liveness 1/2: Turn RIGHT")]
    _calls, mark = marks()

    result = phones.verify(JEFF, snapshot(), "frame", mark)

    assert (result.stage, result.step) == ("liveness", "RIGHT")
    assert result.message == "Liveness 1/2: Turn RIGHT"
    assert 0.5 < result.progress < 1.0


def test_no_face_is_the_start_of_the_bar(phones):
    _calls, mark = marks()

    result = phones.verify(JEFF, snapshot(), "frame", mark)

    assert (result.stage, result.step, result.progress) == ("finding", None, 0.0)


def test_a_failed_save_marks_nobody_and_keeps_the_phone(phones, pipeline, detectors):
    """
    The database was down: the pipeline left the track unsaved and unrefused
    (it retries later). Not done, not marked - the once-a-day rule must not
    see a mark that has no row behind it.
    """
    calls, mark = marks()
    pipeline.next_states = {5: track(JEFF)}
    pipeline.next_overlays = [overlay(5, "Not recorded - system error")]

    result = phones.verify(JEFF, snapshot(), "frame", mark)

    assert result.done is False and result.refused is False
    assert calls == []
    assert len(phones) == 1 and not detectors[0].closed
