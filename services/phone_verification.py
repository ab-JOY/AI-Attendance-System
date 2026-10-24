"""
Attendance from a student's phone, decided by the classroom camera's rules.

The phone used to send **one photo**, judged by a single LBPH predict against a
looser 70.0 distance, with no frontality check and no liveness. One good photo
of a student - or a photo *of* a photo - recorded them. The classroom camera
needs five provisional frames, a 20-frame window at 85% agreement and eight
consecutive, a frontal face, a two-step head-turn challenge and eight more
matching frames after it, all at 58.0.

So a phone now runs **the classroom camera's own `recognise_frame()`**, not a
second implementation of it. What the phone gets of its own is only what one
person verifying in their hand needs:

* **Its own detector and tracker.** The session's FaceMesh carries tracking
  state between frames (static_image_mode=False) and belongs to the classroom
  loop; a request thread borrowing it interleaved two video streams into one
  tracker's idea of where the faces were.
* **A label map holding only the signed-in student's labels.** LBPH still
  compares the face with *every* enrolled student, so a friend's face scores
  nearest to the friend - a label not in this map - and is a weak prediction
  that never confirms. This is what stops Jeff's friend, signed in as Jeff,
  recording Jeff with their own face, and it uses the existing rule
  (`label in label_map`) rather than adding one.
* **Its own steadiness rules** (`phone_track_config()`, PHONE_TRACK_OVERRIDES).
  See there for each one. In short: the classroom's tracker is built for a
  fixed camera at 5-15 fps watching a room of people, and a phone is one
  person, in their hand, at about 1-2 frames a second. The user reported the
  classroom rules on a phone as too slow, and as dropping back to "Unknown"
  mid-count on a moving hand (2026-10-22).

  **What is NOT relaxed - the security core, the classroom's exactly:** the
  58.0 distance on every vote and on the window's average, 85% agreement, the
  frontal-face check, the two-step liveness challenge and its re-check, and the
  label map above.

**Concurrency.** Each phone has a lock, so its detector and tracker see one
frame at a time. The recognizer is shared, as `_PREDICT_POOL` already shares
it across threads; `preprocess_for_lbph()`'s CLAHE is per-thread
(face_preprocessing.py). Phones are capped (MAX_PHONES) because each frame is a
full LBPH predict on the classroom PC.

This module imports no camera, model or MediaPipe code. `web/api.py` passes in
the real `make_detector`, tracker and `recognise_frame`; tests pass fakes.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from vision.session import SessionSnapshot

logger = logging.getLogger(__name__)

# A phone that sends nothing for this long has been put down or closed; its
# detector is released and it starts again from the beginning next time.
IDLE_SECONDS = 30.0

# Phones verifying at once. Each frame costs a full LBPH predict (~170 ms on
# the classroom PC), on top of the classroom camera's own.
MAX_PHONES = 4

# See the module docstring. Long enough for a slow phone on mobile data.
#
# 15 s, was 5 (2026-10-25, user: "stuck on verifying 10/12, then back to 2").
# Measured on the user's phone over 4G: one request every ~5 s (32 frames in
# 9 requests over 44.5 s), so the gap between two batches was 3-4 s and any
# hiccup crossed 5 s - the track expired, the next frame was a new track, and
# the count started again. Expiry protects against one person's votes carrying
# over to another; on a phone only the signed-in student's labels can vote at
# all, and a phone silent for IDLE_SECONDS is dropped outright.
PHONE_TRACK_TIMEOUT_SECONDS = 15.0

# Where a phone's track differs from the classroom's TRACK_CONFIG. Everything
# not named here is the classroom's. Each line is about steadiness, not about
# who the face is:
PHONE_TRACK_OVERRIDES = {
    # A phone sends a picture at a time over the network, not 5-15 fps from a
    # fixed camera; at 1.5 s a track expired between two uploads and restarted.
    # Measured on real faces: frames 2 s apart never confirm at 1.5 s.
    "timeout_seconds": PHONE_TRACK_TIMEOUT_SECONDS,
    # A hand moves the phone, so the face can jump across the square or grow
    # and shrink between two pictures. The classroom treats that as a new
    # person (60% size change, or far with no overlap) and starts the count
    # again from zero. A phone has one person in front of it: any face in the
    # next picture continues the track, unless its size changes more than
    # ~6x. Who it is is still decided by LBPH, vote by vote.
    "min_iou": 0.0,
    "max_distance": 10_000.0,
    "min_size_similarity": 0.15,
    # The classroom wipes the count after 3 weak frames in a row, so a track
    # that wandered onto another face cannot resume stale votes. On a phone
    # the only votes a track can hold are the signed-in student's (the label
    # map), so there is nothing stale to protect against - and a little motion
    # blur was wiping a student's progress. Wiped only after a full window's
    # worth of weak frames.
    "max_weak_frames_before_clear": 12,
    # Fewer frames for a phone: 1 to start, a 12-frame window, 5 in a row.
    # Each vote still has to be under 58.0 and 85% have to agree. The measured
    # basis for 58.0 is per frame (no impostor frame under 62.2,
    # docs/benchmarks.md §8), so a shorter window is a smaller margin against a
    # fluke frame, not a different threshold.
    #
    # 1 to start, was 3 (2026-10-25, user: "looking for the face in the box is
    # too long"). These frames only wait for the shape to stay a valid face, so
    # a poster or a light fitting in a classroom scene is never boxed; they are
    # not an identity check. On a phone the face fills the square, and at the
    # measured ~0.5 frames/s each waited frame cost ~2 s.
    "real_face_confirm_frames": 1,
    "prediction_history_size": 12,
    "min_consecutive_identity_frames": 5,
}


def phone_track_config(base):
    """The classroom's TrackConfig with PHONE_TRACK_OVERRIDES applied."""
    return dataclasses.replace(base, **PHONE_TRACK_OVERRIDES)

