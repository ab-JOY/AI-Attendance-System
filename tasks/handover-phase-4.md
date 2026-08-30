# Handover — Phase 4 → the rest of Phase 4, then Phase 5

**Sprint:** Phase 4, data model & functional gaps, **plus browser enrolment**
(pulled forward from Phase 5 at the user's request)
**Date:** 2026-08-11
**Status:** ◧ **Substantially done, four items outstanding — §7 lists them.**
**Branch:** `phase-4-data-and-capture`, 12 commits, branched from `main`
**Read with:** [`todo.md`](todo.md), [`lessons.md`](lessons.md) (L10 and L11 are
new and both came out of this sprint)

---

## 1. Read this first — what will bite you

1. **`dataset/`, `dataset_staging/` and `trainer/` are gitignored and must stay
   that way.** The check is now **three** paths, not two:
   ```bash
   [ "$(git check-ignore dataset dataset_staging trainer | wc -l)" -eq 3 ] && echo SAFE || echo STOP
   git diff --cached --name-only | grep -qE '^(dataset|dataset_staging|trainer)/' \
     && echo "STOP - sensitive data staged" || echo "SAFE"
   ```
   `dataset_staging/` is new: browser enrolment writes partial captures there.
   A partial capture is no less biometric data than a finished one.

2. **`python app.py` now migrates a stale schema by itself.** The system is
   deployed by copying it to another machine, and the tester's database is not
   this one. `check_database_on_startup()` runs from the `__main__` block and
   applies anything pending, because `CREATE TABLE IF NOT EXISTS` is a no-op on
   an existing table - so without it a newer copy runs against an older
   database and fails on whichever page first touches a missing column.

   ⚠️ **It is called from `__main__` only, never at import**, and a test
   enforces that with `ast`. The suite imports `app` and CI has no database;
   a connection at import would make every test depend on a running server.

   ⚠️ **Single process only.** `AUTO_MIGRATE=false` turns it off; nothing here
   takes a lock, so two workers starting together would race. Use
   `scripts/migrate.py` anywhere that is not one dev server.

   A failed check **stops the application** rather than serving a half-migrated
   schema. Each cause names its own fix: "Start MySQL/MariaDB…", "Run: python
   setup_db.py", or the migration that failed.

3. ~~**The database server stops on its own.**~~ ✅ **Corrected 2026-08-16 —
   this was never a defect.** XAMPP's MySQL does not start automatically after
   a Windows restart, and the user starts it by hand; every occurrence recorded
   here and in later handovers was a session that began after a reboot, which
   is also why no shutdown was ever logged. See
   [`handover-phase-5b.md`](handover-phase-5b.md) §1.6 and [L15](lessons.md).
   **Do not carry this forward.** The original note follows.

   It was found down mid-sprint —
   no `mysqld` process, nothing listening on 3306 on either IPv4 or IPv6 —
   despite `C:\xampp\mysql\data\mysql_error.log` recording a clean start at
   20:04:15 with no shutdown logged. Restarted with:
   ```
   mysqld --defaults-file=C:\xampp\mysql\bin\my.ini --standalone
   ```
   It then died **again** later in the same session, and the error log records
   **no shutdown and no error** either time — so it is being killed abruptly
   rather than stopping. One likely contributor: a `mysqld` started from a
   terminal or tool session can be killed when that session ends.

   **Start it from the XAMPP Control Panel**, or install it as a Windows
   service so it is independent of any shell:
   ```
   C:\xampp\mysql\bin\mysqld --install
   net start mysql
   ```
   **Check the server is up before concluding anything is broken** — a stopped
   server makes `/enrol`, `/dashboard` and every other page 500, which looks
   exactly like application breakage and is not.

4. **It is MariaDB 10.4.32, not MySQL**, under XAMPP's `C:/xampp/mysql`
   directory. Every document in the repository says MySQL, and the XAMPP panel
   labels it MySQL, which is why nobody noticed. It matters twice: migrations
   are written to the syntax **both** MariaDB 10.4 and MySQL 8 accept (no
   `RENAME COLUMN`, no functional indexes, no `utf8mb4_0900_*`), and CI's
   service container should match the local engine.

5. **`sql_mode` has no `STRICT_TRANS_TABLES`.** This is the single most
   dangerous fact in this document. Conversions that you expect to fail
   silently succeed with a wrong value: `NULL` becomes `0`, an unparseable
   time becomes midnight, an over-long string is truncated. It cost two wrong
   migration drafts — see [L11](lessons.md) and §4.

6. **Two thresholds on one metric scale is this project's signature bug**, and
   it has now happened three times (L2, SE-12, and FS-14 this sprint). Before
   adding any second threshold on LBPH distance, read [L10](lessons.md).

7. **`db_cursor()` is one transaction per `with`.** A loop of `with
   db_cursor(...)` blocks is N transactions. The Absent write in
   `/end-attendance` is a single `executemany` inside one block for exactly
   this reason.

8. **Never put a real student identifier in a test.** [L6](lessons.md).
   Unchanged, and it now also applies to the enrolment routes, which can write
   into `dataset/`.

---

## 2. Verified current state

Measured on this branch at `7f6d1bc`.

| Check | Result |
|---|---|
| `ruff check .` | **clean** |
| `pytest tests/` | **628 passed** (450 at the branch point) |
| `eval_heldout_accuracy.py` | **60/60, avg distance 34.95** — unchanged since Phase 0 |
| Schema version | `schema_migrations` at **006** |
| Database | **MariaDB 10.4.32**, XAMPP, `attendancesystem_db` |
| Live rows | students **5**, subjects **1**, attendance **0**, enrolments **0**, instructors **0**, admin **1** |
| Model | `trainer/trainer.yml`, 52.4 MB, 3 identities |
| `git check-ignore dataset dataset_staging trainer` | 3 matches — SAFE |
| Python | 3.11.5 |

**Still not verifiable by an agent:** live camera enrolment in a browser, the
live recognition run, and therefore whether the FS-14 fix actually lets a real
student mark attendance. §6.

### Re-establish the baseline

```bash
git checkout phase-4-data-and-capture
.venv/Scripts/python.exe -m ruff check .
.venv/Scripts/python.exe -m pytest tests/ -q          # expect 628 passed
.venv/Scripts/python.exe eval_heldout_accuracy.py     # expect 60/60, avg 34.95
.venv/Scripts/python.exe scripts/migrate.py --status  # expect 0 pending
```

---

## 3. What changed

Ten commits. The three marked ⭐ will affect your work most.

| commit | what |
|---|---|
| `fb7f13d` | Migrations as the single schema source (PO-5) |
| `0901150` | `vision/pose.py` extracted from `capture_dataset.py` |
| ⭐ `e3533c8` | **FS-14, FS-15** — the two bugs the user reported live |
| `9259011` | `vision/enrolment.py` — the nine-stage capture protocol |
| `718c369` | `infra/dataset_store.py` — staging and atomic promotion |
| ⭐ `8acfafd` | **Browser enrolment** — five routes, capture page, upload hardening |
| `3fe4f47` | Migrations 002–006 written (⚠️ committed unapplied — see §4) |
| ⭐ `82eedaa` | Migration 004 made safe, applied to the live database, L11 |
| `a5e0e7e` | Attendance on the new schema — FS-3, FS-4, FS-5, FS-8, RE-3, FS-7 |
| `cd97cbe` | Class-list screen so `enrolments` can be populated |

### ⭐ FS-14 — a recognised student could never be marked present

Reported from a live run: `Verifying 20/20 100% (56.1)` for ever. Twenty of
twenty frames agreeing unanimously on the right student, nothing recorded,
nothing logged.

`RECOGNITION_THRESHOLD` is 58.0 (a frame votes) and
`TrackConfig.confirmation_confidence` was 52.0 (the window average must beat
it). Anything settling between them was recognised on every frame and
unmarkable for ever. **A test asserted the band deliberately**, so it read as a
design rather than a defect.

**The evaluator could not have caught it** — it scores same-session crops at
avg 34.95 and never reaches the band. Only a real face in a real room does.

Measured before changing it (`docs/benchmarks.md` §4a): 750 impostor images
from 50 unenrolled Georgia Tech subjects score **62.2 at the closest**, lowest
subject mean 69.9; genuine same-session crops top out at 34.9. Nothing lies
between 52 and 58 in either population, so the bar now derives from the
threshold and a test fails if a gap reopens.

The silence mattered more than the gap: `can_confirm()` returned a bare `False`
for five different reasons and now goes through `confirmation_blocker()`, which
names the unmet condition, logs once per stuck track, and shows the operator
something actionable.

### ⭐ Browser enrolment

`/capture_face` spawned `capture_dataset.py` as a subprocess, which opened an
OpenCV window **on the server desktop**. Five routes replace it:

```
POST /enrol         renders the capture page
POST /enrol/start   opens the session and the staging folder
POST /enrol/frame   one JPEG in, one verdict out, ~5/second
POST /enrol/finish  promote the folder, THEN write the student row
POST /enrol/cancel  discard the staging folder
```

**The browser supplies a camera and a screen. Every gate stays on the server**,
through `vision/enrolment.py`, on the same code the capture window used. A
check added to the page would recreate MA-4.

⚠️ **Frames are judged at exactly 1920×1080** and `offer_frame()` refuses
anything else. Two thresholds are absolute pixels (`CENTER_TOLERANCE = 180`,
`min_eye_distance_px`), and a browser has no fixed frame size.
`infra/uploads.py` crops to aspect and scales — **never squashes**, because
stretching 4:3 to 16:9 widens a face by a third and fails the aspect-ratio and
eye-separation gates for a reason that has nothing to do with the person.

⚠️ **`getUserMedia` needs a secure context.** `http://localhost` qualifies, so
this works **on the server machine only**. Enrolling from another machine needs
TLS, which was explicitly out of scope. **CO-3 is reduced, not closed** — and
CO-1 and PO-6 survive too, because `camera_utils.py` still opens the classroom
camera with `cv2.CAP_DSHOW` for recognition. Only CO-2 and MA-2 die outright,
and only once `capture_dataset.py` is actually deleted (§7).

### ⭐ Migration 004, and why `82eedaa` exists

`3fe4f47` committed the migrations **unapplied**, because the database went
down. Verifying them afterwards found 004 was dangerous. See §4.

---

## 4. Corrections

**Check these against your plan before building anything.**

### 4.1 — ⚠️ to `3fe4f47`'s own commit message

That message says `MODIFY subject_id INT NOT NULL` would "fail loudly rather
than deleting or guessing". **It does not.** With no `STRICT_TRANS_TABLES` it
silently converts NULL to **0**. Adding the foreign key first did not catch it
either: with `foreign_key_checks = 1` the `ALTER TABLE` rebuild **did not
re-validate the constraint**, and the run reported *success* leaving a row
pointing at `subject_id 0`.

By then `DROP COLUMN subject_code` had committed — DDL cannot roll back — so
the column needed to repair it was gone. Fixed in `82eedaa` with an explicit
`CASE`/subquery gate placed before anything destructive. Written up as
[L11](lessons.md).

### 4.2 — to `handover-phase-3b.md` §2

It records the suite as **444 passed**. It was **450** at this branch point;
the two commits after that handover added tests. Not important, but the number
you compare against should be 450.

### 4.3 — to `todo.md` §5, Phase 4 checklist

"Fix the `edit_instructor.html` template name (FS-6)" **was already done in
Phase 2** ([app.py:1222](../app.py)). The item was stale and is struck.

### 4.4 — the dataset is not three students

`students` holds **5** rows. `12345` and `test-id` are both dangling test rows
with no usable dataset, and `dataset/12345_test student/` is an empty folder —
FS-9 caught in the act, from a capture the log records as 0 of 100 images.
They are data, so they were left alone; deleting them is the user's call.

---

## 5. What the data model now does

Verified by driving the real routes against the deployed database with a seeded
subject, then removing every trace and asserting the row counts back (L6).

| Finding | Before | Now |
|---|---|---|
| FS-3 | every student in the database marked absent for every subject | register scoped to `enrolments`; an unenrolled student is refused and does not appear |
| FS-4 | Absent computed in memory, never stored; `/reports` always 0 | persisted on session end, one `executemany` in one transaction |
| FS-8 | everything "Present" — `time_in` was VARCHAR vs TIME | **Late derived**; the first non-Present this system has recorded |
| RE-3 | read-then-write TOCTOU | `UNIQUE` + `ON DUPLICATE KEY UPDATE`; the check is deleted, not kept |
| RE-4 | no foreign keys at all | 8 foreign keys |
| FS-5 | four hardcoded `0` in the template | real aggregates; the route had no database access at all before |
| FS-7 | subject typed by hand, twice, unchecked | dropdowns bound to `subjects` |
| FS-11 | export ignored filters, fixed filename at repo root | filters honoured, timestamped file in `reports/` |

⚠️ **`enrolments` is empty on the live database.** Until somebody uses the new
Class List screen (Subjects → Class List), every session will record nothing
at all. That is the first thing to do before any live test.

---

## 6. Handed to the user — what only a person can do

1. **A live browser enrolment run.** Camera permission, all nine stages at a
   real distance, cancel mid-way leaving nothing behind, a second tab getting
   409. Composited stills got as far as `Hold straight: 1/6` but mostly read
   as `detected DOWN` — expected, because they are *aligned* crops with the eye
   line pinned and the STRAIGHT pitch band is 0.015 of frame height wide.
   **Composited stills are not a substitute for a camera.**
2. **A live recognition run**, to confirm FS-14 actually lets a real student
   mark attendance. This is the run `handover-phase-3b.md` §6 also asked for
   and it is still outstanding.
3. **Populate a class list** before either — see §5.
4. **`tasks/notes.txt` is still uncommitted.** Four handovers have now left it
   alone because it is the user's.

---

## 7. What is NOT done

**Be honest with yourself about this list before planning.** Four items from
the approved plan did not land.

- ⚠️ **FS-10, the attendance override screen.** Migration 006 created
  `attendance_audit` and nothing writes to it. The table exists, the screen
  does not. This is the largest gap.
- ⚠️ **Integration tests against a real database.** Everything in §5 was
  verified by ad-hoc end-to-end scripts, not by `pytest`. **The suite still has
  no database and CI has no service container**, so none of the schema
  behaviour is regression-protected. The scripts are in the session scratchpad
  and are gone; they need rewriting as `tests/integration/`, gated on an env
  var and pointed at a scratch schema — never the configured one (L6).
- ⚠️ **The `dataset/{student_id}` folder migration.** Decided with the user and
  deliberately reordered after the data model, then not reached. Still worth
  doing: it kills the rename coupling in `todo.md` §7.5 permanently. It needs
  `security/paths.py`, `train_model.parse_dataset_folder`, `labels.txt`, both
  `eval_*.py`, and a retrain with held-out re-verified at 60/60.
  ⚠️ **`labels.txt` currently stores `{id}_{name}`, and the overlay reads the
  name from it.** Moving to id-only folders means the display name has to come
  from the database — plan that before starting.
- ⚠️ **`capture_dataset.py` and the old routes still exist.** 1,586 lines,
  plus `/capture_face`, `/recapture_face` and `config/exit_codes.py`. They
  still work. **Delete them only once a person has driven the browser flow**,
  because until then they are the only working enrolment path. CO-2 and MA-2
  do not close until they go.

Also still open and unchanged: **PE-8** (pandas given a raw DBAPI2 connection),
**MA-1** (`app.py` is now ~2,300 lines; blueprints are Phase 5), **PO-3**
(`selected_camera.txt`; `active_session.txt` is dead and can go).

---

## 8. Open decisions

Settled this sprint, all with the user:

1. ✅ **`attendance` keys on `subject_id`**, not `subject_code` — a subjects
   row is a section offering and two sections share a code.
2. ✅ **Dataset folders become `dataset/{student_id}`** — agreed, not yet done
   (§7).
3. ✅ **No TLS this sprint** — browser enrolment is localhost-only, CO-3
   reduced rather than closed.
4. ✅ **Browser capture before the data model** — done in that order.
5. ✅ **The confirmation bar derives from `RECOGNITION_THRESHOLD`** (FS-14),
   with the impostor measurement as justification.

Nothing in the remaining work is blocked on a decision.

---

## 9. Suggested first move

```bash
git checkout phase-4-data-and-capture
.venv/Scripts/python.exe -m ruff check .
.venv/Scripts/python.exe -m pytest tests/ -q          # expect 623
.venv/Scripts/python.exe scripts/migrate.py --status  # expect 0 pending
```

Then **write the integration tests before writing any more schema code**. §5
is a table of claims backed by scripts that no longer exist; every one of them
is a regression waiting to happen, and this project's whole history is defects
that looked like working code.

After that, FS-10, then the folder migration, then delete `capture_dataset.py`
once the user has confirmed the browser flow works.

The user is running these phases in separate sessions and asked for this
document to bridge them. Assume the next reader has no memory of this one.
