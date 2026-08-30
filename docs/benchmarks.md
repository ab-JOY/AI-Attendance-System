# Benchmarks

**Measured 2026-08-09** on the deployment machine, at the end of Phase 3.
Every number here was produced by a run on that day unless it is explicitly
marked as carried forward. `tasks/lessons.md` L8 is the reason for that
distinction: a handover that does not say which figures are fresh lets a
five-sprint-old number travel into the manuscript as a current one.

**Machine:** Windows 11, Python 3.11.5, OpenCV 4.10.0, MediaPipe 0.10.14.
**Model:** LBPH `radius=2, neighbors=8, grid 8×8`, 3 identities, 300 samples.

Reproduce with the commands in §6.

---

## 1. Cold start

Median of three cold subprocess runs. "Peak" is peak working set for the
process, read from `GetProcessMemoryInfo`.

| | before (Phase 3a) | after (Phase 3b) | change |
|---|---:|---:|---|
| `import recognize_face` | 9.15 s / 168 MB | **1.57 s / 55 MB** | **5.8× faster** |
| `import app` | 12.16 s / 204 MB | **3.18 s / 95 MB** | **3.8× faster** |

**What changed (PE-4).** Both imports used to load the 55 MB LBPH model and
construct a MediaPipe FaceMesh at module scope. Measured breakdown of the old
cost: `cv2` 0.60 s, **`mediapipe` 2.78 s**, LBPH read ~4.8 s. Deferring only
the model would have left roughly 5.5 s, so the MediaPipe import moved inside
`make_detector()` as well. Nothing happens at import now; the model is loaded
at session start and cached against a signature of the file on disk.

What remains is library import cost, which any process using these libraries
pays: cv2 0.60 s, pandas 1.32 s, flask 0.51 s.

> **Update, 2026-08-16 (Phase 5).** `pandas` is no longer imported: PE-8
> replaced `pd.read_sql` with openpyxl, and it was the only caller. Re-measured
> as a median of five cold subprocess imports on this machine, `import pandas`
> is **1.94 s** and `import openpyxl` **1.43 s** — both slower than the 1.32 s
> recorded above, which is a different machine-state, not a regression.
>
> ⚠️ **Do not read a start-up figure off `import app` here.** Consecutive runs
> gave 3.11 s, 3.54 s, 3.59 s, 12.52 s and 14.23 s — dominated by OS file
> caching, not by the import graph. That is why the saving above is quoted as
> the library's own cost rather than as a change in start-up time (L9).

**Consequence for the test suite**, which was the stated payoff:
`tests/conftest.py` no longer forbids importing `recognize_face` or `app`, and
`tests/test_route_security.py` is no longer marked `slow` — 74 access-control
tests, needing no database, now run in the fast suite.

---

## 2. Test suite

| | before | after |
|---|---:|---:|
| Full suite | 286 passed / 58.9 s | **444 passed / 75.7 s** |
| Fast suite (`-m "not slow"`) | 202 passed / 16.4 s | **421 passed / 28.7 s** |
| Tests only in the slow suite | 84 | **23** |

The fast suite grew from 202 to 421 tests — it now covers the route table, the
session lifecycle, the camera reader, the camera-selection gate, database
access, background jobs and the liveness state machine, none of which needs
hardware, a model or a database.

---

## 3. The recognition loop

Measured by driving `generate_frames()` over composited 1280×720 frames
through the real MediaPipe detector and the real LBPH model, 90–120 frames
after a warm-up frame.

| scene | throughput | per frame |
|---|---:|---:|
| One face in shot | **6.8 fps** | 147.3 ms |
| Empty frame, no face | **20.5 fps** | 48.7 ms |

This is the first time this figure has been measured at all, so there is no
"before" to compare it against — the loop could not be driven without a camera
until Phase 3a built the headless harness.

**PE-3 is visible in it.** 147 ms with one face against 49 ms with none is
mostly the LBPH predict: 300 stored histograms × 16,384 dimensions per face
per frame. It is a documented limitation, not an open defect — the backend
decision (§7 Q1) settled on keeping LBPH.

### ⚠️ A correction to the PE-6 commit message

The PE-6 commit (`df3c776`) quoted a table of "frames re-processed" for two
assumed consumer costs, 45 ms and 10 ms per frame, and called the 45 ms row
"the realistic one for this deployment."

**That was wrong, and this measurement is what shows it.** The real consumer
costs 48.7–147.3 ms per frame, and the camera is capped at 30 fps (33.3 ms).
The recognition loop is therefore **always slower than the camera on this
machine**, in both the face and no-face cases — so it never outran the camera
under the old code either, and the duplicate-frame half of PE-6 delivers no
measurable saving here.