LOOKING_FOR_FACE = (
    "Looking for your face - fit it inside the square, not too close."
)

NOT_MATCHED_YET = "Not recognised yet - face the light and hold still."


class PhonesBusy(Exception):
    """MAX_PHONES are already verifying."""


class PhoneResult(NamedTuple):
    """
    What one frame from a phone came to.

    `done` is True once attendance is recorded, or was already. `refused` is a
    permanent answer from the register (not in this class, not enrolled), which
    no further frame can change.

    `stage`, `step` and `progress` say the same as `message`, for the app to
    draw rather than to read: an arrow for the head turn being asked for and
    one bar for the whole check. The text alone made the student read a
    sentence mid-turn, and "Liveness 1/2: Turn LEFT" did not say how far along
    the whole thing was. See `progress_of()`.
    """

    done: bool
    message: str
    refused: bool = False
    stage: str = "finding"
    step: str | None = None
    progress: float = 0.0


# The stages of one phone's verification, in order, and the share of the
# progress bar each one fills. Who-it-is is most of the frames; the turns are
# a few seconds; the re-check is the last stretch.
STAGE_FINDING = "finding"
STAGE_MATCHING = "matching"
STAGE_LIVENESS = "liveness"
STAGE_RECHECK = "recheck"
STAGE_DONE = "done"

_MATCHING_SHARE = 0.55
_LIVENESS_SHARE = 0.30


def progress_of(state) -> tuple[str, str | None, float]:
    """
    `(stage, step, progress)` for one track: where it is, which head turn it
    is being asked for (LEFT/RIGHT/CENTER, or None), and 0..1 overall.

    Read only - it decides nothing. Every number comes from the track's own
    state, the same state the classroom overlay is drawn from.
    """
    if state is None:
        return STAGE_FINDING, None, 0.0

    if state.attendance_saved:
        return STAGE_DONE, None, 1.0

    liveness = state.liveness

    if not state.confirmed or liveness is None:
        window = state.config.prediction_history_size
        votes = len(state.history)
        return STAGE_MATCHING, None, _MATCHING_SHARE * min(votes / window, 1.0)

    if not liveness.passed:
        steps = len(liveness.sequence) or 1
        share = _LIVENESS_SHARE * liveness.index / steps
        return STAGE_LIVENESS, liveness.current_step, _MATCHING_SHARE + share

    needed = liveness.config.post_match_frames or 1
    share = (1.0 - _MATCHING_SHARE - _LIVENESS_SHARE) * min(
        liveness.post_match_count / needed, 1.0
    )
    return STAGE_RECHECK, None, _MATCHING_SHARE + _LIVENESS_SHARE + share


@dataclass
class _Phone:
    key: tuple
    detector: Any
    tracker: Any
    last_seen: float
    lock: threading.Lock = field(default_factory=threading.Lock)
    # For the timing line logged when the phone is done or dropped. Nothing
    # decides on these.
    started: float = 0.0
    frames: int = 0
    requests: int = 0
    judging_ms: list = field(default_factory=list)
    capture_ms: list = field(default_factory=list)


