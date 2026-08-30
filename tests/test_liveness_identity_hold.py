"""
What a confirmed track does when LBPH stops agreeing with it (SE-12, UAT).

⚠️ **Reported from a UAT as "identity verified correctly, but on the liveness
check the same verified student keeps getting an unknown verdict."**

`process_confirmed_track()` had one negative branch:

    candidate_agrees = candidate_id is not None and candidate_id == locked_id

which gave the same weight to two completely different observations:

* **a contradiction** - LBPH read a *different enrolled student* on a track
  whose identity is locked. That is evidence of a track switch, which is the
  attack these counters exist to stop, and five frames of it should be fatal.
* **no evidence at all** - LBPH read nothing usable: a crop the quality gate
  refused, a face blurred by the very head movement the challenge just asked
  for, a distance over the threshold. This says nothing about identity.

Both fed `mismatch_frames`, so at 7-15 fps **two frames (0.13 s) redrew the
challenge and five (0.33-0.7 s) destroyed the identity** - on precisely the
frames the challenge itself produces. The student was told "Turn LEFT", turned,
and was dropped for it.

The fix splits the branch. These tests pin both halves, and the second half
matters as much as the first: relaxing the unreadable case must not relax the
case it was conflated with.

`vision/` is the preferred place to test (conftest.py), but this decision lives
in `recognize_face.py` - it is where the two counters, the challenge and the
attendance write meet. Importing it is allowed and cheap since PE-4.

⚠️ **`save_attendance()` is stubbed for the whole module.** It reaches the live
MySQL database, and lessons.md L6 is the story of a test that deleted two live
rows. Nothing here should reach it - every case is a *negative* frame - but a
test that proves a guard works must not depend on the guard working.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import recognize_face
from vision import liveness as liveness_module
from vision.tracking import TrackState

ALICE = "23-1-1-0559"
ALICE_NAME = "Alice"
BOB = "23-1-1-0918"

CONFIG = recognize_face.TRACK_CONFIG

# Straight ahead. Every case here is about the identity check, not about the
# pose, so the yaw is held at a value that satisfies nothing and advances no
# step - the challenge must not complete underneath a test about losing it.
NO_MOVEMENT = 0.0


@pytest.fixture(autouse=True)
def never_touch_the_database(monkeypatch):
    """See the module docstring - L6."""

    def refuse(*args, **kwargs):
        raise AssertionError(
            "save_attendance() was called. No case in this file should reach "
            "an attendance write; if one now legitimately does, stub it "
            "deliberately rather than removing this guard."
        )

    monkeypatch.setattr(recognize_face, "save_attendance", refuse)


@pytest.fixture
def context():
    return SimpleNamespace(
        subject=recognize_face.ActiveSubject(
            subject_id=1, session_id=1, subject_code="TEST-101"
        ),
        recognized=set(),
        label_map={},
    )


def confirmed_track():
    """A track locked onto Alice with a fresh challenge, as confirm() leaves it."""
    state = TrackState(config=CONFIG)
    state.valid_face_frames = CONFIG.real_face_confirm_frames
    state.confirm(ALICE, ALICE_NAME, recognize_face.create_liveness_state())
    return state


def drive(context, state, candidate_id, frames):
    """Run `frames` frames of the same observation. Returns the last result."""
    result = None

    for _ in range(frames):
        student_id, name, status, state = recognize_face.process_confirmed_track(
            context, state, candidate_id, NO_MOVEMENT
        )
        result = (student_id, name, status, state)

    return result


# ---------------------------------------------------------------------------
# No evidence: the face could not be read
# ---------------------------------------------------------------------------


def test_an_unreadable_frame_keeps_the_identity(context):
    """One bad frame is not an opinion about who this is."""
    state = confirmed_track()

    student_id, name, status, state = drive(context, state, None, frames=1)

    assert student_id == ALICE
    assert name == ALICE_NAME
    assert state.confirmed
    assert state.student_id == ALICE
    assert status.startswith("Hold still")


def test_a_run_of_unreadable_frames_survives_the_old_fatal_count(context):
    """
    ⚠️ **The regression.** Five consecutive unreadable frames used to destroy
    the identity, and five frames is a third of a second.
    """
    state = confirmed_track()
    frames = CONFIG.max_confirmed_mismatch_frames * 2

    assert frames < CONFIG.max_unreadable_frames_before_clear

    student_id, _name, _status, state = drive(context, state, None, frames=frames)

    assert student_id == ALICE, (
        f"{frames} unreadable frames dropped the locked identity. The old "
        f"code lost it after {CONFIG.max_confirmed_mismatch_frames}, which is "
        f"well under a second of the head movement the challenge asks for."
    )
    assert state.confirmed
    assert state.unreadable_frames == frames
    assert state.mismatch_frames == 0


def test_unreadable_frames_do_not_redraw_the_challenge(context):
    """
    The sequence must survive too, not just the identity.

    Redrawing put a fresh random prompt on screen while the student was still
    performing the previous one - so the challenge changed under them every
    two bad frames, which is the "it keeps asking me something different"
    half of the report.
    """
    state = confirmed_track()
    challenge = state.liveness
    sequence = challenge.sequence

    _student_id, _name, _status, state = drive(
        context, state, None, frames=CONFIG.liveness_reset_mismatch_frames * 3
    )

    assert state.liveness is challenge, "the challenge object was replaced"
    assert state.liveness.sequence == sequence
    assert state.liveness.restarts == 0


def test_unreadable_frames_do_eventually_give_up(context):
    """
    A track that has genuinely lost its face must not stay locked forever.

    The limit is generous, not absent: holding an identity on a track that is
    no longer looking at anybody would let the next person to walk into that
    box inherit it.
    """
    state = confirmed_track()

    student_id, _name, status, state = drive(
        context, state, None, frames=CONFIG.max_unreadable_frames_before_clear
    )

    assert student_id == "Unknown"
    assert status == "Identity check lost"
    assert not state.confirmed
    assert state.student_id is None

    # The "this is a real face" credit survives, exactly as reset_identity()
    # has always done - the object was never in doubt, only its name.
    assert state.valid_face_frames == CONFIG.real_face_confirm_frames


# ---------------------------------------------------------------------------
# A contradiction: a different enrolled student
# ---------------------------------------------------------------------------


def test_a_different_student_still_redraws_the_challenge(context):
    """
    ⚠️ **The case the split must NOT relax**, and the reason these counters
    exist: another face taking over a challenge already in progress.
    """
    state = confirmed_track()
    challenge = state.liveness

    _student_id, _name, _status, state = drive(
        context, state, BOB, frames=CONFIG.liveness_reset_mismatch_frames
    )

    assert state.liveness is not challenge, (
        "a different student on a locked track did not redraw the sequence - "
        "this is the track-switch guard and it must stay strict"
    )


def test_a_different_student_still_takes_the_identity_away(context):
    """The other half of the guard, at its original count."""
    state = confirmed_track()

    student_id, _name, status, state = drive(
        context, state, BOB, frames=CONFIG.max_confirmed_mismatch_frames
    )

    assert student_id == "Unknown"
    assert status == "Identity check lost"
    assert not state.confirmed
    assert state.student_id is None


def test_a_contradiction_is_fatal_far_sooner_than_an_unreadable_frame(context):
    """
    The two limits express different evidence, so they must not be equal.

    A test on the relationship rather than on the numbers: Phase 6 calibrates
    thresholds, and this property has to hold whatever it picks.
    """
    assert (
        CONFIG.max_confirmed_mismatch_frames
        < CONFIG.max_unreadable_frames_before_clear
    )


# ---------------------------------------------------------------------------
# The counters do not contaminate each other
# ---------------------------------------------------------------------------


def test_a_good_frame_clears_both_counters(context):
    """A frame that agrees is a clean slate, whichever way the doubt arose."""
    state = confirmed_track()

    _result = drive(context, state, None, frames=3)
    state = _result[3]
    _result = drive(context, state, BOB, frames=1)
    state = _result[3]

    assert state.unreadable_frames == 0, "a contradiction left stale doubt"
    assert state.mismatch_frames == 1

    student_id, _name, _status, state = drive(context, state, ALICE, frames=1)

    assert student_id == ALICE
    assert state.mismatch_frames == 0
    assert state.unreadable_frames == 0


def test_unreadable_frames_do_not_accumulate_towards_a_contradiction(context):
    """
    ⚠️ The conflation, stated directly.

    Enough unreadable frames to have been fatal before, followed by a single
    contradicting frame, must leave the identity intact - the contradiction
    count starts from one, not from where the doubt left off.

    ⚠️ The assertion is on `state`, not on the returned id. A contradicting
    frame has always drawn "Unknown" while the lock survives underneath, and
    that is correct: the overlay must not put Alice's name on a frame that
    just read somebody else. What is being pinned here is the *counter*.
    """
    state = confirmed_track()

    _result = drive(
        context, state, None, frames=CONFIG.max_confirmed_mismatch_frames + 2
    )
    state = _result[3]

    assert state.confirmed

    _student_id, _name, _status, state = drive(context, state, BOB, frames=1)

    assert state.confirmed and state.student_id == ALICE, (
        "one contradicting frame after a run of unreadable ones dropped the "
        "identity - the counters are sharing state again"
    )
    assert state.mismatch_frames == 1


# ---------------------------------------------------------------------------
# The challenge must advance on frames LBPH could not read
#
# ⚠️ **The second half of the same defect, found in Phase 6c and left open.**
# The tests above pin that an unreadable frame does not *destroy* the identity
# or *redraw* the sequence. Neither of them asks whether it lets the sequence
# move forward - and it did not.
#
# `process_confirmed_track()` returned from its `candidate_id is None` branch
# before `state.liveness.update()` was ever reached, so the challenge advanced
# only on frames that produced a usable LBPH match. A head turned to satisfy
# "TURN LEFT" is motion-blurred while it moves, and a blurred crop is what
# RECOGNITION_QUALITY.min_blur_variance refuses; a profile view also pushes the
# LBPH distance over the threshold. Both routes give `candidate_id is None`.
#
# So the system asked for a movement and then ignored the frames that movement
# produced, while an 8-second wall-clock step timeout ran regardless. Measured
# in a live session: 4 min 07 s to record one student, of which the liveness
# timeouts were 37 s and the identity churn they caused was 3 min 03 s.
#
# The yaw needs nothing from LBPH. `get_face_yaw()` reads the FaceMesh
# landmarks, which are present whenever MediaPipe found a face at all - which
# is the precondition for being in this function in the first place.
# ---------------------------------------------------------------------------

LIVENESS = recognize_face.LIVENESS_CONFIG


def yaw_satisfying(step):
    """A yaw-over-face-width that completes `step`, comfortably clear of it."""
    if step == liveness_module.LEFT:
        return LIVENESS.turn_of_face_width * 2

    if step == liveness_module.RIGHT:
        return -LIVENESS.turn_of_face_width * 2

    return 0.0


def test_an_unreadable_frame_still_advances_the_challenge(context):
    """
    ⚠️ **The regression, stated as one frame.**

    One frame, carrying a pose that satisfies the step being asked for, and no
    identity. The step must complete. Before the fix the index never moved,
    because the branch returned first.
    """
    state = confirmed_track()
    challenge = state.liveness

    step = challenge.current_step
    index_before = challenge.index

    recognize_face.process_confirmed_track(
        context, state, None, yaw_satisfying(step)
    )

    assert state.liveness.index == index_before + 1, (
        "a frame that showed the requested pose did not advance the "
        "challenge, because LBPH could not read an identity on it - which is "
        "the frame a head turn actually produces"
    )


def test_the_whole_challenge_can_be_passed_without_a_single_readable_frame(context):
    """
    The end-to-end shape of the defect.

    A student who performs every step correctly, while every frame of the
    movement is too blurred for LBPH, must still get through the sequence.
    Before the fix this was unpassable - the challenge could only advance on
    frames where the student was *not* moving, so it depended on catching them
    between the two poses it had just asked for.

    ⚠️ The identity is deliberately never re-confirmed here: `passed` is the
    assertion, not `identity_reconfirmed`. Re-establishing identity after the
    challenge is what `post_match_frames` is for, and it is unchanged - it
    still requires real matching frames.
    """
    state = confirmed_track()
    challenge = state.liveness

    assert not challenge.passed

    for _ in range(len(challenge.sequence)):
        step = state.liveness.current_step

        if step is None:
            break

        recognize_face.process_confirmed_track(
            context, state, None, yaw_satisfying(step)
        )

    assert state.liveness.passed, (
        "the sequence could not be completed on unreadable frames alone"
    )
    assert state.liveness.restarts == 0, "a step timed out and was redrawn"
    assert state.confirmed and state.student_id == ALICE


def test_advancing_on_unreadable_frames_does_not_confirm_anybody(context):
    """
    ⚠️ **The guard on the fix.** Passing the challenge is not being recognised.

    Nothing about advancing a pose sequence on unreadable frames may satisfy
    the post-challenge identity re-check, because that check exists precisely
    to establish that the face which performed the movement is still the face
    the track is locked to. `save_attendance()` is stubbed to raise for this
    whole module, so an attendance write here fails loudly rather than
    silently recording somebody.
    """
    state = confirmed_track()

    for _ in range(len(state.liveness.sequence) + LIVENESS.post_match_frames * 2):
        step = state.liveness.current_step
        yaw = yaw_satisfying(step) if step is not None else 0.0

        recognize_face.process_confirmed_track(context, state, None, yaw)

    assert state.liveness.passed
    assert not state.liveness.identity_reconfirmed, (
        "unreadable frames re-confirmed an identity - attendance could be "
        "recorded for a face the recogniser never actually read"
    )
    assert state.liveness.post_match_count == 0
    assert not state.attendance_saved
