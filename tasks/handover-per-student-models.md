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
- ~~Not incremental~~ - done, see §6.
- **Still one session for the whole server** (§19 step 9). Nothing here changed that.

## 4. Next (todo.md §19)

~~2: load only the running class list's models~~ done, as option B - see §5.
9: one `RecognitionSession` per running class. 6: measure at a realistic class size. 8: central server setup.

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

## 6. Same evening: incremental training

- `train_model(full=False)`: unchanged students are hard-linked from the live `students/` into the staging dir,
  so the all-or-nothing pair and `.bak` are untouched. Reuse needs: model mtime > the folder's mtime and every
  file's (the folder's own mtime is what catches a *deleted* image), and `_training.txt` in `students/` equal to
  `training_fingerprint()` (OpenCV version, LBPH_PARAMS, sha256 of face_preprocessing.py). CLI `--full`.
- ⚠️ **The margin is not decoration.** New models are stamped `start - 2 s`. Measured: the last image written
  2 ms before training began had an mtime *equal* to `time.time()` at the start, and the test that caught it
  failed only when run with other tests. Removing the margin brings back a skipped-image bug.
- Measured on 17 real students: nothing changed 0.0 s, one changed 1.3 s, `--full` 22.2 s. After the
  rebuild, fresh single model vs per-student: 1,700/1,700 bit-exact. Tests 1,953 passed / 99 skipped.
  Mutations (drop the mtime check; ignore the fingerprint) fail 3 and 1 tests.
- The live `students/` was rebuilt twice by the CLI this session (first run trained everyone to write the
  fingerprint). `serve.py` picks it up on the next Start or restart.

## 7. 2026-10-28/29: the administrator's Activity page (`/activity`, admin only)

User request: the admin should see what the system is doing. Three sections:
- **Face captures** - the one capture running now, read live from `enrolment_slot` (who, whose, X of 100, how
  long), and history in `capture_events` (migration 014): completed / cancelled / failed / abandoned, recorded
  by `services/capture_history.py` at every end of a capture, including the idle sweep in `EnrolmentSlot.current()`.
- **Attendance sessions** - existing `attendance_sessions`, with Present/Late/Absent counts. "Not ended" also
  marks 3 sessions from 2026-09-01 the server never saw end.
- **Sign-ins** - `login_events` (migration 013): web + app, success / failed / locked. No IP stored.

⚠️ Both recorders **never raise** and use `db_cursor(commit=True)` - the first version lacked it and silently
rolled back every row; `tests/integration/test_activity_history.py` fails if that returns. ⚠️ `tests/conftest.py`
stubs both recorders for every test (`login_history_calls`, `capture_history_calls`, `.real` for the original):
without that, route tests write fake rows into the real database whenever MySQL is up.
Migrations 013 and 014 apply automatically on the next `serve.py` start. Tests 2,011 passed; integration 100.

## 8. 2026-10-30: several classes at once (§19 step 9)

`recognize_face.session` is gone. `recognize_face.sessions` is a `vision.session.SessionRegistry`: one
`RecognitionSession` per running subject (key `subject_id`). Read `tasks/todo.md` §19 "Step 9 plan" for the design.

- **Limits** (both under one lock, racing Starts tested): `settings.max_concurrent_sessions` (5, measured ceiling
  on this laptop - see todo.md 9e) and **one session on the server's own camera** (`ServerCameraBusy`); other
  rooms use "This device's camera". Per-session `recognition_lock` and upload overlays (were module globals).
- **Shared models**: `load_shared_model()` caches by (files on disk, student set); `preload_model()` warms it.
- **Which session a request means** (`web/sessions.py`): `OPERATING_SUBJECT_KEY` set at Start; else the only
  running class this login may use; else None. `/attendance/live` and `/video_feed` take `?subject_id=`.
  Uploads find their session by `BROWSER_CAMERA_KEY` (= attendance_sessions row). `/stop_camera` stops only
  this login's class.
- **End** ends the submitted subject's session only. ⚠️ R3/FS-16 kept: while this login's class runs, a subject
  with no session is refused; the DB fallback ignores open rows of classes *running now* (another operator's)
  and refuses only on a forgotten open row this login may use.
- **Phone**: `web.api._session_for_student()` - a running class the student is enrolled in, newest first; never
  another class (tested).
- **Activity**: "Running now" vs "Never ended" for unended rows.
- Tests: `tests/fake_sessions.FakeRegistry` replaces the old `recognition_session` swaps.
- ✅ **Confirmed working live by the user (2026-10-30)** after the two-device test plan (THS2 + tt10 run
  side by side on two laptops via faceattend.me). Pushed as `72977ba`.
- Still a server-wide cap, not per class: `MAX_PHONES = 4` phone verifications at once
  (`services/phone_verification.py`). With several classes a 5th phone gets 429 and retries.