def _session_key(subject) -> tuple:
    """Which attendance session a phone's progress belongs to."""
    return (subject.subject_id, subject.session_id)


class PhoneVerifications:
    """The phones verifying right now, one entry per student."""

    def __init__(
        self,
        *,
        make_detector: Callable[[], Any],
        make_tracker: Callable[[], Any],
        recognise: Callable[[Any, Any], list],
        clock: Callable[[], float] = time.monotonic,
        idle_seconds: float = IDLE_SECONDS,
        max_phones: int = MAX_PHONES,
    ) -> None:
        self._make_detector = make_detector
        self._make_tracker = make_tracker
        self._recognise = recognise
        self._clock = clock
        self._idle_seconds = idle_seconds
        self._max_phones = max_phones
        self._lock = threading.Lock()
        self._phones: dict[str, _Phone] = {}

    def __len__(self) -> int:
        with self._lock:
            return len(self._phones)

    # -- the one public operation -------------------------------------------

    def verify(
        self,
        student_id: str,
        snapshot: SessionSnapshot,
        frame: Any,
        mark_recognized: Callable[[str, str | None], None],
        *,
        first_of_request: bool = True,
        capture_ms: float | None = None,
    ) -> PhoneResult:
        """
        Run one frame from `student_id`'s phone through the classroom rules.

        `snapshot` is the running session's (`session.snapshot()`), and
        `mark_recognized` its `mark_recognized`, called once attendance is
        written so the classroom screen and the live list show the student.

        `first_of_request` and `capture_ms` (the phone's own time per picture)
        are for the timing line only; nothing decides on them.

        Raises PhonesBusy when MAX_PHONES other students are verifying.
        """
        if student_id in snapshot.recognized:
            self.discard(student_id)
            return PhoneResult(
                True,
                "You are already recorded for this session.",
                stage=STAGE_DONE,
                progress=1.0,
            )

        phone = self._phone_for(student_id, _session_key(snapshot.subject))

        with phone.lock:
            phone.last_seen = self._clock()
            phone.frames += 1

            if first_of_request:
                phone.requests += 1

                if capture_ms:
                    phone.capture_ms.append(capture_ms)
            judging_started = time.perf_counter()

            context = SessionSnapshot(
                running=True,
                subject=snapshot.subject,
                reader=None,
                detector=phone.detector,
                recognizer=snapshot.recognizer,
                label_map={
                    label: entry
                    for label, entry in snapshot.label_map.items()
                    if entry.get("student_id") == student_id
                },
                tracker=phone.tracker,
                # A copy: the pipeline writes the student in here when it
                # records them, and the session's own mapping is changed only
                # through mark_recognized(), under its lock.
                recognized=dict(snapshot.recognized),
            )

            overlays = self._recognise(context, frame)
            outcome = self._outcome(student_id, phone.tracker)
            phone.judging_ms.append((time.perf_counter() - judging_started) * 1000)

        if outcome is not None:
            done, status, refused = outcome

            if done:
                mark_recognized(student_id, status)

            self._log_timing(student_id, phone, status)
            self.discard(student_id)

            if refused:
                return PhoneResult(False, status, refused=True)

            return PhoneResult(
                True, f"Verified! Marked as {status}.", stage=STAGE_DONE, progress=1.0
            )

        overlay, state = self._chosen(student_id, phone.tracker, overlays)
        stage, step, progress = progress_of(state)

        return PhoneResult(
            False,
            self._message(overlay, state),
            stage=stage,
            step=step,
            progress=progress,
        )

    # -- housekeeping -------------------------------------------------------

    def discard(self, student_id: str) -> None:
        """Forget one phone and release its detector."""
        with self._lock:
            phone = self._phones.pop(student_id, None)

        if phone is not None:
            self._close(phone)

    def discard_all(self) -> None:
        """Forget every phone - the session has ended."""
        with self._lock:
            phones = list(self._phones.items())
            self._phones.clear()

        for student_id, phone in phones:
            self._log_timing(student_id, phone, "session ended before it finished")
            self._close(phone)

    def sweep(self, current_key: tuple | None) -> None:
        """
        Release phones that went quiet, and every phone from an earlier session.

        A student who verified halfway through one session and comes back in
        the next must not resume a window and a challenge from the last one.
        """
        now = self._clock()

        with self._lock:
            stale = [
                student_id
                for student_id, phone in self._phones.items()
                if phone.key != current_key
                or now - phone.last_seen > self._idle_seconds
            ]
            phones = [(student_id, self._phones.pop(student_id)) for student_id in stale]

        for student_id, phone in phones:
            self._log_timing(student_id, phone, "stopped before it finished")
            self._close(phone)

    # -- internals ----------------------------------------------------------

    def _phone_for(self, student_id: str, key: tuple) -> _Phone:
        self.sweep(key)

        with self._lock:
            phone = self._phones.get(student_id)

            if phone is not None:
                return phone

            if len(self._phones) >= self._max_phones:
                raise PhonesBusy()

            # Built under the lock so two first frames from one student cannot
            # each make a detector. FaceMesh construction is ~0.04 s.
            phone = _Phone(
                key=key,
                detector=self._make_detector(),
                tracker=self._make_tracker(),
                last_seen=self._clock(),
                started=self._clock(),
            )
            self._phones[student_id] = phone

            return phone

    @staticmethod
    def _outcome(student_id, tracker):
        """`(done, status, refused)` once the register has answered, else None."""
        for state in tracker.states.values():
            if state.student_id != student_id:
                continue

            if state.attendance_saved:
                return True, state.recorded_status, False

            if state.attendance_refused is not None:
                return False, state.attendance_refused, True

        return None

    @staticmethod
    def _chosen(student_id, tracker, overlays):
        """
        The face this phone is about, and its track state (either may be None).

        Their own track when there is one; otherwise the largest face's, since
        the person holding the phone is the one nearest it.
        """
        if not overlays:
            return None, None

        def theirs(overlay):
            state = tracker.states.get(overlay.track_id)
            return state is not None and state.student_id == student_id

        own = [overlay for overlay in overlays if theirs(overlay)]
        chosen = own[0] if own else max(
            overlays, key=lambda overlay: overlay.box.width * overlay.box.height
        )

        return chosen, tracker.states.get(chosen.track_id)

    @staticmethod
    def _message(chosen, state) -> str:
        """
        What to tell the student: the status the classroom screen would draw
        under their box - "Verifying 12/20", "Turn LEFT", "Move closer".
        """
        if chosen is None:
            return LOOKING_FOR_FACE

        # "Unknown" is the classroom's word for a frame that did not match -
        # right over a stranger's box on a projector, and alarming on your own
        # phone in the middle of a count. Nothing is lost on such a frame any
        # more (max_weak_frames_before_clear), so say where the count stands.
        if chosen.status == "Unknown":
            votes = len(getattr(state, "history", ()) or ())

            if votes:
                window = state.config.prediction_history_size
                return f"Hold still - verifying {votes}/{window}"

            return NOT_MATCHED_YET

        return chosen.status

    @staticmethod
    def _log_timing(student_id, phone: _Phone, outcome) -> None:
        """
        One line per phone verification: how long it took, how many frames
        reached the server, and how long the server spent on them. Frames per
        second *received* is the number that says whether a slow verification
        is the network or the phone (lessons.md L39).
        """
        def median(values):
            values = sorted(values)
            return values[len(values) // 2] if values else 0.0

        seconds = max(phone.last_seen - phone.started, 0.0)

        logger.info(
            "Phone verification for %s: %s - %d frames in %d requests over "
            "%.1f s (%.1f frames/s received); server %.0f ms per frame, phone "
            "camera %.0f ms per picture (medians)",
            student_id,
            outcome,
            phone.frames,
            phone.requests,
            seconds,
            phone.frames / seconds if seconds else 0.0,
            median(phone.judging_ms),
            median(phone.capture_ms),
        )

    @staticmethod
    def _close(phone: _Phone) -> None:
        """
        Release the detector - after any frame still inside it.

        ⚠️ Under the phone's lock: a sweep from another request can reach a
        phone whose session just ended while its own last frame is mid-process,
        and closing a FaceMesh under a running `process()` is the crash
        services/enrolment.py's `_GuardedDetector` exists to prevent.
        """
        close = getattr(phone.detector, "close", None)

        if close is None:
            return

        with phone.lock:
            try:
                close()
            except Exception:
                logger.debug("Closing a phone's detector failed", exc_info=True)
