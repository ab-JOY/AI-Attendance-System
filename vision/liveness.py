"""
The active liveness challenge (SE-12).

SE-12: "Liveness is defeatable by a video replay - the challenge is one of two
fixed poses (TURN_LEFT/TURN_RIGHT) with no depth, texture, or blink component.
Presenting a phone playing a recording passes."

**Scope was decided by the user on 2026-08-08: a randomised multi-step
challenge, and no blink detection.** Blink/EAR was explicitly declined, and the
reason is worth keeping next to the code: the thresholds are uncalibrated for
this camera and this lighting, glasses degrade them, and a threshold slightly
wrong means a student who cannot mark attendance at all - days before a
prefinal defense. A false rejection here is not an inconvenience, it is a
student marked absent for a class they attended.

Which makes the *other* finding in this module the important one.

---

## The distance bug this fixes, measured

The previous thresholds compared yaw in **frame-normalised** units
(`nose.x - eye_centre.x`, both in 0..1 across the frame) against a constant
0.030. But the geometry gate that decides whether a face is acceptable at all
works in **face-width** units, refusing a nose more than 0.28 face-widths off
centre. Those two do not scale together: the same head turn produces a smaller
normalised yaw the further away the person stands.

Measured, driving the real MediaPipe mesh over the enrolment set's deliberate
left-turn images composited at a range of sizes:

| face box | turn actually made | old threshold needed | reachable? |
|---:|---:|---:|---|
| 208 px | 0.136w | **0.185w** | **no** |
| 260 px | 0.148w | 0.148w | borderline |
| 342 px | 0.144w | 0.112w | yes |
| 422 px | 0.144w | 0.091w | yes |
| 508 px | 0.154w | 0.076w | yes |

**The turn a person actually makes is a constant ~0.14 of their face width at
every distance** - as it must be, being a fact about heads rather than about
cameras. The old threshold demanded a different fraction at every distance,
and past roughly a 137 px face box it demanded more turn than the geometry
gate would accept: the student would be rejected for turning too far before
they could ever be credited with turning far enough.

So a student standing a normal distance back could turn their head all day and
never pass, with the box still drawn green and nothing logged. That is the
exact failure mode the blink decision was made to avoid, already present in the
mechanic that was kept.

Thresholds here are therefore **fractions of face width**, and the caller is
required to supply yaw in those units.

---

## What the randomised sequence is worth, honestly

The challenge is now an ordered sequence of two *distinct* steps drawn from
LEFT/RIGHT/CENTER - six possible sequences - each with its own timeout, issued
only once an identity has been confirmed.

This **raises** the replay bar. It does not close it. A recording that cycles
left, centre, right, centre satisfies every one of the six sequences given
enough time; what stops it is the per-step timeout, so an attacker holding up a
looping video has to be at the right point in the loop when the challenge is
issued rather than being certain to pass. That is a meaningful change from the
old one-of-two fixed challenge, which any recording containing a single head
turn defeated outright, and it is nothing like proof of presence.

`docs/data_privacy.md` §7.6 states this in the same terms. Closing it properly
needs depth or texture analysis, which is out of scope for this thesis.

---

Nothing here imports a camera, MediaPipe or OpenCV, so the sequence logic is
tested directly with synthetic yaw traces. `clock` and `rng` are injected for
the same reason.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

LEFT = "LEFT"
RIGHT = "RIGHT"
CENTER = "CENTER"

# Positive yaw is a turn to the subject's left as the camera sees it - the
# sign convention comes from `nose.x - eye_centre.x` and is unchanged.
ALL_STEPS = (LEFT, RIGHT, CENTER)

PROMPTS = {
    LEFT: "Turn LEFT",
    RIGHT: "Turn RIGHT",
    CENTER: "Look at the camera",
}


@dataclass(frozen=True)
class LivenessConfig:
    """
    Thresholds and timings, all in face-width units or seconds.

    `turn_of_face_width` sits well below the ~0.14w a person actually turns
    (see the table above) and well below the geometry gate's 0.28w ceiling, so
    there is a usable window at every distance the gate accepts. That window
    not existing at some distances was the bug.
    """

    turn_of_face_width: float = 0.10
    center_of_face_width: float = 0.06

    steps: int = 2
    step_timeout_seconds: float = 8.0

    # Frames on which the locked identity must still match *after* the
    # sequence completes, before attendance is written. Unchanged behaviour,
    # moved here from a loose module constant.
    post_match_frames: int = 8

    def __post_init__(self) -> None:
        if not 0 < self.center_of_face_width < self.turn_of_face_width:
            raise ValueError(
                "center_of_face_width must be positive and smaller than "
                "turn_of_face_width, or the two states overlap and a single "
                "pose satisfies both"
            )

        if not 1 <= self.steps <= len(ALL_STEPS):
            raise ValueError(
                f"steps must be between 1 and {len(ALL_STEPS)} - the sequence "
                f"is drawn without replacement"
            )


DEFAULT_LIVENESS_CONFIG = LivenessConfig()


def draw_sequence(config=DEFAULT_LIVENESS_CONFIG, rng=None):
    """
    An ordered sequence of distinct steps.

    Distinct because "LEFT then LEFT" asks for no second movement at all - the
    subject is already there - so it would be a one-step challenge wearing a
    two-step label.
    """
    chooser = rng if rng is not None else random

    return tuple(chooser.sample(list(ALL_STEPS), config.steps))


@dataclass
class LivenessChallenge:
    """
    One track's progress through its sequence.

    Replaces the plain dict the old code passed around
    (`{"challenge", "started_at", "movement_seen", "passed",
    "post_match_count"}`) mutated by three free functions.
    """

    config: LivenessConfig = DEFAULT_LIVENESS_CONFIG
    sequence: tuple[str, ...] = ()
    index: int = 0
    passed: bool = False
    post_match_count: int = 0
    step_started_at: float = 0.0
    restarts: int = 0
    clock: Any = None
    rng: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.clock is None:
            import time

            self.clock = time.monotonic

        if not self.sequence:
            self.sequence = draw_sequence(self.config, self.rng)

        if not self.step_started_at:
            self.step_started_at = self.clock()

    # -- state -------------------------------------------------------------

    @property
    def current_step(self) -> str | None:
        if self.passed or self.index >= len(self.sequence):
            return None

        return self.sequence[self.index]

    @property
    def identity_reconfirmed(self) -> bool:
        """Passed the sequence *and* still matched for the required frames."""
        return self.passed and self.post_match_count >= self.config.post_match_frames

    def _satisfied_by(self, step: str, yaw_of_face_width: float) -> bool:
        if step == LEFT:
            return yaw_of_face_width >= self.config.turn_of_face_width

        if step == RIGHT:
            return yaw_of_face_width <= -self.config.turn_of_face_width

        return abs(yaw_of_face_width) <= self.config.center_of_face_width

    # -- advancing ---------------------------------------------------------

    def update(self, yaw_of_face_width: float) -> None:
        """
        Advance on one frame. `yaw_of_face_width` is yaw ÷ face-box width.

        Intermediate poses are ignored rather than treated as failures. Moving
        from LEFT to RIGHT necessarily passes through centre, and failing a
        student for the way their head travelled between two requested
        positions would be a false rejection with no security value.
        """
        if self.passed:
            return

        step = self.current_step

        if step is None:
            return

        if self.clock() - self.step_started_at > self.config.step_timeout_seconds:
            self.restart()
            return

        if not self._satisfied_by(step, yaw_of_face_width):
            return

        self.index += 1
        self.step_started_at = self.clock()

        if self.index >= len(self.sequence):
            self.passed = True

    def restart(self) -> None:
        """
        Abandon this attempt and draw a fresh sequence.

        A new draw rather than a retry of the same one: a sequence that has
        been on screen long enough to time out has also been on screen long
        enough to have been read by whoever is holding up a recording.
        """
        self.sequence = draw_sequence(self.config, self.rng)
        self.index = 0
        self.passed = False
        self.post_match_count = 0
        self.step_started_at = self.clock()
        self.restarts += 1

    # -- post-liveness identity re-check ------------------------------------

    def note_identity_match(self) -> None:
        if self.passed:
            self.post_match_count += 1

    def note_identity_mismatch(self) -> None:
        self.post_match_count = 0

    # -- what the operator sees ---------------------------------------------

    def message(self) -> str:
        if self.passed:
            if self.post_match_count < self.config.post_match_frames:
                return (
                    "Liveness passed - Rechecking "
                    f"{self.post_match_count}/{self.config.post_match_frames}"
                )

            return "Liveness Passed"

        step = self.current_step

        if step is None:
            return "Preparing liveness..."

        return (
            f"Liveness {self.index + 1}/{len(self.sequence)}: {PROMPTS[step]}"
        )
