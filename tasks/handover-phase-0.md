# Handover — Phase 0 → Phase 1

**Sprint:** Phase 0, Restore a working system
**Dates:** 2026-08-07 → 2026-08-08
**Status:** Complete. Development paused for handover.
**Baseline commit:** `2ec776d` on `main` (174 files, working tree clean)
**Read with:** [`todo.md`](todo.md) (audit + full plan), [`lessons.md`](lessons.md)

---

## 1. Read this first — five things that will bite you

1. **`dataset/` and `trainer/` are gitignored, and must stay that way.**
   `dataset/` holds face images of three identifiable students — sensitive
   personal information under RA 10173. Git history is permanent and is
   copied to every clone. If you ever run `git add -f` on those paths, stop
   and tell the user. Two checks, both verified to work:
   ```bash
   # 1. Ignore rules still in place? (expects exactly 2 matches)
   [ "$(git check-ignore dataset trainer | wc -l)" -eq 2 ] && echo SAFE || echo STOP

   # 2. After staging, before committing - anything sensitive slip in?
   git diff --cached --name-only | grep -qE '^(dataset|trainer)/' \
     && echo "STOP - sensitive data staged" || echo "SAFE"
   ```
   Do not use `git check-ignore -q` with more than one path — `-q` accepts a
   single pathname only and exits non-zero, producing a false alarm.

2. **There is no pre-Phase-0 state to diff against.** The repository was
   created *after* Phase 0's changes. `2ec776d` already contains them. The
   original code is not recoverable from git — §4 below is the only record
   of what changed.

3. **`recognize_face.py` loads a 55 MB model at import time.** Any script
   that imports it pays ~9 s and a large memory spike before running a
   single line. This is finding PE-4, deliberately left for Phase 3. Do not
   import it in a fast unit test.

4. **The database has live data.** `attendancesystem_db` on `127.0.0.1`,
   user `root`, empty password. 4 students, 1 subject, 0 attendance rows.
   Phase 1 touches no schema, but be aware that `setup_db.py` and
   `database/schema.sql` **both** define the schema and can drift.

5. **`app.py` derives dataset paths from database values** as
   `dataset/{student_id}_{name}`. Changing a student's name in the DB
   without renaming the folder silently breaks delete, edit, and recapture.
   Do not "clean up" student names.

---

## 2. Verified current state

Everything below was measured on 2026-08-08, not inferred.

| Property | Value |
|---|---|
| Model | `trainer/trainer.yml`, 54,984,159 bytes (55 MB) |
| Model load | 9.2 s |
| LBPH config | `radius=2, neighbors=8, grid 8×8`, no augmentation |
| Identities | 3 (`23-1-1-0559`, `23-1-1-0918`, `23-1-1-0920`) |
| In-sample (`test_accuracy.py`) | 300/300, avg distance **0.00** — memorisation, not accuracy |
| Held-out (`test_heldout_accuracy.py`) | 60/60, avg distance 34.95 — inflated, see §6 |
| Prediction cost | 64.2 ms per face |
| `start_camera()` model gate | passes |
| Database | 4 students, 1 subject (`CS401`), 0 attendance rows, 0 instructors |

**Not verified, and cannot be by an agent:** live camera capture, live
recognition, and the browser UI. Everything upstream of the hardware is
verified; a live session needs a camera and a face in front of it.

### Re-establish the baseline before you start

```bash
.venv/Scripts/python.exe -m py_compile app.py recognize_face.py train_model.py
.venv/Scripts/python.exe test_heldout_accuracy.py   # expect 60/60, avg 34.95
git status --porcelain                              # expect empty
```

If the held-out run does not reproduce 60/60, something regressed — stop and
diagnose before building on it.

---

## 3. What Phase 0 set out to do vs. what it found

Phase 0 was scoped at **1 hour**: delete an empty folder, stop one bad folder
from aborting training, make the model save atomic. It took considerably
longer because the stated root cause was wrong.

The audit diagnosed "an interrupted write destroyed the model." True, but
incomplete. The real cause was `LBPH neighbors=12`, which broke the system in
**two independent ways at once**:

1. **Unpersistable.** 262,144-dim histograms → a 1.835 GB model.
   `recognizer.write()` succeeds; `recognizer.read()` fails on
   `persistence.cpp:1613 (-215) ofs == fs_data_blksz[blockIdx]`. Round-trip
   testing put the `cv::FileStorage` ceiling between 0.572 GB and 1.835 GB.
2. **Unmatchable.** Raising `neighbors` shifted match distances into the
   81–107 band while `RECOGNITION_THRESHOLD` stayed at 58.0. Even held in
   memory it rejected every face: **0/60**.

Failure 2 is the more instructive one and is the subject of
[`lessons.md` L2](lessons.md): a parameter change moved the *scale* of a
metric, and a threshold calibrated to the old scale was left behind. Nothing
errored. It silently classified every face as unknown.

---

## 4. Changes made (the only record of them)

