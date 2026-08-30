# Assessment — Phase 6c: liveness latency, pipeline cost, camera lifecycle

**Date:** 2026-08-21
**Status:** Assessment only. **Nothing in this document has been implemented.**
**Trigger:** four UAT findings, plus a request to assess two proposed
approaches (MiniFASNet for liveness; optimising the recognition pipeline).
**Read with:** [`handover-phase-6b.md`](handover-phase-6b.md),
[`todo.md`](todo.md) §7 Q1 — **which this assessment reopens with new
evidence**.

Every number below was measured on this machine today. Where a figure is an
extrapolation it says so.

---

## 0. Immediate, before anything else

**Two Python processes started 15:39 are still running and still holding the
camera.** That is why the Windows Camera app answers
`0xA00F429F<WindowShowFailed> (0x8007001F)` — "a device attached to the system
is not functioning" is what Windows says when the device is held by a process
that will not let go.

```powershell
Get-Process python | Stop-Process
```

This is not a Windows fault and reinstalling a driver will not help. §3 is why
it happens.

---

## 1. What the four minutes actually were

The log recorded the whole failing session, so this does not need to be
reconstructed from memory. From [`logs/app.log`](../logs/app.log):

| Time | Event |
|---|---|
| 16:01:38 | Dropped the locked identity `test-111` — 25 frames, no usable match |
| 16:02:00 | Dropped again |
| 16:02:28 | Dropped again |
| 16:02:56 | Dropped again |
| 16:03:54 | Dropped again |
| 16:04:41 | Dropped again (**six times, ~3 min 3 s**) |
| 16:04:56 | Liveness timed out on step **RIGHT**, redrawn (restart 1) |
| 16:05:06 | Timed out on **RIGHT** (restart 2) |
| 16:05:21 | Timed out on **RIGHT** (restart 3) |
| 16:05:33 | Timed out on **RIGHT** (restart 4, **37 s total**) |
| 16:05:45 | Through at last — then 106 × `Rejected attendance: test-111 is not enrolled in subject 1` |

**Total ≈ 4 min 7 s**, which matches the reported "~3 mins" for the liveness
stage.

⚠️ **The liveness challenge is not the main cost.** Identity churn is
**~3 minutes (74%)**; liveness timeouts are **37 s (15%)**. Every one of those
six drops throws away the locked identity, so the track must re-accumulate a
full 20-frame prediction window *and* 8 consecutive agreeing frames before the
challenge can even start again.

**This matters for the proposal:** replacing the challenge with MiniFASNet
removes the 15%, not the 74%. On this evidence, the two proposed approaches
are in the wrong priority order.

Two more things the log gives away for free:

- **The frame interval is ~210 ms — about 4.8 fps.** The `Rejected attendance`
  lines fire once per frame, and they are 205–220 ms apart throughout. The
  code's own comments assume 7–15 fps; the real figure on this hardware is
  roughly a third of that. §2 accounts for it exactly.
- **All four timeouts are on `RIGHT`. Never `LEFT`, never `CENTER`.** Both
  turn steps use the same ±0.10 face-width threshold, so this asymmetry is not
  explained by the configuration. It is a real signal and §5.3 proposes how to
  measure it, but it is a **hypothesis, not a finding** — I have no camera here.

---

## 2. Root cause A — the liveness challenge only advances on frames LBPH could read

**Confirmed by execution, not by reading.** Same track, same head pose, same
yaw; the only difference is whether LBPH produced a strong match on that frame:

```
strong LBPH match      -> liveness index 1, passed=True
no usable LBPH match   -> liveness index 0, passed=False
```

