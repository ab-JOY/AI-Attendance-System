# Handover — Phase 2 → Phase 3

**Sprint:** Phase 2, Security
**Date:** 2026-08-08
**Status:** Complete. Every Phase 2 item done, plus the CI that Phase 1 deferred.
**Branch:** `phase-2-security`, 4 commits, branched from `main` at `26691d2`
**Read with:** [`todo.md`](todo.md) (audit + full plan), [`lessons.md`](lessons.md)

Phase 1's handover is history now. This document supersedes it; where the two
disagree, §4 below says which is right.

---

## 1. Read this first — seven things that will bite you

1. **`dataset/` and `trainer/` are gitignored and must stay that way.**
   Face images of three identifiable students and the biometric templates
   derived from them. Both checks were run today and both work:
   ```bash
   # 1. Ignore rules still in place? (expects exactly 2 matches)
   [ "$(git check-ignore dataset trainer | wc -l)" -eq 2 ] && echo SAFE || echo STOP

   # 2. After staging, before committing - anything sensitive slip in?
   git diff --cached --name-only | grep -qE '^(dataset|trainer)/' \
     && echo "STOP - sensitive data staged" || echo "SAFE"
   ```
   Never use `git check-ignore -q` with more than one path.

2. **A negative test run deleted two live database rows during this sprint.**
   Running the new route tests against `main` — to prove they caught SE-5 —
   executed `POST /delete_student/<a real student ID>` and
   `GET /delete_subject/1` for real, because on `main` neither route has the
   guard the test was asserting. Both rows were restored (the student from
   `trainer/labels.txt`, the subject from `todo.md`'s Phase 0 note) and the
   counts in §2 are post-restoration. The 100 face images survived by luck,
   not design. Every identifier in `tests/test_route_security.py` is now fake,
   though `/delete_subject/1` shows that numeric IDs have no fake form — a
   scratch database is the real answer. **A worktree isolates files, not the
   database.** Read [`lessons.md` L6](lessons.md) before running anything
   against old code.

3. **The database now differs from what a pre-Phase-2 checkout expects.**
   `admin` and `instructors` have a `must_change_password` column and their
   `password` columns hold bcrypt hashes. **Checking out `main` and running it
   against this database will fail every login** — `main` compares plaintext
   in SQL against a `$2b$...` string. That is a one-way door for the database,
   not for the code: `git checkout phase-2-security` restores working logins.

4. **`recognize_face.py` still loads a 55 MB model at import** — ~11.6 s
   measured today (PE-4, Phase 3). Consequences you inherit:
   - **`tests/test_route_security.py` is the only file under `tests/` allowed
     to import `app`.** It pays the cost once in a session-scoped fixture and
     is marked `slow`. `tests/conftest.py` states the rule and the exception.
   - `app.py` must call `configure_logging()` **before** importing
     `recognize_face`, or that module's startup messages are discarded. Two
     imports carry `# noqa: E402` for this. Remove them when PE-4 is fixed.

5. **`LBPH_PARAMS` is still not configurable, on purpose**, and
   `RECOGNITION_THRESHOLD` is still 58.0 with `tests/test_settings.py`
   asserting it. Changing it needs Phase 6 calibration, not a judgement call
   ([`lessons.md` L2](lessons.md)).

6. **Renaming a student still breaks their dataset folder.** The scheme is
   still `dataset/{student_id}_{name}`. Phase 2 closed the *traversal* (SE-3)
   but the user decided against the surrogate key, so this coupling survives
   deliberately — it is recorded in `todo.md` §7.5, not overlooked. `app.py`
   now fails loudly instead of deleting the wrong tree, which is the safe
   failure, but it is still a failure.

7. **A new route with no access-control marker is refused, not served.**
   `security/access.py` denies by default. If you add a route in Phase 3 and
   it returns 403 with a log line about an "unclassified endpoint", you forgot
   `@public` / `@authenticated` / `@role_required`. `test_route_security.py`
   fails on it too.

---

## 2. Verified current state

Every number below was measured today.

| Check | Result |
|---|---|
| `ruff check .` | **clean** |
| `pytest tests/` | **205 passed in 57.9 s** (43 before this sprint) |
| `pytest -m "not slow"` | **121 passed, 84 deselected, in 15.7 s** |
| `eval_heldout_accuracy.py` | **60/60, avg distance 34.95** — identical to Phase 0 and Phase 1 |
| End-to-end against live MySQL | **33/33 checks** — see §5 |
| New route tests against `main` | **48 of 74 fail** — see §5 |
| `import app` | 11.6 s, boots clean |
| Route table | **35 routes**: 4 public, 9 authenticated, 22 admin-only, 0 unclassified |
| `git check-ignore dataset trainer` | 2 matches — SAFE |
| Python | 3.11.5 |
| Model | `trainer/trainer.yml`, 55 MB, 3 identities |
| Database | 4 students, 1 subject (`CS401`), 1 admin (bcrypt, flagged), 0 instructors, 0 attendance rows — both restored rows verified present |

**Still cannot be verified by an agent:** live camera capture, live
recognition, and the browser UI rendering. Everything upstream of the
hardware is verified, including real HTTP requests against the live database
for every access-control rule.

### Re-establish the baseline before you start

```bash
git checkout phase-2-security
.venv/Scripts/python.exe -m ruff check .              # expect: All checks passed
.venv/Scripts/python.exe -m pytest tests/ -q          # expect: 205 passed
.venv/Scripts/python.exe eval_heldout_accuracy.py     # expect 60/60, avg 34.95
git status --porcelain                                # expect empty
```

If the held-out run does not reproduce 60/60, stop and diagnose. Phase 2 did
not touch the recognition pipeline, so any movement means something else did.

---

## 3. What changed

Four commits.

### `50687dc` — `security/` package

Primitives first, wired in later, so each is testable without the 11.6 s
`import app`.

- **`passwords.py`** — bcrypt, and verification **in Python**. This is the
  part to understand: passwords used to be compared inside the SQL query, so
  MySQL decided the match under a `utf8mb4_general_ci` collation that is
  case-insensitive and PAD SPACE. Hashing alone would not have fixed that; a
  hash compared in SQL has the same defect. `verify_password()` also returns
  False rather than raising on a plaintext row — `bcrypt.checkpw` raises
  `ValueError("Invalid salt")` on one, and an unhandled exception in `login()`
  would have turned a security fix into an outage on any unmigrated database.
- **`paths.py`** — the single authority on dataset paths. Allowlist, then
  resolve and prove the result is a direct child of `dataset/`. **Underscores
  are barred from student IDs** and that is load-bearing:
  `parse_dataset_folder()` and `load_model_and_labels()` both split the folder
  name on the *first* underscore, so an ID containing one would silently move
  part of the ID into the name. Names accept non-ASCII letters and
  apostrophes — `Peña` and `O'Brien` are names.
- **`rate_limit.py`** — in-memory, clock injected for testing.
- **`access.py`** — the default-deny hook and the three declaration
  decorators.

### `6d1a34a` — bcrypt storage and the forced change (SE-1)

- `must_change_password` on both credential tables, in `schema.sql` **and**
  `setup_db.py`.
- `setup_db.ensure_column()`, because `CREATE TABLE IF NOT EXISTS` is a no-op
  on an existing table and MySQL has no `ADD COLUMN IF NOT EXISTS`.
- `scripts/migrate_passwords.py`, idempotent, with `--dry-run`. **Applied to
  the live database today.**

### `03b9adb` — the application (SE-2..SE-11, SE-16, SE-17, FS-6, FS-13)

The whole route table. `if 'user' not in session` disappears from 28
handlers; the hook does it once, and refuses anything unmarked.

Also here, found while working and fixed rather than deferred, because a
route with authentication added that still fails for the legitimate user is
not meaningfully secured:

- **SE-16** — `/change_password` updated `admin WHERE id=1` whoever was signed
  in, and instructors could not change their own password at all. That blocks
  the forced-change flow, which has to work for both roles.
- **SE-17** — both instructor tables rendered `{{ instructor.password }}`.
- **FS-13** — the Delete button on `/students` was a GET link to a POST-only
  route: 405, did nothing.
- **FS-6** — `/edit_instructor` rendered a template that does not exist. Its
  template also passed the wrong `url_for` argument and asked for a column the
  schema has never had.

### `8d7dcee` — tests, CI, privacy document

74 route tests, `.github/workflows/ci.yml`, `docs/data_privacy.md`.

---

## 4. Corrections to the Phase 1 handover

1. **§7.4 was wrong: a git remote exists.** `origin` →
   `github.com/ab-JOY/AI-Attendance-System`, with `main` already pushed —
   46 files, no `dataset/` or `trainer/`, verified with `git ls-tree`. CI was
   deferred for want of a remote, so it landed here instead.
2. **§8 said `fix-fs-12` was unmerged.** Correct at the time; it was
   fast-forwarded into `main` at the start of this sprint.
3. **"35 tests is a floor, not coverage" is still true, with one change.**
   Phase 1 said *"nothing tests a route"*. Something does now — 74 tests
   drive real requests. But read what they cover: **denials**. Not one
   asserts that a route does the right thing when it is *allowed* to. There
   is still no test for a SQL query, for recognition, or for enrolment.

---

## 5. Evidence, and what it is worth

**205 tests pass** — but the number that matters is that **48 of the 74 route
tests fail against `main`**:

| Group | Failing on `main` | Finding |
|---|---:|---|
| Unauthenticated access | 9 | SE-2, SE-4 |
| Role enforcement | 23 | SE-5 |
| Destructive routes over GET | 2 | SE-6 |
| CSRF | 5 | SE-7 |
| Forced password change | 3 | SE-1 |
| Session hardening | 1 | SE-11 |
| Route-table audit | 1 | SE-4 |
| Raw exceptions in responses | 1 | SE-10 |
| SE-3 source guards (separate file) | 3 | SE-3 |

A regression test that passes before the fix proves nothing, so each of these
was run both ways ([`lessons.md` L4](lessons.md)). One of them passed for the
wrong reason at first: `POST /add_subject` with an empty body already returned
400 on `main`, from a missing form key, so a status-code-only assertion made
unprotected code look protected. That is why `app.py` has a dedicated
`CSRFError` handler — it gives the test something specific to check.

**33/33 end-to-end checks** against the live MySQL and real requests, covering
the migration, the forced change, the SE-15 demonstration run in reverse
(`ADMIN`, `AdMiN`, `admin   ` now all rejected), instructor role boundaries,
SE-16, `/video_feed` after logout, a stale CSRF token, path traversal through
the add-student form, and the login lockout.

**Held-out accuracy unchanged at 60/60, avg 34.95.** That is the proof the
security phase was recognition-neutral. It still means what it always meant:
same-session split, no impostors, N=3. Do not quote it without the caveats in
`docs/walkthrough.md` §4.

---

## 6. Phase 3 scope, with warnings

From [`todo.md`](todo.md) §5. All of it is open.

- [ ] **Decide the recognition backend** (§7 Q1) — still the user's decision,
      still unmade. It blocks the rest of this phase.
- [ ] **Extract `vision/validation.py`** as the single face-geometry gate
      (MA-4).
      ⚠️ The two copies use *different* thresholds, so enrolment and
      recognition currently accept different populations of faces. Unifying
      them **changes which faces are accepted**. Re-run
      `eval_heldout_accuracy.py` before and after and record both numbers —
      this is the first Phase 3 task that can move that figure.
- [ ] **Extract the per-track state machine** from `generate_frames()` (MA-12).
- [ ] **`RecognitionSession` with an `RLock`**, one session at a time (RE-2).
      ⚠️ `/video_feed` is now `@authenticated`, so a second tab still gets a
      second generator — authentication does not serialise access. RE-2 is
      untouched by Phase 2.
- [ ] **Load the model once at session start** (PE-4).
      ⚠️ This is the one that pays back everywhere: it removes the `# noqa:
      E402` pair in `app.py`, lets `tests/conftest.py` drop its exception, and
      takes ~11.6 s off `import app`.
- [ ] **Throttle `CameraReader`** (PE-6).
- [ ] **Training as a background job** with a status endpoint (PE-5, US-2).
      ⚠️ The status endpoint needs `@authenticated` **and** `@json_api`, or the
      polling JavaScript gets an HTML redirect and parses it as JSON.
      ⚠️ `/train_model` is admin-only. If instructors should be able to
      trigger a retrain, that is a decision, not an oversight.
- [ ] **MySQL connection pooling** (PE-7).
- [ ] **Strengthen liveness** (SE-12) — still open, and the only Security
      finding Phase 2 did not touch. `docs/data_privacy.md` §7.6 states the
      residual risk honestly; keep it that way.
- [ ] **Verify:** before/after model size, cold start and FPS in
      `docs/benchmarks.md`.

**Verify at end of Phase 3:** `ruff check` clean, `pytest` green, app still
boots, and `eval_heldout_accuracy.py` reports a number you have explained. If
MA-4 lands, that number may legitimately change — say so and show both.

---

## 7. Open decisions — still the user's

From [`todo.md`](todo.md) §7.

1. **Recognition backend (blocks Phase 3).** Unchanged and undecided.
   Recommendation on the table: keep LBPH for thesis continuity *and* add an
   embedding backend behind the same interface as a documented comparison.
2. **Deployment topology (blocks Phase 5).** Unchanged and undecided.
   ⚠️ Phase 2 added a second reason this matters: the system runs over plain
   HTTP, so `SESSION_COOKIE_SECURE` defaults to `false`. The moment the
   browser is on another machine, credentials and the camera stream cross the
   network in clear text. Answering Q2 with "yes, remote" means TLS is a
   prerequisite, not a nicety.
3. **Dataset recapture (blocks Phase 6).** Feasibility still unknown.

**Decided during this sprint, no longer open:** CI (added), and the SE-3
folder scheme (sanitise + containment, not a surrogate key).

---

## 8. Suggested first move

```bash
git checkout main
git merge --ff-only phase-2-security
git push origin main            # CI will run for the first time
git checkout -b phase-3-recognition
```

Then answer §7 Q1 before writing any Phase 3 code — everything else in that
phase depends on it.

One operator action is outstanding and takes a minute: **sign in as
`admin`/`admin` and set a real password.** The account is flagged, so it can
do nothing else until you do, and the seeded credential is published in the
README.