### `train_model.py`
- **`write_model_atomically()`** — new. Temp write → `.bak` rotation →
  `os.replace()`, with rollback if promotion fails. Previously the function
  deleted `trainer.yml` and `labels.txt` *before* writing replacements, so
  any mid-write failure destroyed the working model. Verified against four
  cases including the exact failure that caused the outage.
- **Skip, don't abort.** A folder with fewer than `MIN_IMAGES_PER_STUDENT`
  images is now skipped and named in the return message. Previously one
  incomplete folder failed the run for every student — which is how a single
  cancelled enrolment made the system unrecoverable.
- **`LBPH_PARAMS`** — new module constant, imported by both evaluators.
  `neighbors` 12 → 8; augmentation removed.
- **`adjust_gamma()` removed** — only existed to serve augmentation.

### `test_accuracy.py`
- **Fixed a bug that made it score 0 images.** `label_map` stored student
  IDs, and the evaluation loop joined those onto `DATASET_DIR` as if they
  were folder names. Nothing matched, every student was skipped, and it
  printed a clean summary of zeros. Split into `label_map` (IDs) and
  `label_folders` (folder names).
- Added a guard: evaluating 0 images now returns an error instead of 0.00%.
- Imports `LBPH_PARAMS` instead of restating parameters.

### `test_heldout_accuracy.py`
- Imports `LBPH_PARAMS`; augmentation removed so it measures what ships.
- Docstring now states the same-session-leakage and no-impostor limitations.

### `docs/walkthrough.md`
- Rewritten. Reproducible numbers, the configuration comparison table, an
  explicit limitations section, and a **correction log** of claims that could
  not be reproduced. Note that "latency 2.5 s → 1.0 s" and "+5–10 FPS" have
  no measurement harness anywhere in the codebase and remain unsubstantiated.

### Data (MySQL, not in git)
- Deleted empty `dataset/test-id_test student/`.
- Inserted 3 students (College of Computing / BSCS / Y4 / B). IDs and names
  derived from `trainer/labels.txt`, not retyped — see §1.5.
- Normalised `college_department` across all 4 rows to `College of Computing`.
- Inserted placeholder subject `CS401`. **Replace before any evaluation run.**

### New files
`.gitignore`, `tasks/todo.md`, `tasks/lessons.md`, this document.

---

## 5. New findings discovered during Phase 0

Added to [`todo.md`](todo.md); all verified against the live system.

| ID | Sev | Finding |
|---|---|---|
| **PE-0** | P0 | LBPH at `neighbors=12` produces a model OpenCV writes but cannot read. **Fixed.** |
| **SE-15** | P1 | **Passwords compare case-insensitively and ignore trailing whitespace.** The `password` columns use `utf8mb4_general_ci` (case-insensitive, PAD SPACE) and `app.py` compares in SQL. Measured: password `admin` is accepted as `ADMIN`, `AdMiN`, and `admin   `. Collapses an alphanumeric alphabet from 62 symbols to 36 — ~79× weaker for an 8-char password, on top of plaintext storage. **Open — Phase 2 fixes it via bcrypt**, which compares in Python and never lets collation decide authentication. |
| FS-3 | P1 | Upgraded from code-reading to **demonstrated**: `test-id`, unrelated to `CS401`, was reported Absent for it. |
| FS-4 | P1 | Upgraded to **demonstrated**: same session gave `/end-attendance` → `Absent=2` and `/reports` → `Absent=0`. |

**Two corrections I had to make to my own audit** — recorded so you don't
inherit the errors:
- Model size was reported as 878 MB from `ls`. That file was a partial write
  truncated at 48%; the real size is 1.835 GB. All derived scaling figures
  were understated by half. ([`lessons.md` L1](lessons.md))
- I claimed the two `college_department` spellings would split a
  group-by-department report. They would not — the `ci` collation merges
  them. The impact was cosmetic. Chasing that error is what surfaced SE-15.

---

## 6. Do not quote the accuracy numbers without their caveats

Both evaluators report 100%. Neither number means what it appears to.

- **In-sample 300/300 at avg distance 0.00** — every test image is also a
  training image, and LBPH stores one histogram per sample, so each query
  matches its own stored histogram exactly. This only proves the
  train → save → load → predict pipeline is wired up.
- **Held-out 60/60** — all 100 images per student come from **one capture
  session**, so the 80/20 split divides near-duplicate consecutive video
  frames. Not a generalisation estimate.
- **No impostor set**, so false-acceptance rate — the security-critical
  metric — is unmeasured, and the 58.0 threshold remains uncalibrated.
- **N = 3.** A 3-way problem does not extrapolate.

Phase 6 addresses all of this. Until then, treat these as pipeline
sanity checks, not results.

---

## 7. Phase 1 scope, with warnings

From [`todo.md`](todo.md) §5. `git init` is **done**; the rest is open.