The producer-side half is unaffected and is the real win:

| | before | after |
|---|---:|---:|
| Failing camera, CPU | **94.7 % of one core** | 0.0 % |
| Failing camera, reads/s | 642,216 | 47 |
| Instant camera, reads/s | 578,590 | 30 |

PE-6's "spins a core at 100 %" was literal, and a camera unplugged mid-session
is exactly when that mattered. The frame-ready signal still earns its place for
two other reasons — it removed the consumer's 20 ms poll-sleep, and `stop()`
now wakes a blocked reader immediately instead of leaving it to time out — but
"80 % of the loop's work was wasted" is not true of this deployment, and the
figure should not be quoted.

See `tasks/lessons.md` L9.

---

## 4. Model

| | value |
|---|---:|
| `trainer/trainer.yml` | **52.4 MB** (3 identities) |
| Per enrolled student | ~17.5 MB |
| Held-out accuracy | **60/60, average LBPH distance 34.95** |

Unchanged by Phase 3 — the phase touched the code around the recognition
pipeline, not the pipeline. The same 60/60 has now held across Phases 0, 1, 2,
3a and 3b, which is the evidence that each of those phases was
recognition-neutral.

⚠️ **That number is not quotable without its caveats.** Same-session split,
no impostors, N = 3. `docs/walkthrough.md` §4 lists all four reasons; a real
FAR/FRR/EER is Phase 6 work, and the Georgia Tech database is the impostor set
it will use.

**The scaling ceiling stands and is a stated limitation.** At ~17.5 MB per
student and a `cv::FileStorage` read failure somewhere between 0.57 GB and
1.84 GB, the hard limit is roughly **31 students** as a planning figure.
See `tasks/todo.md` §2.2 (PE-2) and §3a.

### 4b. Training cost *(measured 2026-08-29)*

§7 used to list this as unmeasured. It matters now because the demo machine
retrains on **every version update**, which makes the retrain part of the
deploy rather than an occasional operator action.

| | value |
|---|---:|
| Full retrain, 4 identities / 400 source images | **17 s** |
| Model produced | 72 MB, 4 identities |
| Held-out accuracy on that model | **80/80, average distance 33.46** |

⚠️ **The 80/80 above is not an improvement on the 60/60 in the table before
it.** It is a different model: the dataset gained a fourth identity, so it is
80 test images across 4 students rather than 60 across 3. Neither number is
comparable to the other, and the §3 caveats in `walkthrough.md` apply to both
equally — same-session split, no impostors, small N. This row exists to record
what the current tree actually reproduces, so that a figure quoted from an
older document can be recognised as belonging to an older model.

⚠️ **Not a linear extrapolation.** Training reads every image and augments it,
and the model is one histogram per training image, so the cost grows with the
roster — but 17 s at four students is one point, not a curve. Re-measure
before quoting a figure for a class-sized roster.

Reproduce with `python train_model.py`, which now also reports a **partial**
retrain through its exit status (3, `EXIT_INCOMPLETE`) rather than exiting 0
with the skipped students named only in prose.

---

## 4a. Impostor distances, and the confirmation bar *(Phase 4)*

Measured 2026-08-11 against the deployed model, prompted by a live run in which
a genuine student sat at "Verifying 20/20 100% (56.1)" indefinitely and was
never recorded.

**The cause was two thresholds on one scale.** A frame's prediction is voted
into the window at or under `RECOGNITION_THRESHOLD` (58.0); the window average
then had to beat `TrackConfig.confirmation_confidence` (52.0) to lock the
identity. Anything settling between them produced a full, unanimous window that
could never confirm — silently, with nothing logged. This is `lessons.md` L2
again: a second threshold on a metric whose scale was set elsewhere.

Before changing it, the question "what does that 6-point band actually protect
against?" was measured rather than argued:

| population | n | min | median | max | ≤ 52.0 | ≤ 58.0 |
|---|---:|---:|---:|---:|---:|---:|
| Genuine — dataset crops | 60 | 18.7 | 29.8 | **34.9** | 100 % | 100 % |
| Impostor — Georgia Tech, 50 unenrolled subjects | 750 | **62.2** | 75.9 | 95.9 | **0 %** | **0 %** |

Per-subject **mean** distance is the quantity the confirmation bar actually
compares against, and across the 50 impostor subjects the lowest is **69.9**
(closest five: s37 69.9, s23 71.3, s19 71.5, s24 71.8, s36 71.9).

