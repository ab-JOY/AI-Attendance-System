# Audit — "enrolment cannot detect a front-facing face at any distance"

**Date:** 2026-08-21
**Trigger:** UAT round 4. Operator reports the message **"No face detected."**
**Status:** ✅ **Cause found and measured on the reporter's own face and
camera.** It is **not** the detector and **not** the pose thresholds. It is an
uncalibrated geometry threshold sitting on the median of the quantity it
bounds, reported through a message that names a different failure.

⚠️ **This document was rewritten after its first version got the answer
wrong.** §5 records what was wrong and why, because the wrong version was
internally consistent and would have been believed.

---

## 1. The finding

**Two thresholds refuse a correctly presented face. Neither is in the detector.**

### ⭐ 1.1 `eye separation out of range` — a threshold on the median

`vision/validation.py:254`, via `ENROLMENT_PROFILE.eye_distance_of_width`:

```python
minimum_eye_distance = max(profile.min_eye_distance_px, face_width * 0.18)
maximum_eye_distance = face_width * 0.70
```

`eye_distance` is measured between the **outer eye corners** (`LEFT_EYE_SINGLE`
= landmark 33, `RIGHT_EYE_SINGLE` = 263). `face_width` is the FaceMesh bounding
box.

Measured live, 734 frames, reporter's face, camera negotiated 1920x1080:

| face % of frame width | n | eye/width min | med | max | **over the 0.70 ceiling** |
|---:|---:|---:|---:|---:|---:|
| 10.0% | 26 | 0.675 | 0.684 | 0.709 | 4% |
| 12.5% | 222 | 0.571 | 0.683 | 0.719 | 12% |
| 15.0% | 38 | 0.632 | 0.692 | 0.709 | 16% |
| 17.5% | 36 | 0.612 | **0.706** | 0.723 | **67%** |
| 20.0% | 49 | 0.646 | **0.715** | 0.726 | **90%** |
| 22.5% | 71 | 0.623 | **0.722** | 0.747 | **66%** |
| 25.0% | 39 | 0.648 | 0.668 | 0.701 | 3% |
| 27.5% | 253 | 0.644 | 0.660 | 0.696 | 0% |

**The ceiling is 0.70 and the population sits at 0.66–0.75.** A threshold on
the median of its own distribution is not a gate, it is a coin flip: frame-to-
frame landmark jitter decides it, which is why enrolment presented as
intermittent rather than broken.

⚠️ **This is expected geometry, not an unusual face.** The FaceMesh covers the
face oval, not the ears, so outer-corner span is naturally ~0.68 of the box
width. **The number was never derived.** `vision/validation.py`'s own header
says so: *"transcribed unchanged from the originals they replace... Neither set
has ever been calibrated against measured data - they are the 'safe starting
values' MA-6 flagged."* MA-6 has now been collected on.

### ❌ 1.2 `CENTER_TOLERANCE = 180` — **retracted, it is not a defect**

An earlier version of this section called this a confirmed second defect, on
`dy` reaching 192 px at 25% face width and 246 px at 27.5% in a free-form
probe where the operator was deliberately leaning as far in as they could.

**The nine-stage run says otherwise.** Across all 2,709 frames of an operator
holding every enrolment pose normally, `dy` peaked at **165 px** — inside the
180 px allowance, on every stage including CLOSE:

| stage | dy p50 | dy p95 | dy max |
|---|---:|---:|---:|
| STRAIGHT | 78 | 97 | 99 |
| DOWN | 154 | 164 | **165** |
| CLOSE | 107 | 111 | 116 |

180 px admits **100%** of them. `CENTER_TOLERANCE` was measured and left alone.

The lesson is the sampling, not the number: a probe that tells someone to
"lean in and out" measures the extremes of what a person *can* do, not the
distribution of what they *do*. The stage-by-stage run is the honest sample
because it asks for the same thing enrolment asks for.

### ⭐ 1.3 The message names the wrong failure

`vision/enrolment.py:330`:

```python
faces = self._usable_faces(frame, width, height)
if not faces:
    return self._refuse(stage, NO_FACE)      # "No face detected."
```

`_usable_faces()` drops every mesh `ENROLMENT_PROFILE` refuses, so an empty
list means **either** "MediaPipe saw nothing" **or** "the geometry gate refused
every candidate". Only the first is reported, and it is the one that never
happens.

**Nothing logs which.** Three sessions of UAT have now been aimed at the
detector because the only evidence available said "no face".

This is [L19](lessons.md) exactly - *"'it does not agree' has two opposites,
and they are not the same evidence"* - one level up from the branch L19 was
written about.

---

## 2. What was ruled out, by measurement

