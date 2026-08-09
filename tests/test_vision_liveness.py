"""
The randomised multi-step liveness challenge (SE-12).

Two things are under test, and the second matters more than the first:

1. **The sequence logic** - ordered, randomised, per-step timeouts, and the
   post-liveness identity re-check.
2. **The unit fix.** The old thresholds compared a *frame-normalised* yaw
   against a constant, while the geometry gate that decides whether a face is
   acceptable works in *face widths*. Those do not scale together, so past
   roughly a 137 px face box the challenge demanded more turn than the gate
   would accept and a student standing a normal distance back could never pass
   it - with the box still drawn green and nothing logged.

   The measured numbers behind that are in `vision/liveness.py`. The tests at
   the bottom of this file replay them: the turn a person actually makes is a
   constant ~0.14 of face width at every distance, the old rule rejected that
   at realistic distances, and the new one accepts it at all of them.

No camera, no MediaPipe, no clock: yaw traces are synthetic and time is
injected, so the whole file runs in milliseconds.
"""

from __future__ import annotations

import random

import pytest

from vision.liveness import (
    ALL_STEPS,
    CENTER,
    LEFT,
    RIGHT,
    LivenessChallenge,
    LivenessConfig,
    draw_sequence,
)

CONFIG = LivenessConfig()

# Comfortably past each threshold, so a test that fails is failing about the
# logic rather than about sitting on a boundary.
TURNED_LEFT = CONFIG.turn_of_face_width * 1.5
TURNED_RIGHT = -CONFIG.turn_of_face_width * 1.5
CENTERED = 0.0


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def challenge(sequence, clock=None, config=CONFIG):
    return LivenessChallenge(
        config=config,
        sequence=tuple(sequence),
        clock=clock or FakeClock(),
    )


def drive(live, yaws):
    for yaw in yaws:
        live.update(yaw)
    return live


# ---------------------------------------------------------------------------
# The sequence is randomised
# ---------------------------------------------------------------------------


def test_a_sequence_has_the_configured_number_of_steps():
    assert len(draw_sequence(CONFIG)) == CONFIG.steps


def test_the_steps_in_a_sequence_are_distinct():
    """
    "LEFT then LEFT" asks for no second movement - the subject is already
    there - so it would be a one-step challenge wearing a two-step label.
    """
    for _ in range(200):
        sequence = draw_sequence(CONFIG)
        assert len(set(sequence)) == len(sequence)


def test_every_step_comes_from_the_declared_set():
    for _ in range(200):
        assert set(draw_sequence(CONFIG)) <= set(ALL_STEPS)


def test_the_sequence_actually_varies():
    """
    The whole point of SE-12. A fixed challenge is defeated by one recording
    of one head turn; this has to draw more than a single answer.
    """
    seen = {draw_sequence(CONFIG, rng=random.Random(seed)) for seed in range(200)}

    assert len(seen) > 1, "the challenge is not randomised at all"
    # 3 steps taken 2 at a time, ordered, is 6 possibilities.
    assert len(seen) == 6, f"expected all 6 orderings to appear, saw {sorted(seen)}"


def test_a_seeded_generator_is_reproducible():
    """So a failing run can be replayed."""
    first = draw_sequence(CONFIG, rng=random.Random(1234))
    second = draw_sequence(CONFIG, rng=random.Random(1234))

    assert first == second


def test_a_new_challenge_draws_its_own_sequence():
    live = LivenessChallenge(config=CONFIG, clock=FakeClock())

    assert len(live.sequence) == CONFIG.steps
    assert live.current_step == live.sequence[0]
    assert not live.passed


# ---------------------------------------------------------------------------
# The sequence must be performed, in order
# ---------------------------------------------------------------------------


def test_the_full_sequence_passes():
    live = challenge([LEFT, CENTER])

    drive(live, [TURNED_LEFT, CENTERED])

    assert live.passed
    assert live.current_step is None


def test_a_partially_performed_sequence_does_not_pass():
    live = challenge([LEFT, CENTER])

    drive(live, [TURNED_LEFT])

    assert not live.passed
    assert live.current_step == CENTER


def test_doing_only_the_second_step_does_not_pass():
    """
    Order is the security property. Satisfying step two while step one is
    outstanding must not advance anything.
    """
    live = challenge([LEFT, RIGHT])

    drive(live, [TURNED_RIGHT] * 20)

    assert not live.passed
    assert live.index == 0
    assert live.current_step == LEFT


def test_the_reverse_order_does_not_pass():
    live = challenge([LEFT, RIGHT])

    drive(live, [TURNED_RIGHT, TURNED_LEFT])

    # The LEFT eventually satisfies step one, but RIGHT is still outstanding.
    assert not live.passed
    assert live.current_step == RIGHT