- [x] `git init`, `.gitignore`, baseline commit
- [ ] **Delete dead code.** `attendance_system.py`, `admin_panel.py`,
      `db_connection.py`, `main.py`, `face_detect.py`, `camera_test.py`,
      `find_camera.py`, `test_webcam.py`, `test_db.py`, `mediapipe_test.py`,
      `hello_flutter/`, root-level generated CSV/XLSX/DB.
      ⚠️ `attendance_system.py` and `admin_panel.py` **execute on import**
      (one opens a camera, one prompts for a password). Do not import them
      to "check usage" — grep instead. Both target a schema that does not
      exist (`attendance_sessions`, `students.course`), so nothing live can
      depend on them. Confirm with grep before deleting.
      ⚠️ **Delete `haarcascade/` too** (930 KB). Verified 2026-08-08: its
      only reference in the entire codebase is `attendance_system.py:20-21`,
      which is on the deletion list. The live pipeline uses MediaPipe
      FaceMesh, not Haar cascades. Re-confirm after deleting:
      ```bash
      grep -rn "haarcascade\|CascadeClassifier" --include=*.py . | grep -v .venv
      ```
- [ ] **`pyproject.toml`**, pin all deps exactly, document Python 3.11.
      ⚠️ `mediapipe==0.10.14` does not support Python 3.12+. Do not "modernise"
      the interpreter without checking this first.
- [ ] **`config/settings.py` + `.env.example`.** Remove hardcoded credentials.
      ⚠️ They appear in **five** places: `app.py:27-33`, `recognize_face.py:795`,
      `db_connection.py`, `setup_db.py`, `test_db.py`. Two of those files are
      scheduled for deletion — do that first so you migrate three, not five.
      ⚠️ `app.secret_key` is hardcoded in `app.py:21`. Moving it to env
      **invalidates existing sessions** — expected, worth telling the user.
      ⚠️ `RECOGNITION_THRESHOLD = 58.0` is duplicated in three files
      (`recognize_face.py`, `test_accuracy.py`, `test_heldout_accuracy.py`).
      Good candidate for the config layer — and see lesson L2 before changing
      its value.
- [ ] **Replace ~200 `print()` with `logging`.**
      ⚠️ `print()` inside `generate_frames()` runs per face per frame. Route it
      through `DEBUG`, not `INFO`, or the log will be unusable.
- [ ] **Add `pytest`, `ruff`, `pytest-cov`; CI running lint + tests.**
      ⚠️ The four `test_*.py` files are print-only scripts with **no
      assertions**; `pytest` collects nothing today. Worse, pytest will try to
      **import them at collection time**, and `test_accuracy.py` /
      `test_heldout_accuracy.py` import `train_model`, which is fine — but
      anything importing `recognize_face` will trigger the 9 s model load.
      Consider renaming them to `eval_*.py` (they are evaluation harnesses,
      not unit tests) and starting a real `tests/` directory.
      ⚠️ Start with tests for what Phase 0 fixed —
      `write_model_atomically()` and the skip-don't-abort path are pure
      functions of the filesystem and easy to cover. A reference
      implementation of the atomic-write test exists in the session
      scratchpad; if unavailable, §4 describes the four cases.

**Verify at end of Phase 1:** `ruff check` clean, `pytest` runs, app still
boots, and the held-out evaluator still reports 60/60.

---

## 8. Open decisions — blocking later phases

From [`todo.md`](todo.md) §7. **Not the agent's to make.**

1. **Recognition backend (blocks Phase 3).** LBPH at `neighbors=8` is
   ~18.3 MB/student, so the `FileStorage` ceiling returns somewhere between
   ≈31 and ≈100 students. It is a stopgap. The recommendation on the table is
   to keep LBPH for thesis continuity *and* add an embedding backend behind
   the same interface as a documented comparison — turning a weakness into a
   contribution. **User has not decided.**
2. **Deployment topology (blocks Phase 5).** `/capture_face` spawns an OpenCV
   GUI window *on the server*, so the system only works when browser and
   server are the same machine. If remote use is required, enrolment must
   move into the browser and the subprocess design goes. **User has not
   decided.**
3. **Dataset recapture (blocks Phase 6).** Phase 6 is meaningless without
   multi-session data plus impostors. **Feasibility unknown.**

---

## 9. Working agreements observed this sprint

- The user's `CLAUDE.md` asks for plan-first, verification-before-done, and
  `tasks/lessons.md` updates after corrections. All followed.
- **Verify by regenerating, not by reading artifacts** — this sprint's most
  expensive mistake (L1).
- **Blocking decisions go to the user.** When Phase 0 could not complete
  without changing the thesis's documented model configuration, work stopped
  and the user was asked with measured evidence for each option. Do the same.
- Corrections were stated plainly and the affected documents updated, rather
  than quietly patched.

---

## 10. Suggested first move

```bash
git checkout -b phase-1-foundation
```

Phase 1 deletes 11 files and a directory. On a branch that is one command to
undo; on `main` it is not. The baseline commit exists precisely so this is
reversible — use it.