| Hypothesis | How | Result |
|---|---|---|
| The detector cannot see the face | Live, 2,945 + 2,604 frames | **100% detection at every size**, 10%–35% of frame width. There is no reach problem |
| The pose thresholds still misfire | Same runs; verdict recorded when the gate let a frame through | Reads `STRAIGHT` — 42% of frames at 15% width, 40% at 17.5%. **Phase 6a's finding-G fix works** |
| Aspect ratio band (0.55–1.15) | Live | median 0.81–0.89 throughout. Never fires |
| `MIN_NATIVE_FRAME_WIDTH` / `canonicalise()` | Camera negotiated 1920x1080 | Not reached |

---

## 3. Where the numbers came from

`camera_utils.open_best_camera()` (production), `infra.facemesh.
make_enrolment_detector()` (production), `infra.uploads.canonicalise()`
(production), `vision.validation` and `vision.pose` gates (production), driven
live at 1920x1080 with `{width: {ideal: 1920}}` requested exactly as
`static/js/enrol.js:62` does.

⚠️ **No frame was written to disk at any point.** Only counts and ratios left
the process — `dataset/`/`trainer/` rules, CLAUDE.md.

⚠️ **One subject, mostly frontal.** The LEFT/RIGHT/UP/DOWN stages change the
projected eye separation and are **not** covered by these numbers. Any new
ceiling has to be checked against them before it is trusted — see §6.

---

### ⭐ 1.4 NEW — the CLOSE and MEDIUM distance bands look unreachable

Not what this audit set out to find, and it only became visible once the
operator was walked through all nine stages. Median face-area ratio achieved,
against the band `vision/pose.py` requires:

| stage | band required | ratio achieved (p50) | |
|---|---|---:|---|
| CLOSE | 0.18 – 0.32 | **0.098** | needs ~1.35x closer in linear terms |
| MEDIUM | 0.07 – 0.17 | **0.051** | below the floor |
| FAR | 0.012 – 0.075 | 0.028 | ✅ comfortably inside |

The operator was asked to "MOVE CLOSER" and got to 0.098 against a 0.18 floor.
Both stages would have answered `"Move closer."` indefinitely.

⚠️ **Not yet a confirmed defect.** This was a measurement probe, not a capture:
the screen was not telling them "Move closer.", and a real enrolment might push
them further. But CLOSE is 10 of the 100 images and MEDIUM another 5, so if it
holds, enrolment cannot complete regardless of §1.1. **Confirm this before
anything else** — it is now the most likely next blocker.

### ✅ 1.5 RESOLVED — the LEFT stage read RIGHT because the operator turned right

| stage | pose verdicts seen |
|---|---|
| LEFT | **RIGHT 76%**, STRAIGHT 13%, LEFT 9% |
| RIGHT | RIGHT 95%, STRAIGHT 3%, LEFT 1% |

Both turn stages read RIGHT, which cannot be correct for both. The audit
declined to call it and asked instead. The operator's answer, 2026-08-21:

> *"i got confused with the left and right so the left capture was right half
> the time"*

**There is no sign bug.** `get_head_pose()` reported exactly what was in front
of it. The probe showed an **un-mirrored** preview and said only "TURN
SLIGHTLY LEFT"; the capture page shows a *mirrored* one and says in words that
left means your own left (finding F). The probe reproduced the ambiguity
finding F exists to remove.

⚠️ **This does not disturb §1.1's calibration, and the reason matters.**
Mislabelling *which* turn a frame was does not change the set of frames
measured. The ceiling is a maximum over all 2,709 (0.739, set by DOWN and
CLOSE, both frontal-ish stages nobody was confused about), and the turned
stages only ever probed the *floor* — where the evidence is that a real turn in
**either** direction reaches 0.403 against a 0.18 floor. Both directions are
present in the data; only their labels are unreliable.

The residual is sampling, not correctness: genuine left turns are
under-represented, so the per-stage LEFT row is a weaker estimate than its
n=264 suggests. Recorded in `MEASURED_ENROLMENT_EYE_OF_WIDTH`'s comment.

⭐ **Take this as a usability finding, because it came from a real person.**
An operator who has read this codebase still turned the wrong way when the
preview was not mirrored and the prompt did not say whose left it meant. That
is direct evidence for the open item in `handover-phase-6a.md` §7: **the
liveness prompt still says only "Turn LEFT"**, with no mirror note and no
upper bound, on the recognition overlay. Enrolment was given the fix; the
attendance path was not.

---

## 4. Decisions this needs (user's, not an agent's)

`ENROLMENT_PROFILE` governs which images enter the biometric dataset. Loosening
it changes what the model is trained on, so it is a `todo.md` §7 decision.
Options, with what each costs:

