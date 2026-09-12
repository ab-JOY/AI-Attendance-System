"""
Face tracking and the per-track verification state machine (MA-12, RE-2).

MA-12 called `generate_frames()` "the single hardest block in the codebase to
reason about or modify" - ~300 lines at seven levels of nesting, driving a
state machine held in plain dicts and mutated by five free functions, with the
track table itself in module-level globals (RE-2).

This module holds the part of that with no camera, no MediaPipe, no database
and no OpenCV in it, so the decision logic can be tested directly instead of
inferred from a video stream.

Three pieces:

- `TrackConfig` - the thresholds, previously loose module constants.
- `TrackState` - one tracked face's verification progress. The dict and the
  five functions that mutated it become one object with named transitions.
- `FaceTracker` - the track table. `tracks`, `track_verification` and
  `next_track_id` were three module globals mutated from two threads with no
  lock; they are now instance state, and `RecognitionSession` owns the one
  instance that exists and the lock around it.

The state machine is deliberately conservative, and the comments that
explain *why* are carried over verbatim from the original where they still
apply - most of them describe attacks and failure modes that were found by
watching the thing run, and they cost more to rediscover than to keep.
"""

from __future__ import annotations

import time
from collections import Counter, deque
from dataclasses import dataclass
from typing import Any

from vision.geometry import FaceBox, box_area, box_center, box_iou


@dataclass(frozen=True)
class TrackConfig:
    """Tracking and verification thresholds."""

    # --- matching a detection to an existing track ---
    timeout_seconds: float = 1.5
    max_distance: float = 180.0
    min_iou: float = 0.08
    min_size_similarity: float = 0.40

    # --- how long an object must look like a face before it is drawn ---
    real_face_confirm_frames: int = 5

    # --- confirming an identity ---
    prediction_history_size: int = 20
    min_label_agreement: float = 0.85
    min_consecutive_identity_frames: int = 8
    confirmation_confidence: float = 52.0

    # --- losing one ---
    max_weak_frames_before_clear: int = 3

    # ⚠️ These two count **contradictions only** - frames where LBPH read a
    # *different* enrolled student on a track whose identity is locked. That
    # is evidence of a track switch, which is what they exist to stop, and
    # five frames of it is rightly fatal.
    #
    # They used to count "no usable prediction" as well, and that is what made
    # the liveness challenge unpassable: motion blur across a requested head
    # turn, or a crop the quality gate refused, is not evidence about identity
    # at all. At 7-15 fps two frames is 0.13 s and five is 0.33-0.7 s, so a
    # student doing exactly what the screen asked lost the sequence and then
    # the identity. See process_confirmed_track().
    max_confirmed_mismatch_frames: int = 5
    liveness_reset_mismatch_frames: int = 2

    # How many consecutive frames a confirmed track may go **unread** - no
    # usable prediction either way - before the identity is dropped. Generous
    # because absence of evidence is not evidence: at 7-15 fps this is roughly
    # 1.7-3.5 s, long enough to cover a turn, a blink of bad exposure or
    # someone walking past, and short enough that a track which has genuinely
    # lost its face does not sit locked forever.
    #
    # It does **not** restart the challenge. Reshuffling on frames the
    # challenge itself caused is what put a fresh random sequence on screen
    # while the student was still performing the previous one.
    max_unreadable_frames_before_clear: int = 25

    # --- retrying an attendance write that failed (FS-17) ---
    #
    # How long to wait before asking the database again after it was
    # *unreachable*. A permanent refusal - not in this class, not a student -
    # is latched instead and never retried, because nothing the camera can see
    # will change the answer; only these transient failures come back here.
    #
    # 25 frames is ~5 s at the 4.8 fps measured in logs/app.log. Without it the
    # write was attempted on every frame of every stuck track: 193 attempts in
    # 200 frames, two round trips each, against a database that had just failed
    # to answer one.
    attendance_retry_frames: int = 25


DEFAULT_TRACK_CONFIG = TrackConfig()


@dataclass
class HistoryVerdict:
    """What the recent prediction history says about a track's identity."""

    dominant_id: str
    dominant_name: str
    agreement_ratio: float
    average_confidence: float
    history_count: int
    dominant_count: int