[`recognize_face.py:897-931`](../recognize_face.py#L897) returns early when
`candidate_id is None`, so `state.liveness.update(current_yaw)` at
[line 958](../recognize_face.py#L958) is never reached. The 8-second step
timeout is wall-clock (`time.monotonic`) and keeps running regardless.

So the system asks the student to turn their head, and then **ignores the turn
on exactly the frames the turn produces** — a moving head is motion-blurred,
and a blurred crop is what `RECOGNITION_QUALITY.min_blur_variance = 50.0`
refuses ([vision/quality.py:97](../vision/quality.py#L97)). A profile view also
raises LBPH distance above the 58.0 threshold. Both routes produce
`candidate_id is None`.

⚠️ **This is a re-occurrence, not a new bug.** The comment at
[recognize_face.py:890-896](../recognize_face.py#L890) describes this exact
failure and fixes half of it: it stopped *redrawing* the sequence on unreadable
frames. It did not make the sequence *advance* on them. The yaw is already
computed and already passed in — `get_face_yaw()` needs only the mesh
landmarks, which are present whenever MediaPipe found the face at all.

**The fix is small and does not need any new dependency:** evaluate the
challenge from the landmarks on every frame that has a face, independently of
whether LBPH read an identity that frame. Identity is what the post-challenge
re-check (`post_match_frames = 8`) is for, and that stays as it is.

Related, same block: `note_identity_mismatch()` at
[line 901](../recognize_face.py#L901) resets `post_match_count` to zero on
every unreadable frame, so the 8-frame recheck also restarts constantly.

---

## 3. Root cause B — the camera is never released, and a dead camera is never noticed

Three separate defects that compound into "crashes after ~3 min" and "can't
pick up the camera after a relogin".

**3.1 `logout()` does not stop the session.**
[`web/auth.py:157-159`](../web/auth.py#L157) is `session.clear()` and a
redirect. It never calls `stop_camera()` — the module is not even imported. So
after a logout the `RecognitionSession` still has `_running = True`, still holds
`_capture`, and its reader thread is still calling `cap.read()` in a loop. On
relogin, [`vision/session.py:267`](../vision/session.py#L267) refuses to start:
*"a session for … is still running. End it first."* The camera is held by your
own process.

**3.2 The viewer slot leaks.** `generate_frames()` releases it in a `finally`
([recognize_face.py:1512](../recognize_face.py#L1512)), but a generator's
`finally` only runs when the generator is closed or collected. When a browser
navigates away from an MJPEG stream under the Werkzeug dev server that is not
prompt. Until it happens, `acquire_viewer()` raises `SessionBusy` and the next
attempt is refused with *"the camera stream is already open in another
window"*.

**3.3 A camera that dies mid-session is invisible.**
[`infra/camera.py:125-132`](../infra/camera.py#L125) counts `_read_failures`
and backs off 20 ms. **Nothing ever reads that counter.** A camera that stops
producing frames — USB drops, driver wedges, another app grabs it — produces a
silent, permanent stall: `read()` returns `(False, None)`, the loop `continue`s,
and no line is written anywhere. `read_failures` is exposed as a property and
has no caller in the codebase. That is the same "code that looks like it works"
shape as CAM-1 and PE-0.

**None of this needs MiniFASNet or a pipeline rewrite.** It is a missing
`stop_camera()` on logout, an `atexit`/teardown hook (there is none — `app.py`
has no `atexit`, no signal handler and no shutdown path), and a threshold on a
counter that is already being maintained.

---

## 4. Root cause C — the recognition pipeline, measured

Per-frame cost on this machine, medians:

| Stage | Cost | Notes |
|---|---:|---|
| MediaPipe FaceMesh @960px, `refine_landmarks=True` | **32.9 ms** | `max_num_faces=10` |
| …same, `refine_landmarks=False` | 30.5 ms | **only 2.4 ms saved** |
| …same, @640px | 31.0 ms | **1.9 ms saved** — resolution is not the cost |
| **LBPH `predict()`, real model, 4 students** | **52.5 ms** | per face, per frame |
| JPEG encode, 1280×720 q70 | **30.2 ms** | per frame |

≈ 115 ms/frame with one face, against 210 ms observed — the rest is Flask,
MJPEG framing, drawing and GIL contention with the reader thread.

**The part that decides the answer: LBPH is linear in the number of stored
training images**, because it compares the query histogram against every one of
them. Measured across a 10× range:

| Students | Stored images | `predict()` median | per stored image | Model size |
|---:|---:|---:|---:|---:|
| 3 | 300 | 33.9 ms | 113 µs | 62 MB |
| 5 | 500 | 53.1 ms | 106 µs | 104 MB |
| 10 | 1 000 | 103.5 ms | 104 µs | 208 MB |
| 20 | 2 000 | 226.0 ms | 113 µs | 416 MB |
| **30** | **3 000** | **278.1 ms** | 93 µs | **625 MB** |

Clean linearity at ~0.1 ms per stored image. The real 72 MB model measures a
little worse than synthetic data at the same count (131 µs/image), so **at 30
students expect ~280–400 ms per face per frame.**

### What that means for a 30-student roster

| | today (4 students) | 30 students, extrapolated |
|---|---:|---:|
| One face in frame | ~210 ms → **4.8 fps** | ~460 ms → **~2 fps** |
| Two faces in frame | ~260 ms | ~850 ms → **~1.2 fps** |
| Time to fill the 20-frame confirmation window | ~4 s | **~10–17 s, per student** |
| Model on disk | 72 MB | **~625 MB** |

⚠️ **This reopens `todo.md` §7 Q1 with evidence that was not available when it
was decided.** Q1 settled on keeping LBPH and recorded the ceiling as a
*storage* limit — "roughly 31–100 students, with the lower bound being the safe
planning figure", derived from `cv::FileStorage` failing to read a model
somewhere between 0.57 GB and 1.84 GB. **625 MB at 30 students is already
inside that danger band**, and the audit never costed the *latency*. The
latency ceiling bites earlier and harder than the loadability one: at ~2 fps
with a 1.5 s track timeout ([tracking.py:44](../vision/tracking.py#L44)), a
track is three frames from expiring at all times.

**I am not proposing to overturn Q1.** It was decided on manuscript grounds and
those grounds have not changed. This is the measured evidence CLAUDE.md
requires me to bring back rather than act on.

---

## 5. Feasibility of the two proposed approaches

### 5.1 Replace the liveness challenge with MiniFASNet — **feasible, with real caveats**

MiniFASNet (the Silent-Face-Anti-Spoofing models, Apache-2.0) is a passive
spoof classifier: one small CNN over an 80×80 face crop, returning
real/print/replay. It does not ask the user to do anything.

**In favour:**

- **It removes the interaction entirely.** No prompts, no timeouts, no
  restarts. The 37 s of timeouts and the *"which way is my left?"* problem both
  disappear.
- **Cost is small and roster-independent:** ~1.9 MB and ~3.4 MB models,
  roughly 5–15 ms per face on CPU. The reference implementation ensembles two,
  so budget ~10–30 ms — comparable to one FaceMesh call, and **far** below the
  280–400 ms LBPH will cost at 30 students.
- **It is honestly stronger than what is there now.**
  [`vision/liveness.py:57-72`](../vision/liveness.py#L57) already concedes the
  current challenge does not stop a replay: *"a recording that cycles left,
  centre, right, centre satisfies every one of the six sequences given enough
  time"*. Texture-based detection is the thing the docstring says is needed and
  calls out of scope.
- **Python 3.11 is fine.** `onnxruntime` supports it and is ~50 MB, versus
  PyTorch at ~2.5 GB on Windows CPU. Export to ONNX once, ship the `.onnx`.

**Against, and these are the ones that decide it:**

- ⚠️ **Uncalibrated thresholds are precisely why blink detection was
  rejected.** [`vision/liveness.py:8-14`](../vision/liveness.py#L8) records
  the user's 2026-08-08 decision: *"the thresholds are uncalibrated for this
  camera and this lighting … a threshold slightly wrong means a student who
  cannot mark attendance at all"*. A pretrained MiniFASNet carries exactly that
  risk — it was trained on someone else's camera, lighting and demographics. It
  needs a spoof/live validation set on **this** camera before any threshold can
  be defended. That is not hard, but it is real work and it is data collection.
- ⚠️ **New dependency, and `pyproject.toml` pins everything on purpose.** Two
  additions (`onnxruntime`, plus the model file) and a licence/attribution note
  in the manuscript.
- ⚠️ **A binary spoof score is harder to explain to an examiner than a head
  turn**, and it fails closed in a way a student cannot argue with. The current
  challenge at least tells them what to do.
- **It fixes 15% of the observed delay.** On its own it will not make the
  reported problem go away.

**Verdict: feasible and defensible, but it is not the fix for the 3 minutes.**
It is the fix for the *security* weakness SE-12 left open, and it happens to
also remove the interaction cost. Do §2 first — it is a handful of lines, no
dependency, and addresses a larger share of the delay.

### 5.2 Optimise the recognition pipeline — **feasible in tiers, with a hard ceiling**

Ordered by measured benefit per unit of risk:

| # | Change | Expected gain | Risk |
|---|---|---|---|
| 1 | **Advance liveness on unmatched frames** (§2) | Removes most of the 37 s and much of the churn | Very low — no threshold moves |
| 2 | **Stop-camera on logout + atexit + surface `read_failures`** (§3) | Fixes the crash and the relogin lockout outright | Very low |
| 3 | **Don't run LBPH on every frame of a confirmed track** | ~52 ms/frame back today; **~300 ms at 30 students** | Low — identity is already locked; the recheck can sample every Nth frame |
| 4 | **Move JPEG encoding off the recognition thread** | ~30 ms/frame | Low–medium |
| 5 | **Cut `max_num_faces` from 10 to what a kiosk actually sees** | Bounds worst case; 10 faces × 400 ms is a 4 s frame | Low |
| 6 | **Shrink the LBPH grid (8×8 → 6×6)** | ~45% off predict | ⚠️ **High — changes the distance scale, so `RECOGNITION_THRESHOLD` must be re-derived. This is [lessons.md L2](lessons.md) exactly, and L2 is the mistake that broke recognition for every user.** |
| 7 | **Cap images per student below 100** | Linear: 50 images ≈ half the predict cost | Medium — costs accuracy, and needs a re-measured DET curve |
| 8 | **Replace LBPH with an embedding model** | ~5–10 ms **regardless of roster size**, model ~5 MB not 625 MB | ⚠️ **Overturns §7 Q1 — a manuscript decision, not an engineering one** |

**Items 1–5 are straightforward, need no new dependency, and no threshold
moves.** Together they should take the current ~210 ms/frame to roughly
100–120 ms, and — more importantly — keep the *confirmed-track* path nearly
free, which is what makes 30 students survivable at all.

⚠️ **Items 1–7 do not remove the ceiling; they postpone it.** LBPH's cost is
linear in enrolled students and no amount of tuning changes that. Only item 8
does, and item 8 is Q1.

**Note that items 8 and 5.1 share one dependency.** If `onnxruntime` is added
for MiniFASNet, an embedding recogniser costs no *further* dependency — which
makes it tempting, and is exactly why it should be a deliberate decision rather
than a side effect.

### 5.3 The `RIGHT`-only asymmetry — measure before fixing

Four timeouts, all on `RIGHT`, none on `LEFT` or `CENTER`. Both use the same
threshold magnitude, so something is biasing yaw positive.
`get_face_yaw()` is `nose.x - eye_centre.x` scaled by face width
([recognize_face.py:566](../recognize_face.py#L566)); a constant positive
offset — camera not centred, a seating angle, or a landmark bias — would make
`RIGHT` need a far larger physical turn than `LEFT` while looking symmetric in
the code.

**Cheapest measurement:** log `get_face_yaw()` at DEBUG for one session and
plot the distribution while the subject faces forward. If it does not centre on
zero, that is the answer, and the fix is a per-session bias estimate rather
than a threshold change. **Do not adjust the thresholds before measuring this
— L23 is a threshold in the wrong units, three sprints running.**

---

## 6. What I recommend, and what is yours to decide

**Not a decision — just broken, and I can fix it in one sprint:**

1. §2 — advance liveness from landmarks, independently of the LBPH read.
2. §3 — `stop_camera()` on logout, a shutdown hook, and a warning when
   `read_failures` climbs.
3. §5.2 items 3–5 — skip redundant predicts, bound `max_num_faces`.

These need no new dependency, move no calibrated threshold, and address the
crash, the relogin lockout, and most of the delay.

**Yours to decide** (`todo.md` §7 territory — bring to the manuscript, not to
me):

- **Q4 — adopt MiniFASNet?** Feasible; needs `onnxruntime`, a model file, and a
  spoof/live validation set captured on this camera. Strengthens SE-12, which
  the code currently admits is open. Costs a manuscript section.
- **Q5 — does §7 Q1 survive the latency evidence?** Q1 assumed a storage
  ceiling of 31–100 students. The measured latency ceiling is tighter: ~2 fps
  and a 625 MB model at 30. If the defended claim is *"this works for a class
  of 30"*, that claim is not currently supported. Options: (a) reduce the
  defended roster size and document the measured ceiling honestly; (b) cap
  images per student and re-derive the threshold; (c) reopen the backend
  decision. **(a) is the cheapest and most defensible, and an examiner asking
  "does this scale?" gets a measured number either way.**

---

## 7. Measurement notes

- All timings: medians, this machine, `.venv` Python 3.11.5, opencv-contrib
  4.10.0.84, mediapipe 0.10.14, on the live 4-student `trainer.yml`
  (72,103,331 bytes).
- Roster-scaling used synthetic 200×200 noise images at the production
  `LBPH_PARAMS` (`radius=2, neighbors=8, grid_x=8, grid_y=8`). Histogram cost
  does not depend on image *content*, only on count and grid — but the real
  model measured 131 µs/image against synthetic ~105 µs, so the extrapolations
  quote a range rather than a point.
- The 4.8 fps figure is from production log timestamps, not a benchmark. It
  agrees with the summed stage costs, which is why I trust both.
- ⚠️ **`refine_landmarks=False` saves only 2.4 ms and dropping to 640px saves
  1.9 ms.** Both are commonly recommended MediaPipe optimisations and **neither
  is worth doing here** — I measured them because they looked obvious.
