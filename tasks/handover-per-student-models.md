# Handover: per-student LBPH models (todo.md §19 step 1)

**Date:** 2026-10-28 · **Branch:** `attendance-browser-camera` · Supersedes nothing; read
[`handover-phone-classroom-code.md`](handover-phone-classroom-code.md) for the phone flow (classroom code since removed).

## 0. State

| | |
|---|---|
| Tests | **1,932 passed, 99 skipped** (all real-face tests ran: smoke loop + phone real) |
| Model on disk | `trainer/students/` = 17 files, 318,513,230 bytes; `labels.txt` byte-identical to before |
| Old model | `trainer/trainer.yml` + `.bak` (636 MB) still on disk, **read by nothing**. User's to delete |
| Server | `serve.py` **must be restarted** to load the new code (L42). Old code + new files also works: it still reads trainer.yml |
| User's own uncommitted files, untouched | `.vscode/settings.json`, `config/settings.py` (`flask_host = "0.0.0.0"` hunk only), `tests/test_camera_selection.py`, the `.docx` |

## 1. What changed and why

User wants LBPH kept (§7 Q1), a large number of students, and many sessions on one central server. One `trainer.yml`
grew 18.7 MB/student and `cv::FileStorage` cannot read past 0.57-1.84 GB, so ~31 students was the ceiling (PE-2).

- `train_model.py`: each student is trained and written alone (`students/{id}.yml`) inside a staging dir;
  `write_model_atomically(write_models)` takes a callback and promotes `students/` + `labels.txt` as a pair with
  one `.bak` generation and rollback - same guarantees as the file pair had. One student's images in memory at a time.
- `vision/student_models.py::StudentModels`: the same `predict(face) -> (label, distance)` the pipeline already
  called, as the min across students, strict `<` in label order = OpenCV's tie rule. Optional pool.
- `recognize_face.load_model_and_labels()` reads labels.txt, then each student's file; returns `StudentModels` over
  `_STUDENT_POOL` (separate from `_PREDICT_POOL` on purpose - nested waits on one pool deadlock).
- `settings.trainer_file` -> `settings.student_models_dir`. preflight's staleness check now uses labels.txt mtime.
  `eval_accuracy.py` uses `StudentModels`.

## 2. Evidence (measured 2026-10-28)

- **Differential:** 1,700 probes (each dataset image mirrored + shifted 3 px, so none is a training image),
  single model vs per-student: **1,700/1,700 bit-exact label and distance**. Distances 48.5-75.8; 601 under 58.0;
  297 probes nearest to another student, matched identically. Script was in the session scratchpad (not kept).
- `test_per_student_models_match_one_combined_model` repeats the claim on real OpenCV with synthetic faces.
- **Speed, per face, 17 students:** single 212 ms · per-student sequential 302 ms · per-student pooled **96 ms**
  (41 ms through the loader). Load 11.5 s either way. Mutation `<` -> `<=` fails 4 tests.

## 3. Warnings

- **Every `predict()` recomputes the query's LBP histogram.** N models = N computations. The pool hides it for one
  or two faces; with a full frame the faces are already spread over CPUs and total work is ~40% above the single
  model. Don't "fix" it by reimplementing chi-square in numpy: summation order changes the last bits, and the
  bit-exact claim (and L2) is what keeps 58.0 valid.
- **Not incremental.** Every retrain still trains all students (28 s at 17). Making enrolment train one student
  means copying unchanged files into the staging dir (hardlinks) - keep the pair atomic.
- **Still one session for the whole server** (§19 step 9). Nothing here changed that.

## 4. Next (todo.md §19)

~~2: load only the running class list's models~~ done, as option B - see §5.
Incremental training (enrol one student, train one). 9: one `RecognitionSession` per running class. 6: measure at a realistic class size. 8: central server setup.

## 5. Same evening: step 2, option B (class list only over the limit)

- User's live test showed Jeff as Chrizol: **no dataset captured in that room** (Jeff and Chrizol are close; 17
  of Chrizol's probes are nearest Jeff). Recapture in the room fixed it (user, 21:27). Not this change: old and new
  models gave identical answers on the same input. Nothing was recorded - 85% agreement + liveness held.
- ⚠️ That recapture changed `dataset/23-1-1-0918`, so the 17:35 `trainer.yml` is **stale**: comparing against it
  gives 1,639/1,700 (all 61 involve Jeff). The valid comparison is a fresh in-memory single model from the current
  dataset: **1,700/1,700**.
- Option B: `settings.recognition_load_all_limit` (50, `RECOGNITION_LOAD_ALL_LIMIT`). Session hooks
  `loads_everyone()` and `students_to_load(subject)`; the in-memory model is keyed by (files, student set).
  `load_model_and_labels(student_ids)` keeps labels.txt labels. Parallel file reads: 11.5 s -> 1.9 s.
- Tests 1,944 passed / 99 skipped. Mutations (ignore the student set in the cache key; preload over the limit)
  each fail a test.
- **The class-list mode has never run in a live room** - only with the limit lowered in a script (7-student class,
  0.66 s). Before relying on it: set `RECOGNITION_LOAD_ALL_LIMIT=5` in `.env`, restart, run a class.
