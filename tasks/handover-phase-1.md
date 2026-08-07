# Handover — Phase 1 → Phase 2

**Sprint:** Phase 1, Foundation
**Date:** 2026-08-08
**Status:** Complete. One item deferred by the user (CI).
**Branch:** `phase-1-foundation`, 3 commits on top of `3bce899`
**Read with:** [`todo.md`](todo.md) (audit + full plan), [`lessons.md`](lessons.md)

Phase 0's handover is history now — read it only for background on the
recognition outage. This document supersedes its §7 (Phase 1 scope).

---

## 1. Read this first — six things that will bite you

1. **`dataset/` and `trainer/` are gitignored and must stay that way.**
   Face images of three identifiable students and the biometric templates
   derived from them — sensitive personal information under RA 10173. Git
   history is permanent and copies to every clone. Both checks below were
   run today and both work:
   ```bash
   # 1. Ignore rules still in place? (expects exactly 2 matches)
   [ "$(git check-ignore dataset trainer | wc -l)" -eq 2 ] && echo SAFE || echo STOP

   # 2. After staging, before committing - anything sensitive slip in?
   git diff --cached --name-only | grep -qE '^(dataset|trainer)/' \
     && echo "STOP - sensitive data staged" || echo "SAFE"
   ```
   Never use `git check-ignore -q` with more than one path — `-q` takes a
   single pathname and exits non-zero, giving a false alarm.

2. **`.env` is required and is not in git.** The app will not start without
   `SECRET_KEY`. A working `.env` exists on this machine; a fresh clone needs
   `cp .env.example .env` plus a generated key. This is deliberate — the old
   hardcoded key is in git history and would let anyone forge a session
   cookie. **Everyone currently logged in was logged out once** when this
   landed; that is expected and does not recur.

3. **`app.py` imports the config object as `app_config`, not `settings`.**
   There is a `def settings()` route handler in that file. Importing the
   config as a bare `settings` lets that def shadow it, and because
   `get_db_connection()` runs per request, **every database call raises
   `AttributeError`** — while the app still boots and every test still
   passes. This actually happened during this sprint; see
   [`lessons.md` L5](lessons.md) and
   `tests/test_no_import_shadowing.py`, which now guards all eight modules.

4. **`recognize_face.py` still loads a 55 MB model at import** — ~9 s and a
   large memory spike (PE-4, Phase 3). Consequences you inherit:
   - Nothing under `tests/` may import `recognize_face` or `app`.
     `tests/conftest.py` says so; `pytest`'s `testpaths = ["tests"]` keeps
     collection away from `eval_*.py`.
   - `app.py` must call `configure_logging()` **before** importing
     `recognize_face`, or that module's startup messages — including "model
     failed to load" — are discarded. That is why two imports in `app.py`
     carry `# noqa: E402`. Remove them when PE-4 is fixed.

5. **`LBPH_PARAMS` is not configurable, on purpose.** It stays a code
   constant in `train_model.py`. Making `neighbors` env-driven would let a
   `.env` edit silently reintroduce PE-0 — a 1.8 GB model OpenCV writes but
   cannot read, *and* a distance scale the threshold no longer matches.
   `RECOGNITION_THRESHOLD` *is* configurable but is still 58.0, and
   `tests/test_settings.py` asserts that. Changing it needs Phase 6
   calibration, not a judgement call ([`lessons.md` L2](lessons.md)).

6. **`app.py` derives dataset paths from database values** as
   `dataset/{student_id}_{name}`. Renaming a student in the DB without
   renaming the folder silently breaks delete, edit and recapture. Do not
   "clean up" student names. Still true, still unfixed — Phase 2's
   path-traversal work (SE-3) is where the surrogate key belongs.

---

## 2. Verified current state

Every number below was measured today, not carried forward.

| Check | Result |
|---|---|
| `ruff check .` | **clean** |
| `pytest tests/` | **35 passed in 3.0 s** |
| `eval_heldout_accuracy.py` | **60/60, avg distance 34.95** — identical to the Phase 0 baseline |
| `import app` + real requests | boots in ~12 s; `POST /login` → 302 `/dashboard`, `GET /students` → 200, `GET /settings` → 200 |
| `py_compile` (11 modules) | clean |
| `git check-ignore dataset trainer` | 2 matches — SAFE |
| Tracked files | 175 → **41** |
| Python | 3.11.5 |
| Model | `trainer/trainer.yml`, 55 MB, 3 identities, load ~9 s |

**Still cannot be verified by an agent:** live camera capture, live
recognition, and the browser UI. Those need a camera and a face. Everything
upstream of the hardware is verified — including, now, real HTTP requests
against the live database, which is further than Phase 0 got.

### Re-establish the baseline before you start

```bash
git checkout phase-1-foundation
.venv/Scripts/python.exe -m ruff check .              # expect: All checks passed
.venv/Scripts/python.exe -m pytest tests/ -q          # expect: 35 passed
.venv/Scripts/python.exe eval_heldout_accuracy.py     # expect 60/60, avg 34.95
git status --porcelain                                # expect empty
```