**Nothing in either population lies between 52.0 and 58.0.** Moving the bar to
58.0 therefore costs zero measured false acceptances and still leaves 4.2 of
headroom to the single closest impostor frame. `confirmation_confidence` is now
derived from `RECOGNITION_THRESHOLD` rather than restated, so the two cannot
drift apart again, and `tests/test_vision_tracking.py` fails if a gap reopens.

⚠️ **Three limits on this, and they matter.**

1. **This is not a false-acceptance rate.** The Georgia Tech images are stills
   from a different capture condition. They establish that unenrolled faces
   score far above the operating point on *this* model; they do not establish
   FAR under this system's own camera. That is Phase 6.
2. **The genuine column is same-session and therefore optimistic** — the §3
   leakage caveat in `walkthrough.md`. Its own evidence is the live 56.1 that
   started this: real conditions put a genuine match 21 points worse than the
   worst same-session crop, which is exactly why the band was reachable in a
   room and not in the evaluator.
3. **This does not calibrate anything.** It justifies closing a gap, not the
   value 58.0, which remains the measured-working number the DET curve in
   Phase 6 is meant to replace.

Reproduce with the impostor set at `gt_db/` (gitignored, and carrying its own
usage terms — check them before publishing any figure derived from it).

---

## 5. Code size

Measured 2026-08-16, after Phase 5. The Phase 3 figures are kept in the right
column because the trend is the point.

| module | lines | note |
|---|---:|---|
| `app.py` | **161** | 1,749 at Phase 3; 2,890 before the split. A factory call and `__main__` |
| `web/` | 2,265 | 8 blueprints — HTTP only |
| `repositories/` | 884 | every SQL statement; each takes a cursor, none opens one |
| `services/` | 371 | enrolment slot, training job, session lifecycle, reporting |
| `recognize_face.py` | 1,399 | 1,817 at the start of Phase 3 |
| `vision/` | 2,604 | no hardware, no database |
| `infra/` | 1,516 | camera, database pool, background jobs, uploads, dataset store, migrations |
| `security/` | 714 | access control, bcrypt, path validation, throttling |
| `static/js/` | 980 | **0 before Phase 5** — every line of this was inline in a template (US-8) |
| `capture_dataset.py` | **deleted** | 1,586 lines, all logic at module scope (MA-2) |

`vision/`, `infra/`, `repositories/` and `services/` are all cheap to import and
testable with fakes, which is what keeps a 1,101-test suite at ~80 s.

⚠️ **`web/` is larger than the `app.py` it replaced (2,265 against 2,890 for
routes alone), and that is expected rather than a regression.** The split adds
eight module docstrings and eight import blocks. What changed is not the total
but where a given kind of change lands: a SQL edit touches `repositories/`, a
status-code edit touches `web/`, and neither can any longer be made by accident
while editing the other.

---

## 6. Reproducing these

```bash
# Cold start (median of three; the peak-memory helper is in the commit note)
for i in 1 2 3; do
  .venv/Scripts/python.exe -c "import time;t=time.perf_counter();import recognize_face;print(f'{time.perf_counter()-t:.2f}s')"
done

# Test suites
.venv/Scripts/python.exe -m pytest tests/ -q
.venv/Scripts/python.exe -m pytest -m "not slow" -q

# Held-out accuracy (needs the gitignored dataset/ and trainer/)
.venv/Scripts/python.exe eval_heldout_accuracy.py

# Model size
ls -l trainer/trainer.yml
```

The recognition-loop throughput and the PE-6 producer figures were measured
with scratch harnesses rather than committed scripts, because both need the
gitignored `dataset/`. The loop figure can be reproduced from
`tests/test_recognition_loop_smoke.py`, which drives the same path; the PE-6
figures need the pre-PE-6 `CameraReader`, which is one `git show` away
(`git show df3c776^:recognize_face.py`).

---

## 7. What is not measured here

- **Live camera FPS.** Every figure in §3 comes from composited stored images,
  not a live feed. A real camera adds capture latency and real exposure
  variation. This needs the DroidCam run that is handed to the user.
- **Recognition latency under more than one face.** The tracker and the
  per-face LBPH predict both scale with faces in shot, and the dataset has
  three identities, so a classroom-sized test is not possible yet.
- **Anything about accuracy beyond §4.** FAR, FRR, EER and a justified
  operating threshold are Phase 6.
- **Database throughput.** The pool was introduced for exception safety
  (PE-7); no load test was run, and a single-process Flask development server
  serving a handful of concurrent requests would not show anything useful.
- ~~**Training cost.**~~ Measured 2026-08-29 - see §4b.
