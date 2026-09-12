"""
The enrolment capture protocol, as a state machine that judges one frame.

This is `capture_dataset.py`'s main loop with the camera, the OpenCV window and
the keyboard taken out of it. What is left is the part that actually decides
things: nine stages, one hundred images, and a chain of gates every frame has
to pass before it is saved.

**Why it is shaped like this.** Enrolment is moving into the browser (todo.md
§7 Q2). The browser supplies pixels and a screen; every quality judgement stays
on the server. Reimplementing the gates in JavaScript would recreate MA-4 - the
duplicated-and-diverged face logic Phase 3 existed to delete - so instead the
transport changes and the decisions do not move at all.

**The frame loop became a request.** `capture_dataset.py` held `count`,
`last_capture`, `last_pose` and `pose_frame_count` in module globals and read
them from a `while True:`. An upload endpoint has no loop to hold them in, so
they live here, in an object, behind a lock, and each uploaded frame is one
call to `offer_frame()`.

Like the rest of `vision/`, this owns no hardware. MediaPipe, face alignment
and the filesystem all arrive as `EnrolmentHooks`, so the whole protocol tests
in milliseconds against fakes - which is the same trick `vision/session.py`
plays with `SessionHooks`, and for the same reason.

⚠️ **Frames must arrive at the canonical size.** Every threshold in
`vision/pose.py` was tuned at 1920x1080 and two of them are absolute pixels.
`offer_frame()` refuses anything else rather than silently judging a 640x480
upload against numbers that do not apply to it. Resizing is the caller's job
because it needs OpenCV; enforcing it is this module's job because the
consequence lands here.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from vision import pose as pose_gates
from vision.geometry import box_to_xywh, get_face_box
from vision.quality import ENROLMENT_QUALITY, quality_issue
from vision.validation import ENROLMENT_PROFILE, rejection_reason

logger = logging.getLogger(__name__)

# =====================================================
# THE CAPTURE PLAN
#
# Unchanged from capture_dataset.py: one session, 100 images, nine stages.
# The distribution is deliberate and the recapture in Phase 6 depends on it,
# so this is not a place to simplify - changing the mix changes what the model
# is trained on, which changes what every accuracy figure means.
# =====================================================

POSE = "pose"
EXPRESSION = "expression"
DISTANCE = "distance"


@dataclass(frozen=True)
class Stage:
    """One capture stage: what to ask for, how to judge it, how many images."""

    name: str
    images: int
    instruction: str
    kind: str

    # Frames the subject must hold the stage before an image is taken. The
    # distance stages ask for less because holding a distance is harder than
    # holding a pose, and the subject is further from the screen.
    hold_frames: int


STAGES: tuple[Stage, ...] = (
    Stage("STRAIGHT", 25, "LOOK STRAIGHT", POSE, 6),
    Stage("LEFT", 15, "TURN SLIGHTLY LEFT", POSE, 6),
    Stage("RIGHT", 15, "TURN SLIGHTLY RIGHT", POSE, 6),
    Stage("UP", 10, "LOOK SLIGHTLY UP", POSE, 6),
    Stage("DOWN", 10, "LOOK SLIGHTLY DOWN", POSE, 6),
    Stage("SMILE", 10, "SMILE", EXPRESSION, 6),
    Stage("CLOSE", 5, "MOVE CLOSER", DISTANCE, 3),
    Stage("MEDIUM", 5, "MOVE TO MEDIUM DISTANCE", DISTANCE, 3),
    Stage("FAR", 5, "MOVE FARTHER", DISTANCE, 3),
)

MAX_IMAGES = sum(stage.images for stage in STAGES)

# Seconds between saved images. Without it a held pose fills a whole stage in
# well under a second with near-identical frames, which is a hundred images of
# nothing in particular.
#
# ⚠️ **0.80 -> 0.40 on 2026-08-21, after the first UAT reported the capture as
# too slow.** With the per-image hold removed (see `_save()`) this is now the
# only thing pacing a stage, so it sets the floor directly: 100 images x 0.40 s
# plus nine stage settles is about **46 s**, against the 114 s measured before.
#
# It is still four upload ticks apart at FRAME_INTERVAL_MS = 120, so the
# near-duplicate problem the paragraph above describes is still addressed - the
# subject drifts visibly in 0.4 s. Halving it was chosen over cutting
# MAX_IMAGES precisely because it changes nothing the evaluation quotes: the
# dataset is still 100 images, the model is still ~18.3 MB/student, and the
# LBPH scaling ceiling in todo.md 3a is unmoved (user's decision, 2026-08-21).
CAPTURE_DELAY = 0.40


class EnrolmentPlan:
    """Which stage a given image index belongs to."""

    def __init__(self, stages: tuple[Stage, ...] = STAGES) -> None:
        if not stages:
            raise ValueError("An enrolment plan needs at least one stage")

        self.stages = stages
        self.total = sum(stage.images for stage in stages)

        # Index -> stage, built once. The original recomputed nine cumulative
        # sums per frame to answer this.
        self._by_index: list[Stage] = []
        for stage in stages:
            self._by_index.extend([stage] * stage.images)

    def stage_for(self, captured: int) -> Stage:
        """
        The stage the *next* image belongs to, given how many are done.

        Clamps at the last stage so a caller asking about a finished session
        gets an answer rather than an IndexError.
        """
        if captured >= self.total:
            return self.stages[-1]

        return self._by_index[captured]

    def describe(self) -> list[dict[str, Any]]:
        """The plan as plain data, for the capture page to render."""
        return [
            {"name": s.name, "images": s.images, "instruction": s.instruction}
            for s in self.stages
        ]


DEFAULT_PLAN = EnrolmentPlan()


# =====================================================
# HOOKS AND VERDICT
# =====================================================


@dataclass(frozen=True)
class EnrolmentHooks:
    """
    The hardware, the model and the filesystem, supplied by the caller.

    Same arrangement as `SessionHooks`: keeping these as callables is what lets
    this module hold the protocol without importing MediaPipe, OpenCV's
    alignment code or a path.
    """

    # (frame) -> a list of landmark lists, one per detected face.
    detect: Callable[[Any], list[Any]]

    # (frame, landmarks) -> an aligned 200x200 grayscale crop.
    # Raises ValueError when the face cannot be aligned safely.
    align: Callable[[Any, Any], Any]

    # (index, image) -> None. `index` is 1-based, matching {1..100}.jpg.
    save: Callable[[int, Any], None]

    # Monotonic seconds. A parameter so CAPTURE_DELAY can be tested without
    # sleeping through it.
    now: Callable[[], float] = time.monotonic


@dataclass(frozen=True)
class FrameVerdict:
    """What the server decided about one uploaded frame."""

    captured: int
    total: int
    stage: str
    instruction: str
    message: str
    accepted: bool = False
    done: bool = False
    hold: int = 0
    hold_required: int = 0
    box: tuple[int, int, int, int] | None = None

    def as_dict(self) -> dict[str, Any]:
        """The JSON body the capture page consumes."""
        return {
            "captured": self.captured,
            "total": self.total,
            "stage": self.stage,
            "instruction": self.instruction,
            "message": self.message,
            "accepted": self.accepted,
            "done": self.done,
            "hold": self.hold,
            "hold_required": self.hold_required,
            "box": list(self.box) if self.box else None,
        }


# Operator-facing refusals. Written as things a person can act on, and kept
# here rather than inline so the capture page and the tests refer to one set.
#
# ⚠️ **NO_FACE means the detector saw nothing, and nothing else.** It used to
# also be the answer when MediaPipe had found a face perfectly well and
# `ENROLMENT_PROFILE` refused it - `_usable_faces()` dropped the refused mesh
# and `offer_frame()` reported the empty list as "No face detected."
#
# Two opposite observations, one message, and the message named the one that
# was not happening: measured live on the reporting operator, the detector
# found the face in **100% of 5,549 frames** at every distance while the
# geometry gate refused half of them. Three UAT rounds were spent looking at
# the detector because that was the only evidence anyone had.
#
# This is L19's shape - "'it does not agree' has two opposites, and they are
# not the same evidence" - one level up from the branch L19 was written about.
# See tasks/audit-face-detector.md §1.3.
NO_FACE = "No face detected."
MANY_FACES = "Only one face is allowed in frame."
OFF_CENTRE = "Center your face."
TOO_FAR = "Move farther."
TOO_CLOSE = "Move closer."
ALIGN_FAILED = "Hold still and face the camera."
NOT_STRAIGHT = "Keep your head roughly straight."
FINISHED = "Capture complete."

# What to tell the operator when the detector DID find a face and
# `ENROLMENT_PROFILE` refused it. The keys are `vision.validation`'s own
# reasons; the values are the only part a person sees.
#
# The reason itself is diagnostic rather than operator-facing - "eye separation
# out of range" tells a developer exactly which threshold fired and tells a
# student nothing they can act on - so the precise reason goes to the log and
# a direction goes to the screen. Same split `_refuse()` already makes for
# `align_face`'s five ValueErrors (SE-10).
SQUARE_ON = "Face the camera squarely."

REFUSAL_DIRECTIONS = {
    "face too small": TOO_CLOSE,
    "face area out of range": TOO_CLOSE,
    "head roll too large": "Keep your head upright.",
    "face aspect ratio out of range": SQUARE_ON,
    "eye separation out of range": SQUARE_ON,
    "eye line outside the expected band": SQUARE_ON,
    "mouth width out of proportion": SQUARE_ON,
    "nose outside the horizontal band": SQUARE_ON,
    "nose outside the vertical band": SQUARE_ON,
    "mouth is not below the eyes": SQUARE_ON,
    "nose is not between the eyes and the mouth": SQUARE_ON,
    "face is not frontal enough": SQUARE_ON,
    "mouth is not aligned with the eyes": SQUARE_ON,
}

# Anything `vision.validation` grows later still gets a usable direction rather
# than leaking a new internal string onto the capture page.
REFUSAL_FALLBACK = SQUARE_ON


class EnrolmentComplete(Exception):
    """A frame was offered to a session that already has all its images."""


class EnrolmentSession:
    """
    One student's capture, from zero images to a hundred.

    Not a singleton, and not global: the application holds one at a time and
    refuses a second, the way it does for recognition. Tests build their own.

    ⚠️ **The lock is held across `offer_frame()`, unlike `RecognitionSession`.**
    There the lock cannot be held across a frame because a `/video_feed`
    response lives as long as the operator leaves the page open, so `stop()`
    would never return. Here a frame is one HTTP request that finishes in
    tens of milliseconds, so holding it is both affordable and necessary -
    MediaPipe's FaceMesh is not thread-safe and two uploads can be in flight
    at once.
    """

    def __init__(
        self,
        student_id: str,
        student_name: str,
        hooks: EnrolmentHooks,
        plan: EnrolmentPlan = DEFAULT_PLAN,
        started_by: str | None = None,
        record: dict[str, Any] | None = None,
        password_hash: str | None = None,
    ) -> None:
        self.student_id = student_id
        self.student_name = student_name
        self.started_by = started_by
        self.plan = plan

        # Opaque to this module. The caller puts whatever it will need at the
        # end here - for the application, the student's course and section, so
        # the database row can be written *after* the images are safely in
        # place rather than before the capture starts (FS-9). `vision/` never
        # looks inside it and never learns a column name.
        self.record = record or {}
        self.password_hash = password_hash

        self._hooks = hooks
        self._lock = threading.RLock()

        self._captured = 0
        self._last_capture_at: float | None = None
        self._hold_stage: str | None = None
        self._hold_count = 0
        self._started_at = hooks.now()

        # The geometry refusal currently being reported, so `_note_refusal()`
        # can log on the transition rather than on every frame.
        self._last_refusal: str | None = None

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def captured(self) -> int:
        with self._lock:
            return self._captured

    @property
    def done(self) -> bool:
        with self._lock:
            return self._captured >= self.plan.total

    @property
    def age_seconds(self) -> float:
        return self._hooks.now() - self._started_at

    def progress(self) -> FrameVerdict:
        """The current state with no frame offered, for a page that just loaded."""
        with self._lock:
            stage = self.plan.stage_for(self._captured)

            return FrameVerdict(
                captured=self._captured,
                total=self.plan.total,
                stage=stage.name,
                instruction=stage.instruction,
                message=FINISHED if self.done else stage.instruction,
                done=self.done,
                hold_required=stage.hold_frames,
            )

    # ------------------------------------------------------------------
    # The frame
    # ------------------------------------------------------------------

    def offer_frame(self, frame: Any) -> FrameVerdict:
        """
        Judge one frame, and save it if it passes every gate.

        The gate order is `capture_dataset.py`'s and is load-bearing: cheap
        rejections come first, and alignment - the most expensive step - runs
        only once framing has been agreed. Every failure resets the hold
        counter, so a stage has to be held continuously rather than
        cumulatively.
        """
        self._require_canonical_frame(frame)

        with self._lock:
            if self.done:
                raise EnrolmentComplete(
                    f"{self.student_id} already has all {self.plan.total} images"
                )

            stage = self.plan.stage_for(self._captured)
            height, width = frame.shape[:2]

            faces, refusals = self._usable_faces(frame, width, height)

            if not faces:
                # Which of the two is it? An empty `refusals` means the
                # detector genuinely returned nothing; a populated one means it
                # found a face and the profile refused it, and saying "No face
                # detected." there is what sent three UAT rounds after the
                # detector. See the NO_FACE comment above.
                if not refusals:
                    self._note_refusal(None)
                    return self._refuse(stage, NO_FACE)

                reason = refusals[0]
                self._note_refusal(reason)

                return self._refuse(
                    stage, REFUSAL_DIRECTIONS.get(reason, REFUSAL_FALLBACK)
                )

            self._note_refusal(None)

            if len(faces) > 1:
                return self._refuse(stage, MANY_FACES)

            landmarks, box = faces[0]
            x, y, box_width, box_height = box_to_xywh(box)

            if not pose_gates.face_centered(
                width, height, x, y, box_width, box_height
            ):
                return self._refuse(stage, OFF_CENTRE, box=(x, y, box_width, box_height))

            ratio = pose_gates.calculate_face_ratio(
                width, height, box_width, box_height
            )

            if not pose_gates.good_distance(width, height, box_width, box_height):
                message = TOO_CLOSE if ratio < pose_gates.MIN_FACE_RATIO else TOO_FAR
                return self._refuse(stage, message, box=(x, y, box_width, box_height))

            try:
                aligned = self._hooks.align(frame, landmarks)
            except ValueError:
                # face_preprocessing raises this for five distinct reasons -
                # eyes too close together to align from, an extreme roll, a
                # scale outside the safe range, a face against the frame edge.
                # None of them is worth relaying verbatim: they are all "the
                # face is not presented well enough", and SE-10 says exception
                # text does not go to the client.
                return self._refuse(
                    stage, ALIGN_FAILED, box=(x, y, box_width, box_height)
                )

            issue = quality_issue(aligned, ENROLMENT_QUALITY)

            if issue:
                return self._refuse(stage, issue, box=(x, y, box_width, box_height))

            return self._judge_stage(
                stage,
                landmarks,
                ratio,
                aligned,
                (x, y, box_width, box_height),
                width,
                height,
            )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _require_canonical_frame(frame: Any) -> None:
        expected = (
            pose_gates.CANONICAL_FRAME_HEIGHT,
            pose_gates.CANONICAL_FRAME_WIDTH,
        )

        if frame is None or frame.shape[:2] != expected:
            got = None if frame is None else frame.shape[:2]
            raise ValueError(
                f"Frames must be {expected[1]}x{expected[0]}, got {got}. Every "
                "threshold in vision/pose.py was tuned at that size and two of "
                "them are absolute pixels; resize before offering the frame."
            )

    def _usable_faces(self, frame, width, height):
        """
        `(usable, refusals)` - the faces that passed, and why the rest did not.

        ⚠️ **Returning the refusals is the fix, not a convenience.** This used
        to hand back only the survivors, so a caller could not tell "the
        detector saw nothing" from "the detector saw a face and the profile
        refused it". `offer_frame()` reported both as NO_FACE.
        """
        usable = []
        refusals = []

        for landmarks in self._hooks.detect(frame) or []:
            box = get_face_box(landmarks, width, height)

            reason = rejection_reason(
                landmarks, width, height, box, ENROLMENT_PROFILE
            )

            if reason is None:
                usable.append((landmarks, box))
            else:
                refusals.append(reason)

        return usable, refusals

    def _note_refusal(self, reason: str | None) -> None:
        """
        Log a geometry refusal once per transition, not once per frame.

        At the 120 ms upload tick a per-frame log is eight lines a second of
        the same sentence, which is how a log stops being read. Throttling on
        the transition instead is what `describe_confirmation_block()` does
        with its `distance` branch, and for the same reason.

        `None` clears the state, so a refusal that returns after a good frame
        is reported again rather than swallowed as a duplicate.
        """
        if reason == self._last_refusal:
            return

        self._last_refusal = reason

        if reason is not None:
            logger.warning(
                "Enrolment for %s is refusing frames: the face was detected "
                "and %s (stage %s)",
                self.student_id,
                reason,
                self.plan.stage_for(self._captured).name,
            )

    def _judge_stage(
        self, stage, landmarks, ratio, aligned, box, frame_width, frame_height
    ):
        """The stage-specific gate, then the hold counter, then the save."""
        # ⚠️ `get_head_pose()` needs the face box since 2026-08-21: its
        # thresholds are fractions of face width and height now, not of the
        # frame. While they were frame-relative a subject looking straight
        # ahead was called UP when they stood back and DOWN when they leaned
        # in, so "LOOK STRAIGHT" - the first stage, and the gate every later
        # stage waits behind - was reachable only at one distance.
        _, _, face_width, face_height = box

        if stage.kind == DISTANCE:
            minimum, maximum = pose_gates.distance_band(stage.name)

            if ratio < minimum:
                return self._refuse(stage, TOO_CLOSE, box=box)

            if ratio > maximum:
                return self._refuse(stage, TOO_FAR, box=box)

            _, yaw, pitch = pose_gates.get_head_pose(
                landmarks, frame_width, frame_height, face_width, face_height
            )

            if not pose_gates.head_is_roughly_straight(yaw, pitch):
                return self._refuse(stage, NOT_STRAIGHT, box=box)

        elif stage.kind == EXPRESSION:
            if pose_gates.detect_expression(landmarks) != stage.name:
                return self._refuse(stage, stage.instruction, box=box)

        else:
            detected, _, _ = pose_gates.get_head_pose(
                landmarks, frame_width, frame_height, face_width, face_height
            )

            if detected != stage.name:
                return self._refuse(
                    stage,
                    f"{stage.instruction} (detected {detected})",
                    box=box,
                )

        held = self._note_hold(stage)

        if held < stage.hold_frames:
            return self._verdict(
                stage,
                f"Hold {stage.name.lower()}: {held}/{stage.hold_frames}",
                hold=held,
                box=box,
            )

        if not self._delay_elapsed():
            return self._verdict(
                stage, "Hold still...", hold=held, box=box
            )

        return self._save(stage, aligned, box)

    def _note_hold(self, stage) -> int:
        if self._hold_stage != stage.name:
            self._hold_stage = stage.name
            self._hold_count = 0

        self._hold_count += 1

        return self._hold_count

    def _reset_hold(self) -> None:
        self._hold_stage = None
        self._hold_count = 0

    def _delay_elapsed(self) -> bool:
        if self._last_capture_at is None:
            return True

        return self._hooks.now() - self._last_capture_at >= CAPTURE_DELAY

    def _save(self, stage, aligned, box) -> FrameVerdict:
        index = self._captured + 1

        # The counter advances only after the write returns. A failed write
        # that still incremented would leave a gap in {1..100}.jpg and a
        # session that reports complete with 99 images on disk.
        self._hooks.save(index, aligned)

        self._captured = index
        self._last_capture_at = self._hooks.now()

        # ⚠️ **The hold is NOT reset here, and that is the fix for "capture is
        # too slow".** It used to be, which meant the subject had to re-earn
        # `hold_frames` for *every one of the 15 images in a stage* - while
        # standing perfectly still in a pose they had never left.
        #
        # Measured: 6 frames at the 200 ms upload tick is 1.2 s per image, and
        # 85 of the 100 images are in six-frame stages. That was **102 of the
        # 114 s floor**, on a server that spends 15 ms judging a frame.
        #
        # The hold answers "has this person settled into the pose?", which is a
        # question about entering a stage, not about each image. `_note_hold()`
        # resets on its own when the stage changes, and `_refuse()` still
        # resets it whenever any gate fails - so a pose that breaks must be
        # settled again. What no longer costs anything is holding one.
        #
        # Nothing about what is saved is relaxed: every gate - framing,
        # distance, pose, expression, blur, brightness, contrast - still runs
        # on every frame, and a frame that fails any of them is still refused.
        # `CAPTURE_DELAY` is what keeps the images from being near-duplicates.

        next_stage = self.plan.stage_for(self._captured)

        return FrameVerdict(
            captured=self._captured,
            total=self.plan.total,
            stage=next_stage.name,
            instruction=next_stage.instruction,
            message=(
                FINISHED
                if self.done
                else f"Captured {self._captured}/{self.plan.total}"
            ),
            accepted=True,
            done=self.done,
            hold_required=next_stage.hold_frames,
            box=box,
        )

    def _refuse(self, stage, message, box=None) -> FrameVerdict:
        """Any gate failing resets the hold: a stage must be held unbroken."""
        self._reset_hold()

        return self._verdict(stage, message, box=box)

    def _verdict(self, stage, message, hold=0, box=None) -> FrameVerdict:
        return FrameVerdict(
            captured=self._captured,
            total=self.plan.total,
            stage=stage.name,
            instruction=stage.instruction,
            message=message,
            hold=hold,
            hold_required=stage.hold_frames,
            box=box,
        )


# =====================================================
# ONE AT A TIME
# =====================================================


@dataclass
class EnrolmentRegistry:
    """
    Holds the single in-progress enrolment.

    One at a time, refused rather than queued, for the same reason recognition
    refuses a second viewer: the detector is stateful and not thread-safe, and
    two operators capturing at once would interleave images into two folders
    through one MediaPipe graph.
    """

    _session: EnrolmentSession | None = None
    _lock: threading.RLock = field(default_factory=threading.RLock)

    @property
    def current(self) -> EnrolmentSession | None:
        with self._lock:
            return self._session

    def start(self, session: EnrolmentSession) -> bool:
        """False if one is already running."""
        with self._lock:
            if self._session is not None:
                return False

            self._session = session
            return True

    def finish(self) -> EnrolmentSession | None:
        """Clear the slot and hand back whatever was in it."""
        with self._lock:
            session, self._session = self._session, None
            return session
