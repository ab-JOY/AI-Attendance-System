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

---

## 5. Code size

| module | lines | note |
|---|---:|---|
| `app.py` | 1749 | still one file; blueprints are Phase 5 (MA-1) |
| `recognize_face.py` | 1247 | 1817 at the start of Phase 3 |
| `vision/` | 1741 | 8 modules, no hardware, no database |
| `infra/` | 604 | camera, database pool, background jobs |

`vision/` and `infra/` are both cheap to import and testable with fakes, which
is what moved 219 tests out of the slow suite.

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
