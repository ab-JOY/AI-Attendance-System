"""
Tests for the per-track verification state machine (MA-12) and tracker (RE-2).

None of this could be tested before. The state machine lived inline in
`generate_frames()` - 405 lines at ten levels of nesting - operating on plain
dicts held in module globals, reachable only by running a camera. The
lifecycle it implements is the security property of the whole system: a track
must look like a real face for several frames, agree with itself across a
window of predictions, and then pass liveness *while still matching the same
identity*, before an attendance row is written.

`tests/conftest.py` forbids importing `recognize_face`; nothing here does.
`vision.tracking` has no camera, no MediaPipe, no OpenCV and no database in
it, which is the point of the extraction.
"""

from __future__ import annotations

import pytest

from vision.geometry import FaceBox
from vision.tracking import FaceTracker, TrackConfig, TrackState

CONFIG = TrackConfig()

ALICE = "SEC-TEST-NOBODY"
BOB = "SEC-TEST-NOONE"


# Identifiers here are fake on purpose. lessons.md L6: a negative test is an
# execution of the code path it is proving, and a real student ID in a test
# is how two live database rows were deleted during Phase 2.


def state() -> TrackState:
    return TrackState(config=CONFIG)


def vote(track: TrackState, student_id: str, times: int, confidence: float = 30.0):
    for _ in range(times):
        track.add_prediction(student_id, f"{student_id} Name", confidence)


# ---------------------------------------------------------------------------
# Becoming a real face at all
# ---------------------------------------------------------------------------


def test_a_new_track_is_provisional():
    """A fresh detection is not drawn and not recognised."""
    track = state()
    assert track.is_provisional


def test_a_track_stops_being_provisional_after_enough_valid_frames():
    track = state()

    for _ in range(CONFIG.real_face_confirm_frames):
        assert track.is_provisional
        track.note_valid_frame()

    assert not track.is_provisional


# ---------------------------------------------------------------------------
# Accumulating evidence
# ---------------------------------------------------------------------------


def test_empty_history_has_no_verdict():
    assert state().evaluate_history() is None


def test_a_unanimous_history_reports_full_agreement():
    track = state()
    vote(track, ALICE, CONFIG.prediction_history_size, confidence=30.0)

    verdict = track.evaluate_history()

    assert verdict is not None
    assert verdict.dominant_id == ALICE
    assert verdict.agreement_ratio == 1.0
    assert verdict.history_count == CONFIG.prediction_history_size
    assert verdict.average_confidence == pytest.approx(30.0)


def test_average_confidence_covers_only_the_dominant_identity():
    """
    A minority vote must not drag the winner's average distance down.

    Averaging across every entry would let a few very close matches for the
    wrong person help the right person clear CONFIRMATION_CONFIDENCE.
    """
    track = state()
    vote(track, ALICE, 15, confidence=40.0)
    vote(track, BOB, 5, confidence=10.0)

    verdict = track.evaluate_history()

    assert verdict.dominant_id == ALICE
    assert verdict.average_confidence == pytest.approx(40.0)
    assert verdict.agreement_ratio == pytest.approx(0.75)


def test_history_is_bounded_by_the_window():
    track = state()
    vote(track, ALICE, CONFIG.prediction_history_size * 3)

    assert len(track.history) == CONFIG.prediction_history_size


def test_consecutive_count_resets_when_the_identity_changes():
    track = state()

    vote(track, ALICE, 5)
    assert track.consecutive_id == ALICE
    assert track.consecutive_count == 5

    vote(track, BOB, 1)
    assert track.consecutive_id == BOB
    assert track.consecutive_count == 1


# ---------------------------------------------------------------------------
# Confirmation - all four conditions are required
# ---------------------------------------------------------------------------


def full_agreement_track() -> TrackState:
    track = state()
    vote(track, ALICE, CONFIG.prediction_history_size, confidence=30.0)
    return track


def test_a_full_confident_consistent_history_confirms():
    track = full_agreement_track()
    assert track.can_confirm(track.evaluate_history(), set())


def test_a_partial_history_does_not_confirm():
    track = state()
    vote(track, ALICE, CONFIG.prediction_history_size - 1, confidence=30.0)

    assert not track.can_confirm(track.evaluate_history(), set())


def test_a_divided_history_does_not_confirm():
    track = state()
    # 14/20 agreement is 0.70, below MIN_LABEL_AGREEMENT of 0.85.
    vote(track, ALICE, 14, confidence=30.0)
    vote(track, BOB, 6, confidence=30.0)

    assert not track.can_confirm(track.evaluate_history(), set())


