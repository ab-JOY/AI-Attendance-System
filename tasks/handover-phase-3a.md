# Handover — Phase 3, first half → second half

**Sprint:** Phase 3, Recognition engine — **first half only**
**Date:** 2026-08-08
**Status:** ⚠️ **INCOMPLETE. 2 of 8 Phase 3 items done.** The branch is green
and every commit is verified, but the phase is not finished. Six items remain.
**Branch:** `phase-3-recognition`, 3 commits, branched from `phase-2-security`
**Read with:** [`todo.md`](todo.md) (audit + full plan), [`lessons.md`](lessons.md)

This supersedes [`handover-phase-2.md`](handover-phase-2.md). Where the two
disagree, §4 below says which is right — and there are **three corrections**,
one of which would have sent you down the wrong path.

**When you finish the phase, write `handover-phase-3b.md`.** Do not overwrite
this file; the two halves were done by agents with no shared memory, and the
second half's reader may want both.

---

## 1. Read this first — what will bite you

1. **`dataset/` and `trainer/` are gitignored and must stay that way.** Face
   images of three identifiable students and the biometric templates derived
   from them. Both checks were run today and both work:
   ```bash
   # 1. Ignore rules still in place? (expects exactly 2 matches)
   [ "$(git check-ignore dataset trainer | wc -l)" -eq 2 ] && echo SAFE || echo STOP

   # 2. After staging, before committing - anything sensitive slip in?
   git diff --cached --name-only | grep -qE '^(dataset|trainer)/' \
     && echo "STOP - sensitive data staged" || echo "SAFE"
   ```
   Never use `git check-ignore -q` with more than one path.

2. **There are uncommitted changes in the tree that are the user's, not
   yours.** `git status` shows `M .gitignore` and `?? tasks/notes.txt`. They
   add the Georgia Tech Face Database (`gt_db/`) to the ignore rules and note
   where it came from. **Leave them alone unless the user says otherwise** —
   they were deliberately kept out of this sprint's commits. See §6.

3. **`recognize_face.py` still loads a 55 MB model *and* imports MediaPipe at
   module scope.** Measured today: `import recognize_face` **9.93 s**,
   `import app` **12.16 s**. That is PE-4 and it is still open.
   - **`tests/test_route_security.py` is still the only file under `tests/`
     allowed to import `app`.** `tests/conftest.py` states the rule.
   - `app.py` must still call `configure_logging()` **before** importing
     `recognize_face`. Two imports still carry `# noqa: E402`. They come off
     when PE-4 lands.
   - **Nothing in `vision/` has this problem.** The whole package is cheap to
     import — no camera, no MediaPipe, no database, and `cv2` only for
     `Laplacian`. That is why 81 new tests run in under 2 seconds.

4. **`LBPH_PARAMS` is still not configurable, on purpose**, and
   `RECOGNITION_THRESHOLD` is still 58.0 with `tests/test_settings.py`
   asserting it. Changing it needs Phase 6 calibration
   ([`lessons.md` L2](lessons.md)).

5. **Renaming a student still breaks their dataset folder.** Scheme is still
   `dataset/{student_id}_{name}`. Deliberate, recorded in `todo.md` §7.5.

6. **A new route with no access-control marker is refused, not served.**
   `security/access.py` denies by default. If you add a route and it 403s with
   a log line about an "unclassified endpoint", you forgot `@public` /
   `@authenticated` / `@role_required`.

7. **Never put a real student identifier in a test.** Every identifier in the
   new tests is `SEC-TEST-NOBODY` / `SEC-TEST-NOONE`.
   [`lessons.md` L6](lessons.md) is the story of two live database rows
   deleted by a test run. **This applies to scratch scripts too** — the
   headless smoke harness in §5 reaches `save_attendance()` with a real
   enrolled ID, and stubs it out before the loop is ever started.

---

## 2. Verified current state

Every number below was measured today, on this branch, at commit `f8c41ae`.
Nothing here is carried forward from an earlier handover.

| Check | Result |
|---|---|
| `ruff check .` | **clean** |
| `pytest tests/` | **286 passed in 56.3 s** (205 at the start of this sprint) |
| `pytest -m "not slow"` | **202 passed, 84 deselected, in 16.5 s** |
| `eval_heldout_accuracy.py` | **60/60, avg distance 34.95** — identical to Phases 0, 1 and 2 |
| `import recognize_face` | **9.93 s**, 168 MB peak working set |
| `import app` | **12.16 s**, 204 MB peak working set |
| Route table | **35 routes** + `static`: **3 public, 9 authenticated, 23 admin-only, 0 unclassified** |
| `git check-ignore dataset trainer` | 2 matches — SAFE |
| `git status` | 2 files, **both the user's** — see §1.2 |
| Python | 3.11.5 |
| Model | `trainer/trainer.yml`, 55 MB, 3 identities |

