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
    max_confirmed_mismatch_frames: int = 5
    liveness_reset_mismatch_frames: int = 2


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
        "attendance_saved",
        "confirmed",
        "consecutive_count",
        "consecutive_id",
        "config",
        "history",
        "liveness",
        "mismatch_frames",
        "student_id",
        "student_name",
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
        self.mismatch_frames = 0
        self.student_id: str | None = None
        self.student_name: str | None = None
        self.confirmed = False
        self.attendance_saved = False
        self.liveness: Any = None

    # -- lifecycle ------------------------------------------------------

    def note_valid_frame(self) -> None:
        self.valid_face_frames += 1

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

    def can_confirm(
        self,
        verdict: HistoryVerdict,
        claimed_student_ids: set[str],
    ) -> bool:
        """
        Four independent conditions, all required.

        A full window of votes, overwhelming agreement within it, a good
        average distance, and a run of consecutive frames on the same
        identity - plus nobody else in this frame having already claimed it.
        """
        return (
            verdict.history_count >= self.config.prediction_history_size
            and verdict.agreement_ratio >= self.config.min_label_agreement
            and verdict.average_confidence <= self.config.confirmation_confidence
            and self.consecutive_id == verdict.dominant_id
            and self.consecutive_count
            >= self.config.min_consecutive_identity_frames
            and verdict.dominant_id not in claimed_student_ids
        )

    def confirm(self, student_id: str, student_name: str, liveness: Any) -> None:
        """Lock an identity to this track and start the liveness challenge."""
        self.student_id = student_id
        self.student_name = student_name
        self.confirmed = True
        self.attendance_saved = False
        self.mismatch_frames = 0
        self.liveness = liveness

    def adopt_completed_identity(self, student_id: str, student_name: str) -> None:
        """
        Inherit an identity that already finished attendance this session.

        Only an unconfirmed track may do this. It is what stops a student who
        stepped out of frame and back in from being asked to pass liveness
        again for a mark they already have.
        """
        self.student_id = student_id
        self.student_name = student_name
        self.confirmed = True
        self.attendance_saved = True
        self.mismatch_frames = 0
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
