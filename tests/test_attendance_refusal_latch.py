"""
A rejected attendance write is not retried on every frame forever (FS-17).

**Reported as "the verification repeats even after a person has already been
verified", and this is the half of it that shows up in the log.**
`logs/app.log` for 2026-08-21 16:01 carries 27 consecutive
`Rejected attendance: ... is not enrolled in subject 1` lines, one per frame,
210 ms apart, for one student standing still.

`process_confirmed_track()` set `attendance_saved` only when
`save_attendance()` returned truthily. `NotRecorded` is falsy by design (US-10),
and two of its three members are **permanent** - the student is not in this
class, or the model holds a face the `students` table does not. Nothing latched
either, `identity_reconfirmed` stayed True, and the next frame asked the
database the same question and got the same answer. Measured before the fix:
**193 writes attempted in 200 frames.**

The cost is not only the log. Each attempt is two round trips, and
`predict_identity()` skipped LBPH only on `attendance_saved` - so a refused
track also paid a full predict every frame, 98 ms at today's roster and
280-400 ms at 30 students (`docs/benchmarks.md`). One student missing from
`enrolments` therefore slowed the stream for everyone else in shot. That is PE-3
reopened at precisely the face it was written to exempt.

⚠️ **`ERROR` is deliberately not latched.** An unreachable database has not
refused anything; it has failed to answer. That is retried, on a backoff.

No database and no camera: `save_attendance` is stubbed in every test here, and
every identifier is fake (lessons.md L6).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import recognize_face
from recognize_face import NotRecorded
from vision.tracking import TrackState

ALICE = "SEC-TEST-NOBODY"
ALICE_NAME = "Nobody"
CONFIG = recognize_face.TRACK_CONFIG

# Straight ahead: no step is satisfied, so nothing here advances a challenge
# underneath a test about what happens after one.
NO_MOVEMENT = 0.0


class Register:
    """A stand-in for `save_attendance()` that counts and can change its mind."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = 0

    def __call__(self, _student_id, _subject):
        self.calls += 1

        if len(self.answers) > 1:
            return self.answers.pop(0)

        return self.answers[0]


@pytest.fixture
def context():
    return SimpleNamespace(
        subject=recognize_face.ActiveSubject(
            subject_id=1, session_id=1, subject_code="TEST-101"
        ),
        recognized={},
        label_map={},
    )


@pytest.fixture
def passed_track():
    """
    A confirmed track that has passed liveness *and* its identity re-check.

    `post_match_count` is pre-filled deliberately: every test in this file is
    about what happens at and after the attendance write, and leaving the
    re-check to accumulate would put eight frames of warm-up in front of each
    one - which is how the two retry timings below would silently measure the
    wrong thing.
    """
    state = TrackState(config=CONFIG)
    state.valid_face_frames = CONFIG.real_face_confirm_frames
    state.confirm(ALICE, ALICE_NAME, recognize_face.create_liveness_state())
    state.liveness.passed = True
    state.liveness.index = len(state.liveness.sequence)
    state.liveness.post_match_count = state.liveness.config.post_match_frames
    return state


def drive(context, state, frames):
    """Run `frames` readable frames that all agree with the locked identity."""
    result = None

    for _ in range(frames):
        result = recognize_face.process_confirmed_track(
            context, state, ALICE, NO_MOVEMENT
        )
        state = result[3]

    return result


# ---------------------------------------------------------------------------
# Permanent refusals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "refusal", [NotRecorded.NOT_IN_CLASS, NotRecorded.UNKNOWN_STUDENT]
)
def test_a_permanent_refusal_is_asked_exactly_once(
    context, passed_track, monkeypatch, refusal
):
    """⚠️ **The regression: 193 attempts in 200 frames.**"""
    register = Register(refusal)
    monkeypatch.setattr(recognize_face, "save_attendance", register)

    drive(context, passed_track, frames=200)

    assert register.calls == 1, (
        f"{refusal.value} is a fact about the database, not about the "
        f"picture. Asking again cannot change it, and asking it "
        f"{register.calls} times cost two round trips and an LBPH predict each."
    )


def test_the_refusal_stays_on_the_overlay(context, passed_track, monkeypatch):
    """The operator needs the reason to persist, not to flicker once and go."""
    monkeypatch.setattr(
        recognize_face, "save_attendance", Register(NotRecorded.NOT_IN_CLASS)
    )

    _id, _name, status, _state = drive(context, passed_track, frames=50)

    assert status == NotRecorded.NOT_IN_CLASS.value