If the held-out run does not reproduce 60/60, stop and diagnose before
building on it.

---

## 3. What changed

Three commits, one per workstream.

### `ab3d86d` — Delete dead code (MA-3, MA-7, MA-8, CO-6)

143 files. Verified by grep first; the only references to any of them were
from each other.

- **Schema orphans:** `attendance_system.py`, `admin_panel.py`,
  `db_connection.py` — all targeted tables that do not exist
  (`attendance_sessions`, `students.course`). The first opened a camera at
  import; the second prompted for a password (SE-14, now moot).
- **Empty files:** `main.py`, `face_detect.py`, `static/js/script.js`.
- **Scratch scripts:** `camera_test.py`, `find_camera.py`, `test_webcam.py`,
  `test_db.py`, `mediapipe_test.py`.
- **`hello_flutter/`** (130 files) and **`haarcascade/`** (930 KB — sole
  reference was in `attendance_system.py`; the live pipeline uses MediaPipe).
- Generated artefacts removed from disk (already untracked).

**Kept deliberately:** `active_session.txt` and `selected_camera.txt` are
live runtime state read by `camera_utils.py`. Retiring them is PO-3.

### `48768a3` — Packaging, configuration, logging (PO-1, SE-8, PO-2, RE-5)

- **`pyproject.toml`** replaces `requirements.txt` (deleted). All direct
  dependencies pinned exactly. `requires-python = ">=3.11,<3.12"` — the upper
  bound is load-bearing, `mediapipe==0.10.14` has no 3.12 wheels.
- **`config/settings.py`** — pydantic-settings. Absorbs DB credentials,
  `SECRET_KEY`, dataset/trainer paths, `RECOGNITION_THRESHOLD` and log
  settings. Three call sites migrated (`app.py`, `recognize_face.py`,
  `setup_db.py`); the other two were deleted in the previous commit.
- **`setup_db.py`** previously hardcoded the database name in its DDL. With
  `DB_NAME` configurable that would create the wrong database, so it reads
  the configured name and validates it against a strict identifier allowlist
  first — MySQL cannot parameterise identifiers.
- **`config/logging_config.py`** — rotating file (`logs/app.log`) + console.
  Only entry points call `configure_logging()`; library modules just take a
  logger. 178 `print()` calls converted. Hot-path recognition diagnostics are
  `DEBUG`; set `LOG_LEVEL=DEBUG` in `.env` when diagnosing recognition.
- Debug artefacts deleted rather than converted (MA-10).

### `ab6a506` — Tests, lint gate, README; import-shadowing fix

- **Evaluators renamed** `test_accuracy.py` → `eval_accuracy.py`,
  `test_heldout_accuracy.py` → `eval_heldout_accuracy.py`. Both now import
  the threshold from config rather than restating 58.0.
- **`tests/` — 35 tests, 3 s.** Covers `write_model_atomically()` (the four
  RE-1 cases), the FS-2 skip-don't-abort path, `parse_dataset_folder()`,
  `config/settings.py`, and import shadowing.
- **`ruff check` clean.** `SIM103`/`SIM108` disabled with reasoning recorded
  in `pyproject.toml`; `E402` ignored per-file in `recognize_face.py`.
- **`README.md`** with setup, layout and the accuracy caveats stated up front.
- **The shadowing bug** described in §1.3.

---

## 4. Corrections and honesty notes

- **The Phase 0 handover said "~200 `print()` calls".** The actual count in
  live modules was **178** after dead-code deletion (211 across all files
  before it). Not a material difference, but the smaller number is the one
  that was converted.
- **"`ruff check` clean" is clean *against the configured ruleset*,** which
  is `E,W,F,I,B,UP,SIM,C4,G` minus `SIM103`/`SIM108`. It is not a claim that
  the code is idiomatic — `app.py` is still 1,480 lines with no blueprints
  (MA-1) and `recognize_face.generate_frames()` still nests seven deep
  (MA-12). Lint passing and code being good are different claims.
- **35 passing tests is a floor, not coverage.** They cover model persistence,
  folder parsing, configuration and import hygiene. **Nothing tests a route,
  a SQL query, recognition, or enrolment.** Do not read the green run as
  evidence that the application works; read it as evidence that the four
  things it covers work.
- **The held-out 60/60 still means what §6 of the Phase 0 handover said it
  means** — same-session split, no impostors, N=3. Phase 1 did not improve
  it and was not supposed to. Do not quote it without the caveats.

---

## 5. New finding

| ID | Sev | Finding |
|---|---|---|
| **FS-12** | **P1** | **A fatal enrolment failure is reported as success.** Every bare `sys.exit()` in `capture_dataset.py` exits with status **0** — missing `face_preprocessing`, missing arguments, empty student ID. `app.py:241` treats returncode 0 as success and runs an automatic retrain, so the operator sees a completed enrolment for a student who has no dataset. That feeds straight into FS-2 and FS-9. |