def test_a_weak_average_distance_does_not_confirm():
    """
    A window whose average is worse than the confirmation bar does not lock.

    ⚠️ This used to be written with confidence=57.0 and a comment reading
    "below RECOGNITION_THRESHOLD (58.0) but above CONFIRMATION_CONFIDENCE
    (52.0): good enough to vote with, not good enough to lock an identity."

    That gap was a live bug, not a design. A frame is voted in at or under
    58.0 and the window average had to beat 52.0, so a genuine unanimous
    20/20 window averaging anywhere in between could never confirm - reported
    from a live run at 56.1, with the box yellow and nothing in any log.
    Production now derives the bar from the recognition threshold; see
    test_the_confirmation_bar_is_not_stricter_than_the_match_threshold.

    The rule itself is still real, so it is still tested - just with a
    distance that is unambiguously bad rather than one sitting in a band that
    should not exist.
    """
    track = state()
    vote(track, ALICE, CONFIG.prediction_history_size, confidence=95.0)

    verdict = track.evaluate_history()

    assert verdict.average_confidence > CONFIG.confirmation_confidence
    assert not track.can_confirm(verdict, set())
    assert track.confirmation_blocker(verdict, set()) == "distance"


def test_the_confirmation_bar_is_not_stricter_than_the_match_threshold():
    """
    The regression guard for the stuck-at-Verifying bug.

    Any gap between "good enough to vote with" and "good enough to confirm"
    is a band in which a student is recognised on every single frame and can
    never be marked present, with no error raised and nothing logged. If a
    future calibration reintroduces one deliberately, it has to come with a
    DET curve and this test has to be changed on purpose.

    Measured before closing it (docs/benchmarks.md): 750 impostor images from
    50 unenrolled subjects score 62.2 at the closest, so there was nothing in
    the 52.0-58.0 band to protect against.
    """
    import recognize_face

    assert (
        recognize_face.TRACK_CONFIG.confirmation_confidence
        >= recognize_face.RECOGNITION_THRESHOLD
    ), (
        "The confirmation bar is stricter than the match threshold. Every "
        "student whose average distance lands between them will verify "
        "forever and never be recorded."
    )


def test_the_reported_stuck_track_now_confirms():
    """
    The exact case from the live run: 20/20 frames, 100% agreement, average
    distance 56.1. Before the fix this returned False on every frame forever
    and the operator saw "Verifying 20/20 100% (56.1)" indefinitely.

    Driven with the *production* config rather than TrackConfig()'s defaults,
    because the bug was in how production assembled it.
    """
    import recognize_face

    track = TrackState(config=recognize_face.TRACK_CONFIG)
    vote(
        track,
        ALICE,
        recognize_face.TRACK_CONFIG.prediction_history_size,
        confidence=56.1,
    )

    verdict = track.evaluate_history()

    assert verdict.history_count == 20
    assert verdict.agreement_ratio == 1.0
    assert verdict.average_confidence == pytest.approx(56.1)

    assert track.confirmation_blocker(verdict, set()) is None
    assert track.can_confirm(verdict, set())


@pytest.mark.parametrize(
    ("blocker", "setup"),
    [
        ("window", lambda t: vote(t, ALICE, 5)),
        ("distance", lambda t: vote(t, ALICE, CONFIG.prediction_history_size, 95.0)),
    ],
)
def test_confirmation_blocker_names_the_unmet_condition(blocker, setup):
    track = state()
    setup(track)

    assert track.confirmation_blocker(track.evaluate_history(), set()) == blocker


def test_confirmation_blocker_reports_a_claimed_identity():
    track = state()
    vote(track, ALICE, CONFIG.prediction_history_size)

    verdict = track.evaluate_history()

    assert track.confirmation_blocker(verdict, {ALICE}) == "claimed"
    assert track.confirmation_blocker(verdict, set()) is None


def test_confirmation_blocker_and_can_confirm_cannot_disagree():
    """
    can_confirm() is defined through confirmation_blocker(), so this is really
    asserting that the definition stays that way rather than drifting into two
    copies of the rules - which is how the 52.0/58.0 pair came about.
    """
    track = state()

    for count in range(0, CONFIG.prediction_history_size + 1):
        track = state()
        vote(track, ALICE, count)

        verdict = track.evaluate_history()

        if verdict is None:
            continue

        assert track.can_confirm(verdict, set()) == (
            track.confirmation_blocker(verdict, set()) is None
        )