**Cleared since Phase 2:** the outstanding operator action is **done** — the
user has signed in and set a real admin password. `admin`/`admin` is no longer
a working credential.

**Still cannot be verified by an agent:** live camera capture, live enrolment,
and browser UI rendering. **But the recognition loop now can be** — see §5.

### Re-establish the baseline before you start

```bash
git checkout phase-3-recognition
.venv/Scripts/python.exe -m ruff check .              # expect: All checks passed
.venv/Scripts/python.exe -m pytest tests/ -q          # expect: 286 passed
.venv/Scripts/python.exe eval_heldout_accuracy.py     # expect 60/60, avg 34.95
git status --porcelain                                # expect ONLY the user's 2 files
```

If the held-out run does not reproduce 60/60, stop and diagnose. This sprint
did not touch the recognition *pipeline* — only the code around it — so any
movement means something else did.

---

## 3. What changed

Three commits.

### `36a5d3c` — `vision/`: the single face-geometry gate (MA-4)

`get_face_box()` and `is_valid_face_candidate()` existed twice, in
`recognize_face.py` and `capture_dataset.py`, with different thresholds. The
image-quality gate was duplicated the same way — four thresholds and four
message strings adrift. All of it now lives in `vision/` and both modules
import it.

New package: `vision/{__init__,landmarks,geometry,validation,quality}.py`.

**The design decision, and it was the user's:** MA-4 says "delete the
duplicates", which reads as "pick one threshold set". **That is wrong here,
and it is the single most important thing to understand about this module.**
`capture_dataset.py` collects 50 of its 100 images per student at
LEFT/RIGHT/UP/DOWN poses (`LEFT_IMAGES = 15`, `RIGHT_IMAGES = 15`,
`UP_IMAGES = 10`, `DOWN_IMAGES = 10`). The recognition gate refuses any face
whose nose sits more than 0.28 × face-width from the eye centre, because an
attendance mark is a decision and a decision wants a frontal face. Collapsing
the two would make half the capture stages **uncollectable** — and no agent
can see that without a camera.

So: **one implementation, two declared profiles.** `RECOGNITION_PROFILE` and
`ENROLMENT_PROFILE` sit side by side in `vision/validation.py`; every
`... | None` field is a check one stage applies and the other does not. The
duplicate *code* is deleted; the divergence became data.

`tests/test_vision_validation.py::test_turned_head_separates_the_profiles`
pins the reason. **If you are tempted to "finish" MA-4 by merging the
profiles, that test is the argument against it.**

### `da3b0c5` — the per-track state machine (MA-12)

| | before | after |
|---|---:|---:|
| `generate_frames()` | 405 lines, **nesting depth 10** | 115 lines, **depth 3** |
| `recognize_face.py` | 1817 lines | **1261 lines** |

`vision/tracking.py` now holds `TrackConfig` (eleven loose module constants,
now one frozen dataclass), `TrackState` (one face's progress, with named
transitions instead of dict mutation at a distance) and `FaceTracker` (the
track table, as instance state).

`recognize_face.py` keeps what genuinely needs hardware, as named steps:
`detect_faces`, `predict_identity`, `resolve_track_identity`,
`resolve_confirmed_track`, `accumulate_towards_confirmation`,
`hint_for_weak_track`, `encode_frame`.

⚠️ **The branch order in `resolve_track_identity()` is load-bearing and is
unchanged from the original.** A confirmed track is checked *first* and is
immutable. That is what stops a fluctuating LBPH result from renaming a face
mid-session. Do not reorder it for tidiness.

MA-12 asked for depth ≤3 (**met**) and <60 lines. The per-face loop body is
~60; the whole generator is 115, the remainder being frame acquisition and the
MJPEG yield. Called out rather than quietly claimed.

### `f8c41ae` — plan and measurements

`todo.md` Phase 3 checkboxes, with the numbers rather than the intentions, and
the warnings in §6 below.

---

## 4. Corrections to the Phase 2 handover

**Check these against your plan before you build anything.**

1. **§6 said MA-4 "is the first Phase 3 task that can move that figure",
   meaning held-out accuracy, and told you to record it before and after. It
   cannot.** `eval_heldout_accuracy.py` imports `settings`,
   `preprocess_for_lbph`, `LBPH_PARAMS` and `parse_dataset_folder` — never the
   geometry gate. It scores stored 200×200 crops; the gate decides which
   frames are *saved at capture* and *recognised at runtime*, and the
   evaluator exercises neither. Confirmed by running it: 60/60, unchanged.
   See [`lessons.md` L8](lessons.md).