class TrackState:
    """
    One tracked face's progress from "an object" to "a marked attendance".

    The lifecycle, and every guard on it, is the security property of this
    system: a track must look like a real face for several frames, then agree
    with itself across a window of predictions, then pass liveness while
    *still* matching the same identity, before anything is written.
    """

    __slots__ = (
        "attendance_refused",
        "attendance_retry_in",
        "attendance_saved",
        "attendance_status",
        "confirmed",
        "consecutive_count",
        "consecutive_id",
        "config",
        "history",
        "last_blocker",
        "liveness",
        "mismatch_frames",
        "student_id",
        "student_name",
        "unreadable_frames",
        "valid_face_frames",
        "weak_frames",
    )

    def __init__(
        self,
        config: TrackConfig = DEFAULT_TRACK_CONFIG,
        valid_face_frames: int = 0,
    ) -> None:
        self.config = config
        self.valid_face_frames = valid_face_frames
        self.history: deque[dict[str, Any]] = deque(
            maxlen=config.prediction_history_size
        )
        self.consecutive_id: str | None = None
        self.consecutive_count = 0
        self.weak_frames = 0
        # Frames on a confirmed track that read a *different* student.
        self.mismatch_frames = 0
        # Frames on a confirmed track that read nothing usable at all. Counted
        # apart from mismatch_frames because the two are different evidence -
        # see TrackConfig above.
        self.unreadable_frames = 0
        # The blocker reported last frame, so a caller can log a stuck track
        # once when it becomes stuck rather than at the frame rate.
        self.last_blocker: str | None = None
        self.student_id: str | None = None
        self.student_name: str | None = None
        self.confirmed = False
        self.attendance_saved = False
        # What the register actually recorded for this track: "Present",
        # "Late", or None when nothing has been written yet or the status is
        # not known to this process. See `recorded_status` below.
        self.attendance_status: str | None = None
        # Why the register permanently refused this track, or None (FS-17).
        # Set only for a refusal no retry can change - the student is not in
        # this class, or the model knows a face the database does not. It is
        # the message to draw, held opaquely: which refusals are permanent is
        # decided in recognize_face.py, where NotRecorded lives.
        self.attendance_refused: str | None = None
        # Frames still to wait before retrying a write the database failed to
        # answer. Counts down; 0 means "ask now".
        self.attendance_retry_in = 0
        self.liveness: Any = None

    # -- lifecycle ------------------------------------------------------

    def note_valid_frame(self) -> None:
        self.valid_face_frames += 1

    @property
    def recorded_status(self) -> str:
        """
        What to put on the overlay for a track that has been recorded.

        ⚠️ **The overlay used to say "Present" unconditionally**, which was
        true only because `save_attendance()` was called with a hardcoded
        "Present" and could never write anything else (FS-8, reopened). Now
        that the schedule decides, a Late student must read Late on the screen
        as well as in the register - two screens disagreeing about one session
        is FS-7, and it is what /reports and the export would be checked
        against.

        Falls back to "Present" when the status is unknown, which is exactly
        the `adopt_completed_identity()` case: a student who stepped out of
        frame and back in is inheriting a mark this track did not make and
        cannot see. That is the pre-existing behaviour rather than a new
        claim - the register, not the overlay, is the record.
        """
        return self.attendance_status or "Present"

    @property
    def attendance_settled(self) -> bool:
        """
        True once this track will make no further attendance write (FS-17).

        Either the row is written or the register has permanently refused it.
        The two are opposite outcomes and identical in the only respect the
        frame loop cares about: there is nothing left to ask, so the track
        needs neither an LBPH predict nor a database round trip on any
        subsequent frame.

        ⚠️ A *transient* failure is deliberately not settled. The database
        being unreachable is not an answer, and `attendance_retry_in` paces
        the next attempt rather than abandoning it.
        """
        return self.attendance_saved or self.attendance_refused is not None

    def refuse_attendance(self, reason: str) -> None:
        """Latch a permanent refusal. `reason` is what the overlay shows."""
        self.attendance_refused = reason
        self.attendance_retry_in = 0

    def defer_attendance_retry(self, frames: int) -> None:
        """Wait `frames` frames before asking the database again."""
        self.attendance_retry_in = frames

    def may_attempt_attendance(self) -> bool:
        """
        Whether to attempt the write on this frame, counting down the backoff.

        ⚠️ Mutates. It is called once per frame from the one place that writes,
        and the countdown has to advance on the frames it refuses, or the
        backoff never expires.
        """
        if self.attendance_retry_in > 0:
            self.attendance_retry_in -= 1
            return False

        return True

    @property
    def is_provisional(self) -> bool:
        """
        True until the object has been a geometrically valid face for long
        enough to be drawn or recognised at all.

        This is what stops a poster, a phone case or a mesh fit on a
        light fitting from being boxed on screen.
        """
        return self.valid_face_frames < self.config.real_face_confirm_frames

    def reset_identity(self) -> TrackState:
        """
        Forget the identity but keep the "this is a real face" credit.

        Returns a fresh state rather than clearing in place, exactly as the
        original `reset_identity_state()` did - the caller rebinds it into
        the track table.
        """
        return TrackState(
            config=self.config,
            valid_face_frames=self.valid_face_frames,
        )

    # -- prediction history --------------------------------------------

    def note_weak_prediction(self) -> None:
        """
        A frame that produced no usable match.

        A run of these clears the history, so a track that wandered off a
        face cannot resume an old identity from stale votes.
        """
        self.weak_frames += 1
        self.consecutive_id = None
        self.consecutive_count = 0

        if self.weak_frames >= self.config.max_weak_frames_before_clear:
            self.history.clear()
            self.weak_frames = 0

    def add_prediction(
        self,
        student_id: str,
        student_name: str,
        confidence: float,
    ) -> None:
        self.weak_frames = 0

        if self.consecutive_id == student_id:
            self.consecutive_count += 1
        else:
            self.consecutive_id = student_id
            self.consecutive_count = 1

        self.history.append(
            {
                "student_id": student_id,
                "student_name": student_name,
                "confidence": float(confidence),
            }
        )

    def evaluate_history(self) -> HistoryVerdict | None:
        items = list(self.history)

        if not items:
            return None

        counts = Counter(item["student_id"] for item in items)
        dominant_id, dominant_count = counts.most_common(1)[0]

        dominant_items = [
            item for item in items if item["student_id"] == dominant_id
        ]

        return HistoryVerdict(
            dominant_id=dominant_id,
            dominant_name=dominant_items[-1]["student_name"],
            agreement_ratio=dominant_count / len(items),
            average_confidence=(
                sum(item["confidence"] for item in dominant_items)
                / len(dominant_items)
            ),
            history_count=len(items),
            dominant_count=dominant_count,
        )

    def confirmation_blocker(
        self,
        verdict: HistoryVerdict,
        claimed_student_ids: set[str],
    ) -> str | None:
        """
        Which condition is stopping this track confirming, or None if none is.

        `can_confirm()` is this function's boolean face, the same way
        `is_valid_face_candidate()` is `rejection_reason()`'s in
        vision/validation.py. Naming the blocker rather than returning a bare
        False is what turns "the camera will not pick me up" into something
        diagnosable - and this system has now produced that exact complaint
        twice for two different reasons.

        The one that motivated it: with the confirmation bar set below the
        recognition threshold, a genuine unanimous window sat at
        "Verifying 20/20 100%" indefinitely with nothing written to any log.
        The band is closed in recognize_face.py, but a future calibration may
        legitimately reopen a gap, and when it does the operator and the log
        must both be told rather than left watching a yellow box.
        """
        if verdict.history_count < self.config.prediction_history_size:
            return "window"

        if verdict.agreement_ratio < self.config.min_label_agreement:
            return "agreement"

        if verdict.average_confidence > self.config.confirmation_confidence:
            return "distance"

        if (
            self.consecutive_id != verdict.dominant_id
            or self.consecutive_count
            < self.config.min_consecutive_identity_frames
        ):
            return "consecutive"

        if verdict.dominant_id in claimed_student_ids:
            return "claimed"

        return None

    def can_confirm(
        self,
        verdict: HistoryVerdict,
        claimed_student_ids: set[str],
    ) -> bool:
        """
        Five independent conditions, all required.

        A full window of votes, overwhelming agreement within it, a good
        average distance, and a run of consecutive frames on the same
        identity - plus nobody else in this frame having already claimed it.

        Expressed through `confirmation_blocker()` so the boolean and the
        explanation cannot disagree about what the rules are.
        """
        return self.confirmation_blocker(verdict, claimed_student_ids) is None

    def confirm(self, student_id: str, student_name: str, liveness: Any) -> None:
        """Lock an identity to this track and start the liveness challenge."""
        self.student_id = student_id
        self.student_name = student_name
        self.confirmed = True
        self.attendance_saved = False
        self.attendance_status = None
        self.attendance_refused = None
        self.attendance_retry_in = 0
        self.mismatch_frames = 0
        self.unreadable_frames = 0
        self.liveness = liveness

    def adopt_completed_identity(
        self,
        student_id: str,
        student_name: str,
        status: str | None = None,
    ) -> None:
        """
        Inherit an identity that already finished attendance this session.

        Only an unconfirmed track may do this. It is what stops a student who
        stepped out of frame and back in from being asked to pass liveness
        again for a mark they already have.

        `status` is what the register actually recorded, when the caller knows
        it. It used to be unknowable here - the mark was made by a track this
        one never saw - so `recorded_status` fell back to "Present". Since
        FS-18 the session is seeded from the register at start, which carries
        the real status with it, and a **Late** student re-appearing must not
        read "Present" on the overlay: two screens disagreeing about one
        session is FS-7. The fallback stays for the within-session case, where
        it remains the pre-existing behaviour rather than a new claim.
        """
        self.student_id = student_id
        self.student_name = student_name
        self.confirmed = True
        self.attendance_saved = True
        self.attendance_status = status or self.attendance_status
        self.attendance_refused = None
        self.attendance_retry_in = 0
        self.mismatch_frames = 0
        self.unreadable_frames = 0
        self.history.clear()