**Deliberately not fixed in Phase 1.** It changes enrolment control flow and
cannot be verified without a camera, and this phase was scoped to be
behaviour-neutral so the held-out number stayed comparable. The fix is
`sys.exit(1)` at each site plus a check of what `app.py` does with each code.
A comment marks it at the call site.

---

## 6. Phase 2 scope, with warnings

From [`todo.md`](todo.md) §5. All of it is open.

- [ ] **Hash passwords with bcrypt**, migrate existing rows, force a change of
      the seeded `admin`/`admin` (SE-1).
      ⚠️ This also fixes **SE-15** for free, and that is the point: the
      `password` columns are `utf8mb4_general_ci` and `app.py` compares **in
      SQL**, so today `admin` is accepted as `ADMIN`, `AdMiN` and `admin   `.
      bcrypt compares in Python and never lets collation decide
      authentication. If you keep any comparison in SQL, SE-15 survives the
      migration.
      ⚠️ `setup_db.py` seeds `admin`/`admin` in plaintext. Update it in the
      same change or the next `python setup_db.py` reintroduces the problem.
- [ ] **`@login_required` + `@role_required('admin')` on every route** — audit
      the full route table, do not spot-fix (SE-4, SE-5).
      ⚠️ The sidebar hides links; the routes do not enforce. A logged-in
      *instructor* can currently call `/delete_student` and `/update_admin`.
- [ ] **Convert destructive routes to POST** (SE-6) and **add CSRF** (SE-7).
      ⚠️ Changing `/delete_subject/<id>` and `/delete_instructor/<id>` to POST
      means editing the templates that link to them, not just the routes.
- [ ] **Path-traversal fix** (SE-3): validate `student_id`/`name` at input,
      derive the folder from a sanitised slug or surrogate key, and assert the
      resolved path is inside `dataset/` before any `rmtree`/`rename`.
      ⚠️ See §1.6 — changing the folder-naming scheme touches every existing
      dataset folder. Plan a migration or you will orphan all three students.
- [ ] **Protect `/video_feed`** and the camera-control routes (SE-2).
- [ ] **Generic error pages**; log details server-side only (SE-10, US-1).
      ⚠️ Several routes currently `return f"Database error: {error}", 500`.
      The logging work is already done — `logger.exception` is at each of
      those sites — so this is now mostly deleting the error text from the
      response.
- [ ] **Session hardening** (SE-11). `SECRET_KEY` from env is **already
      done**; `HTTPONLY`, `SAMESITE`, `PERMANENT_SESSION_LIFETIME` are not.
- [ ] **Login rate limiting / lockout** (SE-13).
- [ ] **`docs/data_privacy.md`** — consent, retention, erasure, access log,
      RA 10173 mapping (SE-9). An examiner will ask.
- [ ] **Verify:** every fixed vulnerability gets a regression test.
      ⚠️ There is no route-level test infrastructure yet. `app.test_client()`
      works — it was used to verify the shadowing fix — but any test that
      imports `app` pays the 9 s PE-4 cost, and there is no test database.
      Budget time for this; it is the real cost of "each vuln gets a test".

**Verify at end of Phase 2:** `ruff check` clean, `pytest` green, app still
boots, and `eval_heldout_accuracy.py` still reports 60/60.

---

## 7. Open decisions — still the user's, still unmade

From [`todo.md`](todo.md) §7. Bring measured evidence and options; do not
choose unilaterally.

1. **Recognition backend (blocks Phase 3).** LBPH at `neighbors=8` is
   ~18.3 MB/student, so the `FileStorage` ceiling returns somewhere between
   ≈31 and ≈100 students. Recommendation on the table: keep LBPH for thesis
   continuity *and* add an embedding backend behind the same interface as a
   documented comparison. **Undecided.**
2. **Deployment topology (blocks Phase 5).** `/capture_face` spawns an OpenCV
   window *on the server*, so browser and server must be the same machine.
   **Undecided.**
3. **Dataset recapture (blocks Phase 6).** Phase 6 is meaningless without
   multi-session data plus impostors. **Feasibility unknown.**
4. **CI (new, deferred 2026-08-08).** The user asked to handle this later.
   There is no git remote. `ruff` and `pytest` are configured and run
   locally; adding `.github/workflows/ci.yml` is ~15 minutes whenever there is
   somewhere to push. Note CI cannot run the evaluators — `dataset/` and
   `trainer/` are gitignored — so it would cover lint and unit tests only.

---

## 8. Suggested first move

```bash
git checkout phase-1-foundation
git checkout -b phase-2-security
```

Phase 2 changes authentication for every user of the system. Do it on a
branch, and read §6's warning about SE-15 before touching the login query —
the collation problem is invisible unless you look for it.

Phase 1 is on a branch and not merged. Merging it to `main` is the user's
call.