2. **§2 reported the route table as "4 public, 9 authenticated, 22
   admin-only". Measured today: 3 public, 9 authenticated, 23 admin-only**
   (plus Flask's `static`). `app.py` was **not touched this sprint**
   (`git diff --name-only phase-2-security..HEAD` confirms), so this is a
   miscount in that document, not a change. The route-security test still
   passes and still proves 0 unclassified.

3. **§6 treated PE-4 as one cost. It is two.** Measured: `cv2` 0.90 s,
   **mediapipe 4.29 s**, FaceMesh construction 0.04 s, **LBPH model read
   4.83 s**. Deferring only the model load leaves ~5.5 s — **still too
   expensive for `tests/conftest.py` to drop its exception**, which was the
   stated payoff. Defer the MediaPipe import too and `import recognize_face`
   falls to roughly 1 s.

4. **PE-4's "multi-GB RSS" (todo.md §2.2) no longer holds.** That was measured
   against the 1.83 GB model. Today: `import app` peaks at **204 MB**.

---

## 5. Evidence, and what it is worth

**286 tests pass**, 81 of them new. But two of the verifications matter more
than the count:

**The MA-4 extraction was proven behaviour-preserving, not asserted.** The
pre-refactor functions were lifted out of the `phase-2-security` blob with
`ast` and exec'd in a clean namespace — neither module can simply be imported,
one loads a 55 MB model at import and the other opens a camera — then run
against the replacements over 40,000 randomised synthetic meshes:

| | mismatches |
|---|---|
| Recognition gate | **0 / 40,000** |
| Bounding boxes | **0 / 40,000** |
| Quality gate | **0 / 4,000** |
| Enrolment gate | 105 / 40,000 (**0.263%**) |

The 105 are the original's integer truncation (`x + int(width * 0.18)`, with
`nose_x` truncated too), which widened each band by under a pixel.
**That was proven, not inferred:** transcribing the original logic with only
the `int()` calls removed agreed with the new module on all 40,000. Worst
case sat **0.865 px** from a band edge; median 0.339 px. See
[`lessons.md` L7](lessons.md).

**The recognition loop was driven headless, end to end.** No test could reach
`generate_frames()` before this sprint. A fake `CameraReader` fed real dataset
crops composited onto 1280×720 frames, with `save_attendance()` **stubbed out
first** ([`lessons.md` L6](lessons.md) — that path reaches the live MySQL with
a real enrolled ID):

```
frames yielded        : 60, all well-formed MJPEG parts
tracks created        : 1, held across all 60 frames
history                : filled to the 20-frame window, consecutive run 20
identity confirmed     : 23-1-1-0559, correctly
liveness               : passed
save_attendance calls  : 1  ("Present")
database writes        : 0
```

That exercises geometry gate → tracker → predict → resolve → confirm →
liveness → save. **It is worth rebuilding** for the second half; RE-2 and PE-4
both rewrite this path and it is the only non-camera way to see it work. It
was a scratch script, not committed, because it reads the gitignored
`dataset/` and so cannot run in CI. **Consider committing a version that
skips when `dataset/` is absent** — the second half changes far more of this
loop than the first did.

**Held-out accuracy unchanged at 60/60, avg 34.95.** Still means what it
always meant: same-session split, no impostors, N=3. Do not quote it without
the caveats in `docs/walkthrough.md` §4.

---

## 6. What is left, with warnings

Six of eight Phase 3 items. From [`todo.md`](todo.md) §5.

- [ ] **`RecognitionSession` with an `RLock`**, one session at a time (RE-2).
      ⚠️ **RE-2 is staged, not partly done.** MA-12 moved `tracks`,
      `track_verification` and `next_track_id` into a `FaceTracker`, but
      **that instance is still module-level and still unlocked**. `cap`,
      `camera_reader`, `attendance_running`, `current_subject`, `recognized`,
      `recognizer` and `label_map` are all still module globals.
      ⚠️ **RE-10 is still live and was re-confirmed by the smoke run:**
      `generate_frames()` calls `cap.release()` when it exits, so one browser
      tab closing still ends the session for everyone. Fix it here.
      ⚠️ `/video_feed` is `@authenticated`, which does **not** serialise
      access — a second tab still gets a second generator.
- [ ] **Load the model once at session start** (PE-4). See §4.3: defer the
      MediaPipe import too, or the payoff does not arrive. Removes the
      `# noqa: E402` pair in `app.py` and lets `conftest.py` drop its
      exception and `test_route_security.py` stop being `slow`.
- [ ] **Throttle `CameraReader`** (PE-6). It is still the tight unthrottled
      loop in `recognize_face.py`; MA-12 did not touch it.