- **A. Re-derive `eye_distance_of_width` from measurement.** Observed range
  0.571–0.747 frontal. A ceiling around **0.85** clears the measured maximum
  with real margin while still refusing a grossly wrong mesh fit. Needs the
  turned stages measured first.
- **B. Raise `CENTER_TOLERANCE`, or make it relative to face size.** 180 px is
  absolute at 1920x1080 and was inherited from a fixed-resolution capture
  window. A fraction of the frame, or of the face box, is the shape the rest of
  `vision/` now uses.
- **C. Split the `NO_FACE` message** into "no face" and "the face was seen but
  refused, because X", and log the refusal. **This one is not a calibration
  decision and is safe to do regardless** — it is a defect on its own terms.

---

## 5. ⚠️ Correction — the first version of this audit was wrong

The first version's headline finding was that MediaPipe cannot see a face
below ~22% of frame width, with a measured table, a mechanism (BlazeFace
short-range), two controls isolating fraction-of-frame from pixel size, and a
remedy validated at 39/39.

**On a real face and a real camera the detection rate was 100% everywhere,
including 12.5% of frame width where the synthetic test scored 0/21.**

The inputs were the mistake: tight 200x200 grayscale aligned crops pasted into
flat grey. That is not a face in a scene — no neck, no shoulders, no hair
beyond the crop, a hard square edge — and the detector needs that context. The
harness was careful, the controls were real, and the stimulus was invented.

**This is [L9](lessons.md) precisely** — *"a benchmark's assumptions are
measurements too"* — and it repeated even though the first version's own §5
listed the synthetic input as its stated limitation. Naming a limitation is not
controlling for it. See [L25](lessons.md).

Practical consequence for whoever reads the old claim: **the centre-crop remedy
solves nothing and should not be built**, and `MIN_FACE_RATIO = 0.012` has no
demonstrated problem.

---

## 6. What was changed

Suite **1,224 passed / 50 skipped** (1,210 before), `ruff` clean. Every new
assertion was mutation-tested.

| | |
|---|---|
| `vision/enrolment.py` | `_usable_faces()` returns `(usable, refusals)`. `offer_frame()` reports NO_FACE **only** when the detector returned nothing; a geometry refusal gets a direction from `REFUSAL_DIRECTIONS` |
| `vision/enrolment.py` | `_note_refusal()` logs the precise reason once per transition, cleared on a good frame — the throttle `describe_confirmation_block()` uses |
| `vision/validation.py` | `ENROLMENT_PROFILE.eye_distance_of_width` **`(0.18, 0.70)` → `(0.18, 0.80)`**, derived from the table in §1.1 and written into the constant's comment |
| `tests/test_vision_enrolment.py` | replaced `test_a_face_that_fails_the_geometry_gate_counts_as_no_face`, which asserted the defect. Five tests now cover the split and the log throttle |
| `tests/test_vision_validation.py` | `MEASURED_ENROLMENT_EYE_OF_WIDTH` + two tests pinning the band against the measured population, parametrised **by stage** |

**Mutation results** — restoring 0.70 fails all nine stage cases and the margin
case; 0.75 (admits every measured frame, 0.011 of headroom) fails the margin
case alone; restoring the NO_FACE conflation fails two tests; removing the log
throttle or its reset each fail exactly one.

⚠️ **A mutation run lied once and it is worth knowing why.** `0.70` and `0.75`
are the same byte length, so `sed -i` left file size unchanged and Python
reused the cached `.pyc`, reporting failures that belonged to the previous
mutation. Run mutations with `-B` and `-p no:cacheprovider`, or change the
byte length. This would have been read as "the test is over-strict".

---

## 7. Handed on

1. **⚠️ Confirm §1.4 first — the CLOSE and MEDIUM bands.** If the operator
   cannot reach a 0.18 face-area ratio at their desk, enrolment still cannot
   finish and §1.1 will not be visible as an improvement. This is the next
   blocker, and it is a `todo.md` §7 decision if it holds, because the distance
   bands decide the dataset's scale diversity.
2. **Re-test §1.5 with an unambiguous instruction.** Do not change a sign on
   the strength of that table.
3. **Run a full 100-image enrolment.** §1.1 is measured but the end-to-end
   capture is still unconfirmed on hardware, which is the state every finding
   in `handover-phase-6a.md` §6 was in.
4. **Re-examine `RECOGNITION_PROFILE` for the same defect.** Its
   `eye_distance_of_width` is `(0.17, 0.68)` against the *four-point* eye
   centres, which are narrower and **not comparable** to the enrolment number —
   but it has the same uncalibrated origin and nobody has measured it. The
   probes in the scratchpad do this in one sitting.
5. **`vision/quality.py` has the same disclaimer and has never been measured
   either** — *"nobody chose to enrol at blur 55 and recognise at blur 50"*.
   Same shape, still open.