def test_a_pose_held_for_many_frames_advances_only_one_step():
    """
    Frames arrive at 15-30 fps, so one held pose is dozens of updates. It must
    not walk the whole sequence on its own.
    """
    live = challenge([LEFT, RIGHT])

    drive(live, [TURNED_LEFT] * 50)

    assert live.index == 1
    assert not live.passed


def test_travelling_through_centre_between_two_turns_is_not_a_failure():
    """
    Moving from LEFT to RIGHT necessarily passes through centre. Failing a
    student for the path their head took between two requested positions
    would be a false rejection with no security value - and a false rejection
    here means a student marked absent for a class they attended.
    """
    live = challenge([LEFT, RIGHT])

    drive(live, [TURNED_LEFT, 0.05, CENTERED, -0.05, TURNED_RIGHT])

    assert live.passed


def test_all_six_orderings_can_be_completed():
    """No sequence the draw can produce is impossible to perform."""
    answers = {LEFT: TURNED_LEFT, RIGHT: TURNED_RIGHT, CENTER: CENTERED}

    for first in ALL_STEPS:
        for second in ALL_STEPS:
            if first == second:
                continue

            live = challenge([first, second])
            drive(live, [answers[first], answers[second]])

            assert live.passed, f"{first} then {second} could not be completed"


def test_updates_after_passing_are_ignored():
    live = challenge([LEFT, CENTER])
    drive(live, [TURNED_LEFT, CENTERED])

    drive(live, [TURNED_RIGHT] * 10)

    assert live.passed


# ---------------------------------------------------------------------------
# Timeouts
# ---------------------------------------------------------------------------


def test_a_step_that_takes_too_long_restarts_the_challenge():
    clock = FakeClock()
    live = challenge([LEFT, CENTER], clock=clock)

    clock.advance(CONFIG.step_timeout_seconds + 0.1)
    live.update(CENTERED)

    assert live.restarts == 1
    assert live.index == 0
    assert not live.passed


def test_a_restart_draws_a_fresh_sequence():
    """
    A sequence on screen long enough to time out has been on screen long
    enough to be read by whoever is holding up a recording, so retrying the
    same one gives them a second attempt at a known answer.
    """
    clock = FakeClock()
    sequences = set()

    for _ in range(200):
        live = LivenessChallenge(config=CONFIG, clock=clock, rng=random.Random())
        clock.advance(CONFIG.step_timeout_seconds + 0.1)
        live.update(CENTERED)
        sequences.add(live.sequence)

    assert len(sequences) > 1, "a restart always reissues the same sequence"


def test_the_timeout_is_per_step_not_per_challenge():
    """
    A two-step challenge must not fail because the two steps together took
    longer than one step's budget - the clock restarts when a step is met.
    """
    clock = FakeClock()
    live = challenge([LEFT, CENTER], clock=clock)

    clock.advance(CONFIG.step_timeout_seconds * 0.9)
    live.update(TURNED_LEFT)

    clock.advance(CONFIG.step_timeout_seconds * 0.9)
    live.update(CENTERED)

    assert live.passed
    assert live.restarts == 0


def test_a_restart_clears_progress_toward_the_re_check():
    clock = FakeClock()
    live = challenge([LEFT, CENTER], clock=clock)
    drive(live, [TURNED_LEFT, CENTERED])
    live.note_identity_match()

    live.restart()

    assert not live.passed
    assert live.post_match_count == 0


# ---------------------------------------------------------------------------
# The post-liveness identity re-check
# ---------------------------------------------------------------------------


def test_passing_the_sequence_is_not_enough_on_its_own():
    """
    Liveness proves something moved, not who it was. The identity has to still
    match for a further run of frames before attendance is written - which is
    what stops one person completing the challenge for another's locked track.
    """
    live = challenge([LEFT, CENTER])
    drive(live, [TURNED_LEFT, CENTERED])

    assert live.passed
    assert not live.identity_reconfirmed


def test_the_re_check_completes_after_enough_matching_frames():
    live = challenge([LEFT, CENTER])
    drive(live, [TURNED_LEFT, CENTERED])

    for _ in range(CONFIG.post_match_frames):
        live.note_identity_match()

    assert live.identity_reconfirmed


def test_a_mismatch_resets_the_re_check():
    live = challenge([LEFT, CENTER])
    drive(live, [TURNED_LEFT, CENTERED])

    for _ in range(CONFIG.post_match_frames - 1):
        live.note_identity_match()

    live.note_identity_mismatch()

    assert not live.identity_reconfirmed
    assert live.post_match_count == 0


def test_matches_before_the_sequence_passes_do_not_count():
    live = challenge([LEFT, CENTER])

    for _ in range(50):
        live.note_identity_match()

    assert live.post_match_count == 0


# ---------------------------------------------------------------------------
# What the operator is told
# ---------------------------------------------------------------------------


def test_the_message_names_the_step_and_the_position_in_the_sequence():
    live = challenge([RIGHT, CENTER])

    assert live.message() == "Liveness 1/2: Turn RIGHT"

    live.update(TURNED_RIGHT)

    assert live.message() == "Liveness 2/2: Look at the camera"