- [ ] **MySQL connection pooling** (PE-7).
      ⚠️ **Audit before you pool.** There are **29** `get_db_connection()`
      calls in `app.py` and **28** `conn.close()`. Today an unclosed
      connection leaks and MySQL eventually reaps it; **behind a pool it
      exhausts the pool and hangs the app.** Find the mismatch first.
- [ ] **Training as a background job** with a status endpoint (PE-5, US-2).
      ⚠️ The status endpoint needs `@authenticated` **and** `@json_api`, or
      the polling JavaScript gets an HTML redirect and parses it as JSON.
      ⚠️ `/train_model` is admin-only. If instructors should be able to
      trigger a retrain, that is a decision, not an oversight.
      ⚠️ `app.py` calls `train_model()` synchronously in **two** places after
      enrolment (around lines 466 and 988) and branches on the result. Making
      training asynchronous changes what those two callers can promise the
      operator.
- [ ] **Strengthen liveness** (SE-12).
      ✅ **Decided 2026-08-08 by the user: a randomised multi-step challenge,
      and no blink detection.** Keep the existing turn mechanic but require a
      randomised *sequence* of 2 steps drawn from LEFT/RIGHT/CENTER with
      per-step timeouts, so a single pre-recorded clip cannot satisfy it.
      **Blink/EAR detection was explicitly declined** — the thresholds are
      uncalibrated for this camera and lighting, glasses degrade them, and a
      threshold slightly wrong means a student who cannot mark attendance at
      all, days before a prefinal defense.
      ⚠️ Build it as a pure state machine in `vision/` and unit-test it with
      synthetic yaw sequences. **Then hand it to the user for a live camera
      run** — no agent can verify this one.
      ⚠️ Keep `docs/data_privacy.md` §7.6 honest about the residual replay
      risk. A randomised sequence raises the bar; it does not close it.
- [ ] **Verify:** before/after model size, cold start and FPS in
      `docs/benchmarks.md`. **The "before" numbers are already measured** —
      §2 above and `todo.md`. `docs/benchmarks.md` does not exist yet.

**Verify at end of Phase 3:** `ruff` clean, `pytest` green, app still boots,
and `eval_heldout_accuracy.py` reports a number you have explained. Then write
`handover-phase-3b.md` and complete the `todo.md` §8 review row for Phase 3.

---

## 7. Open decisions

**All three `todo.md` §7 questions remain answered; nothing in Phases 3–6 is
blocked on a decision.** Two more were settled during this half:

1. ✅ **MA-4 gate unification (user, 2026-08-08): one module, two named
   profiles.** Not a single threshold set. Rationale in §3 and in
   `vision/validation.py`'s docstring.
2. ✅ **SE-12 scope (user, 2026-08-08): randomised multi-step challenge only,
   no blink detection.** Rationale in §6.

### One thing the user has in flight that may reshape Phase 6

`gt_db/` is on disk — the **Georgia Tech Face Database, 50 subjects × 15
images** — correctly gitignored, with the user's `.gitignore` and
`tasks/notes.txt` edits still uncommitted (§1.2).

This was **not** acted on, and it is the user's call. But it is worth raising
with them, because it touches two things `todo.md` currently sequences much
later:

- **§7 Q3 waits for a Phase 5/6 recapture to get an impostor set.** 50
  non-enrolled identities is an impostor set **that exists today**. An
  open-set FAR/FRR/EER could be computed before the recapture rather than
  after it.
- **§7 Q1 obliges a measured answer to "does this scale to a real
  classroom?"** — the LBPH ceiling of roughly 31 students. Training on 31+
  of these identities turns that from an extrapolation into a measurement,
  which is exactly what an examiner will ask for.

⚠️ Two caveats to carry into that conversation. The GT images are a different
capture condition from this dataset, so they belong as **impostors and
probes, not mixed into training** — mixing them changes what the model is.
And the database carries its own usage terms, which is a separate question
from the RA 10173 consent that governs the three enrolled students.

---

## 8. Suggested first move

```bash
git checkout phase-3-recognition
.venv/Scripts/python.exe -m ruff check .
.venv/Scripts/python.exe -m pytest tests/ -q     # expect 286 passed
```

Then take **RE-2 and PE-4 together, as one commit.** They are listed
separately in `todo.md` but they touch the same seven globals and the same
lifecycle: the session object is what owns the model, and "load once at
session start" is a method on it. Doing them in two passes means rewriting
`start_camera()`, `stop_camera()` and `generate_frames()` twice.

Rebuild the headless harness from §5 first, not last. It is the only way to
see that path work without a camera, and RE-2 and PE-4 rewrite all of it.

The user is running the two halves of this phase in separate sessions and has
asked for this document to bridge them. Assume they will read your handover
the same way.