class FaceTracker:
    """
    The track table: which detection this frame is which face from last frame.

    Was three module globals (`tracks`, `track_verification`, `next_track_id`)
    mutated from both the Flask worker thread and the camera thread with no
    lock (RE-2). They are instance state now; `RecognitionSession` owns the
    single instance and the lock.
    """

    def __init__(
        self,
        config: TrackConfig = DEFAULT_TRACK_CONFIG,
        clock=time.time,
    ) -> None:
        self.config = config
        self._clock = clock
        self._boxes: dict[int, dict[str, Any]] = {}
        self.states: dict[int, TrackState] = {}
        self._next_track_id = 0

    def __len__(self) -> int:
        return len(self._boxes)

    def reset(self) -> None:
        self._boxes.clear()
        self.states.clear()
        self._next_track_id = 0

    def expire_old_tracks(self) -> None:
        now = self._clock()

        expired = [
            track_id
            for track_id, data in self._boxes.items()
            if now - data["last_seen"] > self.config.timeout_seconds
        ]

        for track_id in expired:
            self._boxes.pop(track_id, None)
            self.states.pop(track_id, None)

    def state_for(self, track_id: int) -> TrackState:
        state = self.states.get(track_id)

        if state is None:
            state = TrackState(config=self.config)
            self.states[track_id] = state

        return state

    def replace_state(self, track_id: int, state: TrackState) -> None:
        self.states[track_id] = state

    def assign(self, box: FaceBox, used_track_ids: set[int]) -> int:
        """
        The track this box continues, or a new id.

        Scores overlap, centre distance and size similarity together rather
        than nearest-centre alone, which is what keeps two faces crossing in
        frame from swapping identities.
        """
        self.expire_old_tracks()

        center_x, center_y = box_center(box)
        new_area = max(box_area(box), 1)

        best_track_id = None
        best_score = float("-inf")

        for track_id, data in self._boxes.items():
            if track_id in used_track_ids:
                continue

            old_box = data["box"]
            old_center_x, old_center_y = box_center(old_box)
            old_area = max(box_area(old_box), 1)

            center_distance = (
                (center_x - old_center_x) ** 2 + (center_y - old_center_y) ** 2
            ) ** 0.5

            overlap = box_iou(box, old_box)
            size_similarity = min(new_area, old_area) / max(new_area, old_area)

            distance_limit = max(
                self.config.max_distance,
                max(box[2] - box[0], box[3] - box[1]) * 0.85,
            )

            if size_similarity < self.config.min_size_similarity:
                continue

            if overlap < self.config.min_iou and center_distance > distance_limit:
                continue

            score = (
                overlap * 2.0
                + size_similarity
                - center_distance / max(distance_limit, 1.0)
            )

            if score > best_score:
                best_score = score
                best_track_id = track_id

        if best_track_id is None:
            best_track_id = self._next_track_id
            self._next_track_id += 1

        self._boxes[best_track_id] = {
            "box": box,
            "center": (center_x, center_y),
            "last_seen": self._clock(),
        }

        return best_track_id