def test_a_short_consecutive_run_does_not_confirm():
    """
    A full, agreeing window is not enough on its own.

    Interleaving keeps agreement high while the consecutive run stays at 1,
    which is what a track flickering between two faces looks like.
    """
    track = state()

    for _ in range(CONFIG.prediction_history_size // 2):
        vote(track, ALICE, 1, confidence=30.0)
        vote(track, BOB, 1, confidence=30.0)

    vote(track, ALICE, CONFIG.prediction_history_size, confidence=30.0)
    assert track.can_confirm(track.evaluate_history(), set())

    flickering = state()
    for _ in range(CONFIG.prediction_history_size):
        vote(flickering, ALICE, 1, confidence=30.0)
        vote(flickering, BOB, 1, confidence=30.0)

    assert flickering.consecutive_count < CONFIG.min_consecutive_identity_frames
    assert not flickering.can_confirm(flickering.evaluate_history(), set())


def test_an_identity_already_claimed_in_this_frame_does_not_confirm():
    """
    Two tracks cannot both become the same student in one frame.

    This is the guard against holding up a photograph of an enrolled student
    beside your own face.
    """
    track = full_agreement_track()
    assert not track.can_confirm(track.evaluate_history(), {ALICE})


# ---------------------------------------------------------------------------
# Losing an identity
# ---------------------------------------------------------------------------


def test_weak_frames_clear_the_history():
    track = state()
    vote(track, ALICE, CONFIG.prediction_history_size)

    for _ in range(CONFIG.max_weak_frames_before_clear):
        track.note_weak_prediction()

    assert len(track.history) == 0
    assert track.consecutive_id is None
    assert track.consecutive_count == 0


def test_a_single_weak_frame_does_not_clear_the_history():
    track = state()
    vote(track, ALICE, CONFIG.prediction_history_size)
    track.note_weak_prediction()

    assert len(track.history) == CONFIG.prediction_history_size


def test_a_good_prediction_resets_the_weak_counter():
    track = state()
    track.note_weak_prediction()
    track.note_weak_prediction()
    vote(track, ALICE, 1)

    assert track.weak_frames == 0


def test_reset_identity_keeps_the_real_face_credit():
    """
    Losing an identity must not send the track back to "unproven object".

    Otherwise a student who is briefly misread disappears from the screen
    entirely for several frames instead of returning to "Verifying".
    """
    track = state()
    for _ in range(CONFIG.real_face_confirm_frames + 3):
        track.note_valid_frame()

    track.confirm(ALICE, "Alice", liveness={"passed": False})
    fresh = track.reset_identity()

    assert fresh.valid_face_frames == track.valid_face_frames
    assert not fresh.is_provisional
    assert fresh.student_id is None
    assert fresh.confirmed is False
    assert len(fresh.history) == 0


# ---------------------------------------------------------------------------
# Confirmed and completed tracks
# ---------------------------------------------------------------------------


def test_confirm_locks_the_identity_and_starts_liveness():
    track = state()
    liveness = {"challenge": "TURN_LEFT", "passed": False}

    track.confirm(ALICE, "Alice", liveness)

    assert track.confirmed
    assert track.student_id == ALICE
    assert track.attendance_saved is False
    assert track.liveness is liveness
    assert track.mismatch_frames == 0


def test_adopting_a_completed_identity_skips_liveness():
    """
    A student who steps out of frame and back in keeps their mark.

    They already passed liveness for it; asking again would mean a second
    challenge for an attendance row that is already written.
    """
    track = state()
    vote(track, ALICE, 5)

    track.adopt_completed_identity(ALICE, "Alice")

    assert track.confirmed
    assert track.attendance_saved
    assert len(track.history) == 0


# ---------------------------------------------------------------------------
# What the overlay is told (FS-8, FS-7)
#
# `save_attendance()` derives Present or Late from the subject's scheduled
# start. The overlay used to say "Present" unconditionally, which was true only
# because the loop passed a hardcoded "Present" and nothing else could ever be
# written. Once the schedule decides, the screen and the register have to agree.
# ---------------------------------------------------------------------------


def test_a_new_track_reads_as_present_until_something_is_recorded():
    """The default is the pre-existing behaviour, not a new claim."""
    assert state().recorded_status == "Present"


def test_the_overlay_reports_the_status_that_was_recorded():
    track = state()
    track.confirm(ALICE, "Alice", liveness=None)

    track.attendance_saved = True
    track.attendance_status = "Late"

    assert track.recorded_status == "Late", (
        "a student marked Late in the register was shown as Present on the "
        "overlay - two screens disagreeing about one session (FS-7)"
    )


def test_confirming_clears_a_status_from_a_previous_identity():
    """
    A track that loses its identity and locks a new one must not inherit the
    previous student's status - `confirm()` resets `attendance_saved`, and the
    status has to travel with it or it outlives the mark it describes.
    """
    track = state()
    track.attendance_saved = True
    track.attendance_status = "Late"

    track.confirm(BOB, "Bob", liveness=None)

    assert track.attendance_saved is False
    assert track.attendance_status is None
    assert track.recorded_status == "Present"


def test_an_adopted_identity_falls_back_rather_than_inventing_a_status():
    """
    `adopt_completed_identity()` inherits a mark made by a track this one never
    saw, so the status is genuinely unknown here. Falling back to "Present" is
    the old behaviour; guessing "Late" would be a fabrication on screen.
    """
    track = state()
    track.adopt_completed_identity(ALICE, "Alice")

    assert track.attendance_status is None
    assert track.recorded_status == "Present"


# ---------------------------------------------------------------------------
# FaceTracker
# ---------------------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_a_first_detection_creates_a_track():
    tracker = FaceTracker(config=CONFIG, clock=FakeClock())
    track_id = tracker.assign(FaceBox(100, 100, 200, 220), set())

    assert track_id == 0
    assert len(tracker) == 1


def test_an_overlapping_detection_continues_the_same_track():
    tracker = FaceTracker(config=CONFIG, clock=FakeClock())

    first = tracker.assign(FaceBox(100, 100, 200, 220), set())
    second = tracker.assign(FaceBox(104, 103, 205, 224), set())

    assert first == second
    assert len(tracker) == 1


def test_a_distant_detection_starts_a_new_track():
    tracker = FaceTracker(config=CONFIG, clock=FakeClock())

    first = tracker.assign(FaceBox(100, 100, 200, 220), set())
    second = tracker.assign(FaceBox(900, 500, 1000, 620), set())

    assert first != second
    assert len(tracker) == 2


def test_a_track_already_used_this_frame_is_not_reused():
    """
    Two faces in one frame get two tracks even when they overlap.

    `used_track_ids` is what stops the second detection in a frame from
    stealing the identity of the first.
    """
    tracker = FaceTracker(config=CONFIG, clock=FakeClock())

    first = tracker.assign(FaceBox(100, 100, 200, 220), set())
    second = tracker.assign(FaceBox(104, 103, 205, 224), {first})

    assert first != second


def test_a_much_larger_face_does_not_continue_a_small_track():
    """Size similarity guards against a track jumping to a nearer face."""
    tracker = FaceTracker(config=CONFIG, clock=FakeClock())

    first = tracker.assign(FaceBox(100, 100, 180, 190), set())
    second = tracker.assign(FaceBox(100, 100, 500, 560), set())

    assert first != second


def test_stale_tracks_expire():
    clock = FakeClock()
    tracker = FaceTracker(config=CONFIG, clock=clock)

    track_id = tracker.assign(FaceBox(100, 100, 200, 220), set())
    tracker.state_for(track_id).note_valid_frame()

    clock.advance(CONFIG.timeout_seconds + 0.1)
    tracker.expire_old_tracks()

    assert len(tracker) == 0
    assert track_id not in tracker.states


def test_an_expired_track_id_is_not_reissued():
    """
    Track ids are monotonic.

    Reusing an id would hand a new face the previous occupant's verification
    state, which is the identity-transfer bug the tracker exists to prevent.
    """
    clock = FakeClock()
    tracker = FaceTracker(config=CONFIG, clock=clock)

    first = tracker.assign(FaceBox(100, 100, 200, 220), set())

    clock.advance(CONFIG.timeout_seconds + 0.1)
    tracker.expire_old_tracks()

    second = tracker.assign(FaceBox(100, 100, 200, 220), set())

    assert second != first


def test_state_for_is_stable_across_frames():
    tracker = FaceTracker(config=CONFIG, clock=FakeClock())
    track_id = tracker.assign(FaceBox(100, 100, 200, 220), set())

    tracker.state_for(track_id).note_valid_frame()
    tracker.state_for(track_id).note_valid_frame()

    assert tracker.state_for(track_id).valid_face_frames == 2


def test_reset_clears_everything():
    tracker = FaceTracker(config=CONFIG, clock=FakeClock())
    tracker.assign(FaceBox(100, 100, 200, 220), set())
    tracker.assign(FaceBox(900, 500, 1000, 620), set())

    tracker.reset()

    assert len(tracker) == 0
    assert tracker.states == {}
    # Ids restart, because a reset means a new attendance session.
    assert tracker.assign(FaceBox(100, 100, 200, 220), set()) == 0