def test_the_message_reports_the_recheck_then_success():
    live = challenge([LEFT, CENTER])
    drive(live, [TURNED_LEFT, CENTERED])

    assert "Rechecking 0/8" in live.message()

    for _ in range(CONFIG.post_match_frames):
        live.note_identity_match()

    assert live.message() == "Liveness Passed"


def test_every_message_keeps_the_prefix_the_overlay_colours_on():
    """
    `recognize_face.status_color()` paints the box by matching on the start of
    the status string. A reworded message that stops starting with "Liveness"
    silently turns the overlay red, which reads to the operator as a rejection.
    """
    for first in ALL_STEPS:
        for second in ALL_STEPS:
            if first == second:
                continue

            live = challenge([first, second])

            assert live.message().startswith("Liveness")


# ---------------------------------------------------------------------------
# Configuration is coherent
# ---------------------------------------------------------------------------


def test_overlapping_thresholds_are_refused():
    """
    If the centre band reached the turn threshold, one pose would satisfy both
    a LEFT step and a CENTER step and the sequence would mean nothing.
    """
    with pytest.raises(ValueError):
        LivenessConfig(turn_of_face_width=0.05, center_of_face_width=0.06)


def test_asking_for_more_steps_than_there_are_is_refused():
    with pytest.raises(ValueError):
        LivenessConfig(steps=len(ALL_STEPS) + 1)


def test_the_turn_threshold_sits_below_what_the_gate_allows():
    """
    The geometry gate refuses a nose past 0.28 face-widths. A liveness
    threshold at or above that asks for a turn that gets the face rejected
    before the turn can be credited - which is exactly the bug SE-12 fixes.
    """
    from vision.validation import RECOGNITION_PROFILE

    assert CONFIG.turn_of_face_width < RECOGNITION_PROFILE.max_nose_offset_of_width


# ---------------------------------------------------------------------------
# The distance bug, replayed from the measurements
# ---------------------------------------------------------------------------

# Measured by driving the real MediaPipe mesh over the enrolment set's
# deliberate left-turn images composited at a range of sizes. The middle
# column is the turn a person actually makes; it is a constant fraction of
# face width, because it is a fact about heads and not about cameras.
MEASURED_TURNS = [
    # (face box width px, turn actually made as a fraction of face width)
    (208, 0.136),
    (260, 0.148),
    (342, 0.144),
    (422, 0.144),
    (508, 0.154),
]

OLD_FRAME_NORMALISED_THRESHOLD = 0.030
FRAME_WIDTH = 1280


@pytest.mark.parametrize(("box_width", "turn_made"), MEASURED_TURNS)
def test_a_real_turn_passes_at_every_distance_now(box_width, turn_made):
    """The fix, at each measured distance."""
    live = challenge([LEFT, CENTER])

    drive(live, [turn_made, CENTERED])

    assert live.passed, (
        f"a real turn of {turn_made}w was not detected at a {box_width} px "
        f"face box"
    )


def test_the_old_rule_was_unreachable_at_a_realistic_distance():
    """
    Not a test of current behaviour - a record of why this changed, so nobody
    reverts the unit change as cosmetic.

    At a 208 px face box the old frame-normalised threshold worked out at
    0.185 of face width, and the turn a person actually makes is 0.136w. The
    student turns their head, the gate keeps accepting them, and the challenge
    never completes.
    """
    box_width, turn_made = MEASURED_TURNS[0]

    old_threshold_in_face_widths = (
        OLD_FRAME_NORMALISED_THRESHOLD * FRAME_WIDTH / box_width
    )

    assert old_threshold_in_face_widths > turn_made, (
        "the fixture no longer reproduces the case this fixes"
    )
    assert CONFIG.turn_of_face_width < turn_made, (
        "the new threshold does not clear a real turn at this distance"
    )


def test_the_old_rule_demanded_more_turn_than_the_gate_allowed_when_far_away():
    """
    The sharp end of it. Below roughly a 137 px face box, the old threshold
    required the nose to move further than the geometry gate's 0.28w ceiling -
    so the face would be refused for turning too far before it could be
    credited with turning far enough. Unwinnable, not merely hard.
    """
    from vision.validation import RECOGNITION_PROFILE

    crossover = (
        OLD_FRAME_NORMALISED_THRESHOLD
        * FRAME_WIDTH
        / RECOGNITION_PROFILE.max_nose_offset_of_width
    )

    assert 130 < crossover < 145, f"crossover moved to {crossover:.0f} px"

    small_box = 120
    old_threshold_in_face_widths = (
        OLD_FRAME_NORMALISED_THRESHOLD * FRAME_WIDTH / small_box
    )

    assert (
        old_threshold_in_face_widths
        > RECOGNITION_PROFILE.max_nose_offset_of_width
    )