def test_a_refused_track_never_claims_to_have_been_recorded(
    context, passed_track, monkeypatch
):
    """
    ⚠️ **Latching must not look like success.** `attendance_saved` means a row
    exists; a refusal means one deliberately does not. Conflating them would
    mark an unenrolled student present on the overlay and in `recognized`.
    """
    monkeypatch.setattr(
        recognize_face, "save_attendance", Register(NotRecorded.NOT_IN_CLASS)
    )

    _id, _name, _status, state = drive(context, passed_track, frames=20)

    assert not state.attendance_saved
    assert state.attendance_refused == NotRecorded.NOT_IN_CLASS.value
    assert state.attendance_settled
    assert context.recognized == {}, (
        "A refused student must never be seeded into the recognised set - the "
        "next session would then skip their verification entirely"
    )


def test_a_settled_track_stops_paying_for_recognition(passed_track, monkeypatch):
    """
    PE-3 at the refused face. `predict_identity()` keyed its skip on
    `attendance_saved`, so the one track that could never succeed was the one
    that kept running LBPH on every frame.
    """
    predictions = []

    class CountingRecognizer:
        def predict(self, _face):
            predictions.append(1)
            return 0, 10.0

    context = SimpleNamespace(recognizer=CountingRecognizer(), label_map={})

    monkeypatch.setattr(recognize_face, "align_face", lambda *_: object())
    monkeypatch.setattr(recognize_face, "get_face_quality_issue", lambda _f: None)
    monkeypatch.setattr(recognize_face, "preprocess_for_lbph", lambda f: f)

    passed_track.refuse_attendance(NotRecorded.NOT_IN_CLASS.value)

    label, confidence, issue = recognize_face.predict_identity(
        context, object(), object(), passed_track, track_id=1
    )

    assert predictions == [], "a settled track must not run LBPH"
    assert (label, confidence, issue) == (-1, 999.0, None)


# ---------------------------------------------------------------------------
# Transient failures are a different thing
# ---------------------------------------------------------------------------


def test_a_database_error_is_retried_rather_than_latched(
    context, passed_track, monkeypatch
):
    """
    ⚠️ **The distinction the fix rests on.** An unreachable database has not
    refused anything. Latching it would mark a student absent for the duration
    of an outage that lasted one frame.
    """
    register = Register(NotRecorded.ERROR, "Present")
    monkeypatch.setattr(recognize_face, "save_attendance", register)

    frames = CONFIG.attendance_retry_frames + 2
    _id, _name, status, state = drive(context, passed_track, frames=frames)

    assert register.calls == 2, "the error must be retried once the backoff expires"
    assert state.attendance_saved
    assert status == "Present"
    assert context.recognized == {ALICE: "Present"}


def test_the_retry_is_paced_rather_than_every_frame(
    context, passed_track, monkeypatch
):
    """A database that just failed must not be asked again 4.8 times a second."""
    register = Register(NotRecorded.ERROR)
    monkeypatch.setattr(recognize_face, "save_attendance", register)

    frames = CONFIG.attendance_retry_frames * 4
    drive(context, passed_track, frames=frames)

    assert register.calls <= 5, (
        f"{register.calls} attempts in {frames} frames is the per-frame "
        f"hammering FS-17 is about, aimed at a database that is already down"
    )
    assert register.calls >= 2, "an outage must still be retried"


def test_the_student_is_told_while_the_retry_backs_off(
    context, passed_track, monkeypatch
):
    """The challenge is done; the screen must not imply there is more to do."""
    monkeypatch.setattr(
        recognize_face, "save_attendance", Register(NotRecorded.ERROR)
    )

    _id, _name, status, _state = drive(context, passed_track, frames=3)

    assert status == NotRecorded.ERROR.value


# ---------------------------------------------------------------------------
# The set that decides what is permanent
# ---------------------------------------------------------------------------


def test_error_is_not_a_permanent_refusal():
    """
    Pinned as a set rather than inferred, because getting this membership
    wrong is silent: ERROR in here marks a student absent for an outage, and
    NOT_IN_CLASS out of it restores the per-frame loop with every test green.
    """
    assert frozenset(
        {NotRecorded.NOT_IN_CLASS, NotRecorded.UNKNOWN_STUDENT}
    ) == recognize_face.PERMANENT_REFUSALS
    assert NotRecorded.ERROR not in recognize_face.PERMANENT_REFUSALS
