# Handover — Phase 4b → Phase 5

**Sprint:** Phase 4b — the two Phase 4 items that did not land: FS-10's
attendance override screen, and integration tests against a real database
**Date:** 2026-08-15
**Status:** ✅ **Both items done. Phase 4 is now 12 of 12.** Two things from
`handover-phase-4.md` §7 remain open and are named in §7 below.
**Branch:** `phase-4-data-and-capture`, 2 further commits (14 total)
**Read with:** [`todo.md`](todo.md), [`lessons.md`](lessons.md) —
[L12](lessons.md) is new and came out of this sprint

---

## 1. Read this first — what will bite you

Everything in `handover-phase-4.md` §1 still applies and is **not** repeated
here. Re-read it: `dataset_staging/`, the start-up migration, ~~the database
stopping on its own~~, MariaDB-not-MySQL, non-strict `sql_mode`, two thresholds
on one scale, `db_cursor()` being one transaction per `with`, and never
putting a real student identifier in a test. All eight were re-verified today
and all eight still hold.

> ✅ **Corrected 2026-08-16: seven, not eight.** "The database stopping on its
> own" was never a defect — XAMPP's MySQL does not start after a Windows
> restart and the user starts it by hand. It was re-verified here in the sense
> that the *symptom* matched, which is exactly the failure [L15](lessons.md)
> describes. See [`handover-phase-5b.md`](handover-phase-5b.md) §1.6.

Four things are new.

1. **`tests/integration/` truncates tables. It is one environment variable
   away from doing that to the deployment.** The gate is three checks, in
   `tests/integration/conftest.py`:

   - `INTEGRATION_DB_NAME` unset → **skip**
   - equal to `settings.db_name` → **fail**, never skip
   - not matching `^test_[A-Za-z0-9_]+$` → **fail**

   All three were run and observed firing. **Do not soften the second one into
   a skip.** A skip reads as "not configured yet", which is exactly how
   somebody runs this against the deployment and believes it passed.

   ```bash
   INTEGRATION_DB_NAME=test_attendance_scratch \
     .venv/Scripts/python.exe -m pytest tests/integration/ -q   # expect 29 passed
   ```

2. **The fast suite must stay database-free.** The `lint-and-test` CI job has
   no server. Three tests in `tests/test_attendance_correction.py` assert
   `!= 400` and `not in (302, 401, 403)` rather than a status code, **on
   purpose**: past the validator the route looks the record up, and asserting
   `== 404` would have passed locally (where MariaDB is running) and failed in
   CI. If you add to that file, assert only what is decided before the route
   body runs.

3. **⚠️ CI runs MySQL 8.0; the deployment runs MariaDB 10.4.32.** The engine
   was the user's choice. The migrations are written to syntax both accept, so
   the job is a real test of them — but **a green tick there is evidence about
   MySQL 8**, and the local run against the real MariaDB is the authority. The
   workflow says this in a comment; do not quietly treat the two as
   interchangeable.

   The workflow pins the service's `sql_mode` to the deployment's non-strict
   setting, because MySQL 8 ships `STRICT_TRANS_TABLES` **on** and the
   deployment has it **off**. Left at the default, CI would catch coercions
   that the deployed system performs silently — a green tick describing a
   server nobody runs. `test_the_server_runs_the_sql_mode_the_deployment_runs`
   asserts it held, so a base-image change fails the run rather than weakening
   it.

4. **A source-level test that greps will match the comment explaining the
   code.** Two of my own FS-10 tests were vacuous for exactly this reason and
   the mutation run caught them. See [L12](lessons.md) — it is short and it is
   the most transferable thing in this sprint.

---

## 2. Verified current state

Measured on this branch at `72b5cd6`, today.

| Check | Result |
|---|---|
| `ruff check .` | **clean** |
| `pytest tests/ -q` | **647 passed, 29 skipped** (628 at the branch point) |
| `pytest tests/integration/ -q` (gated) | **29 passed** — 4 migrations, 16 schema, 9 FS-10 |
| `eval_heldout_accuracy.py` | **60/60, avg distance 34.95** — unchanged since Phase 0 |
| Schema version | `schema_migrations` at **006** — **this sprint added no migration** |
| Database | **MariaDB 10.4.32**, XAMPP, `attendancesystem_db` |
| `sql_mode` | `NO_ZERO_IN_DATE,NO_ZERO_DATE,NO_ENGINE_SUBSTITUTION` — **still no `STRICT_TRANS_TABLES`** |
| Live rows | students **5**, subjects **1**, attendance **0**, enrolments **0**, attendance_sessions **0**, attendance_audit **0**, instructors **0**, admin **1** |
| `git check-ignore dataset dataset_staging trainer` | 3 matches — SAFE |
| Python | 3.11.5 |

**The live row counts are byte-identical to the ones in
`handover-phase-4.md` §2.** The integration suite builds and drops its own
schema and never touches the configured one; that was asserted before and
after every run, because L6's incident was found by a row count an hour too
late.

**Still not verifiable by an agent:** live camera enrolment in a browser, and
the live recognition run. §6.

### Re-establish the baseline

```bash
git checkout phase-4-data-and-capture
.venv/Scripts/python.exe -m ruff check .
.venv/Scripts/python.exe -m pytest tests/ -q               # 647 passed, 29 skipped
INTEGRATION_DB_NAME=test_attendance_scratch \
  .venv/Scripts/python.exe -m pytest tests/integration/ -q # 29 passed
.venv/Scripts/python.exe eval_heldout_accuracy.py          # 60/60, avg 34.95
.venv/Scripts/python.exe scripts/migrate.py --status       # 0 pending
```

All five were run today in that order and produced those numbers.

---

## 3. What changed

Two commits.

| commit | what |
|---|---|
| ⭐ `aaedb40` | `tests/integration/` — the Phase 4 schema claims under test, plus a CI service container |
| ⭐ `72b5cd6` | **FS-10** — the attendance override screen and its audit trail |

### ⭐ The integration tests, and why the gate is the interesting part

`handover-phase-4.md` §5 was a table of eight findings recorded as fixed,
every one verified by scripts that were written into a session scratchpad and
then lost. The claims were true when made; nothing was stopping them becoming
false again.

**20 tests now cover them** — 16 for FS-3, FS-4, FS-5, FS-7, FS-8, RE-3 and
RE-4, plus 4 for PO-5. FS-10 adds 9 integration tests and 17 in the fast
suite, for 29 integration and 647 fast overall.

`infra/migrations.apply_all()` already took a `database` parameter, and its
docstring already said the integration tests were why. This is the caller it
was written for. `settings.db_name` turned out to be mutable at runtime and
`db_kwargs()` follows it, so the fixture redirects the whole application at a
scratch schema with `reset_pool()` and puts it back afterwards.

**Every test was proven non-vacuous before being trusted**, which is the only
reason to believe any of them:

| defect re-introduced | test that caught it |
|---|---|
| `save_attendance` joins `students`, not `enrolments` | FS-3 |
| the register covers every student in the database | FS-3 |
| Absent computed but never written | FS-4 |
| `derive_status` always returns Present | FS-8 |
| the duplicate insert raises instead of being absorbed | RE-3 |
| the dashboard counters are never read | FS-5 |

The two schema claims were proven differently and better: **a database built
from migration 001 alone has 0 foreign keys and no unique index on the
register key**, so the RE-3 and RE-4 tests cannot pass against the real
pre-Phase-4 schema. That is stronger evidence than a hand-made mutation
because it is the actual thing they are claiming to have changed.

### ⭐ FS-10 — the override screen

Migration 006 created `attendance_audit` last sprint and nothing wrote to it.

```
GET  /attendance/<int:attendance_id>/correct    the record, the form, the history
POST /attendance/<int:attendance_id>/correct    apply it
```

**`@authenticated`, not admin-only** — decided with the user. FS-10's premise
is that the *instructor* cannot correct a false negative, and they are the
person who watched it happen. What makes that safe to open up is that nothing
is anonymous.

**The audit trail is the feature; the screen is the interface to it.** The
status update and the audit insert are in **one `db_cursor` block**, which is
one transaction, so a failing audit insert takes the correction with it. The
integration test injects that failure and asserts the register still reads
Absent. A separate `ast` test asserts the two statements stay in one block —
because splitting them leaves both statements working and removes the property
silently, which is the regression a future refactor is most likely to cause.

Three validation decisions, each with a reason attached:

- **Status from an allowlist** of the three the system produces. `/reports`
  and `/export_excel` aggregate by exact string match, so a typed `"present"`
  would be a fourth category every counter ignores — FS-7 one layer down.
- **Reason required, length checked in Python.** ⚠️ `VARCHAR(255)` will not
  refuse a longer value on this server, it will truncate it and report
  success. A half-stored explanation of why a biometric determination was
  overridden is a corrupt record that looks fine. L11 applied to ordinary DML.
  A test ties the Python constant to migration 006 so they cannot drift.
- **Correcting to the status a record already has writes nothing** and says
  so.

⚠️ **`time_in` is never written, deliberately.** Correcting an Absent to
Present would otherwise have to invent an arrival time, and a fabricated
timestamp in a biometric register is worse than an obviously untouched field.
The docstring says this and a test enforces it, so the next reader does not
"fix" it.

---

## 4. Corrections

### 4.1 — to my own work, mid-sprint

Two FS-10 tests asserted on the route's **source text**, searching for
`FOR UPDATE` and `time_in`. Both were **vacuous**: the route's own docstring
and comments explain why it locks the row and why it leaves `time_in` alone,
so the strings are present whether or not the SQL is. `grep -c "FOR UPDATE"
app.py` returned **2** — one statement, one comment. Deleting the real SQL
left both tests green.

Both now extract the string constants from the `cursor.execute()` calls with
`ast` and assert on those. Written up as [L12](lessons.md), which generalises
it: **the better a decision is commented, the more certainly a text search for
it passes for the wrong reason.**

### 4.2 — ⚠️ to `72b5cd6`'s own commit message

It says FS-10 added "12 new integration tests". It added **9**. The fast-suite
figure in the same message (628 → 647, 19 new) is correct, and 17 of those 19
are FS-10's — the other 2 are the route-security table entries.

Counted per file afterwards, which is what I should have done before writing
the number rather than estimating it from the plan. The commit is immutable,
so the correction lives here and in `todo.md` §8 — a handover that quietly
disagrees with a commit is worse than either alone ([L9](lessons.md)).

Correct counts: **29 integration** (4 migrations, 16 schema, 9 FS-10) and
**647 fast, 29 skipped**.

### 4.3 — to `.github/workflows/ci.yml`'s own header comment

It said "there is no test database either, so nothing here exercises a SQL
query". That was true and is not any more. Corrected in place rather than left
to contradict the job below it.

### 4.4 — to `pyproject.toml`'s pytest comment

It carried a note saying "nothing under tests/ may import `recognize_face` or
`app`" because the import cost 9 s. PE-4 removed that in Phase 3 and
`tests/conftest.py` already documented the measurement and lifted the ban; the
`pyproject` note had been contradicting it since. Removed.

### 4.5 — `handover-phase-4.md` §7 said four items were outstanding

Two are now done (this sprint). Two remain and are **not** Phase 4 checklist
items — see §7.

---

## 5. What the tests now protect

| Finding | Protected by |
|---|---|
| FS-3 | an unenrolled student is neither recorded nor marked absent |
| FS-4 | Absent rows are written on session end and counted by `/reports` |
| FS-5 | the four dashboard counters are real aggregates |
| FS-7 | the attendance page offers the subjects that exist; an unknown id is refused |
| FS-8 | Late/Present derived from `subjects.time_in` **through a real TIME column**, which is the part a unit test with a hand-made value misses (MySQL returns TIME as a `timedelta`) |
| FS-10 | the correction, the audit row, and the rollback when the audit row cannot be written |
| RE-3 | the unique index exists; a second write is one row; ending twice does not overwrite a Present |
| RE-4 | student delete cascades; instructor delete sets `subjects.instructor_id` NULL; a bad `subject_id` is refused |
| PO-5 | 001–006 build a database from nothing; a second `apply_all` is a no-op |

---

## 6. Handed to the user — what only a person can do

Unchanged from `handover-phase-4.md` §6, and **none of it is closed by this
sprint**. The integration tests seed their own scratch data; they are not a
substitute for any of this.

1. **`enrolments` is still empty on the live database** (verified today, 0
   rows). Until somebody uses the Class List screen (Subjects → Class List),
   every session records nothing at all. **First thing to do before any live
   test.**
2. **A live browser enrolment run.** Camera permission, all nine stages at a
   real distance, cancel mid-way leaving nothing behind, a second tab getting
   409.
3. **A live recognition run**, to confirm FS-14 actually lets a real student
   mark attendance. Outstanding since `handover-phase-3b.md` §6.
4. **`tasks/notes.txt` is still uncommitted.** Five handovers have now left it
   alone because it is the user's.

---

## 7. What is NOT done

- ⚠️ **The `dataset/{student_id}` folder migration** (todo.md §7.5). Agreed
  with the user, deliberately sequenced after the data model, not reached in
  Phase 4, and out of scope for 4b by the user's choice. It needs
  `security/paths.py`, `train_model.parse_dataset_folder`, `labels.txt`, both
  `eval_*.py`, a rename on disk, and a retrain with held-out re-verified at
  60/60.
  ⚠️ **`labels.txt` stores `{id}_{name}` and the recognition overlay reads the
  display name from it.** Moving to id-only folders means the display name has
  to come from the database — plan that before starting. Note that
  `save_attendance()` already reads the name from `students`, so the pattern
  exists.
- ⚠️ **`capture_dataset.py` and the old routes still exist.** 1,586 lines, plus
  `/capture_face`, `/recapture_face` and `config/exit_codes.py`. **Still
  blocked on §6.2** — until a person has driven the browser flow they are the
  only proven enrolment path. CO-2 and MA-2 do not close until they go.

Also open and unchanged: **PE-8** (pandas given a raw DBAPI2 connection, and
the export is still not streamed — FS-11's remaining half), **MA-1**
(`app.py` is now ~2,500 lines; blueprints are Phase 5), **PO-3**
(`selected_camera.txt`; `active_session.txt` is dead and can go), **CO-3**
(reduced, not closed — browser enrolment is localhost-only without TLS).

---

## 8. Open decisions

**Nothing in the remaining work is blocked on a decision.** Two were taken
this sprint, both with the user:

1. ✅ **Attendance corrections are `@authenticated`**, not admin-only —
   instructors and admins both. The audit trail is what makes it safe.
2. ✅ **CI's service container is MySQL 8.0**, while the deployment is MariaDB
   10.4.32. Recorded in §1.3 with what it costs.

Everything in `handover-phase-4.md` §8 still stands.

---

## 9. Suggested first move

Phase 5 is the web layer, and it is smaller than its original estimate — the
capture rewrite, the subject dropdown and the export filters all landed early.
What remains is **HTTPS** (a prerequisite for remote browser enrolment, not a
polish task — see todo.md §7 Q2 on the SAN requirement and `mkcert`),
**blueprints (MA-1)**, flash messages, the live recognised-students feed
(US-3), the `confirm()` quoting bug (US-5), the accessibility pass (US-6) and
the responsive layout (US-7).

**MA-1 is the one to sequence carefully.** `app.py` is ~2,500 lines and the
integration tests now drive it through `app.test_client()`, so they will catch
a blueprint split that changes behaviour — which makes this a much safer
refactor than it would have been last week. Run them before and after, and
note that `tests/test_route_security.py` walks `app.url_map`, so a route that
loses its marker during the move fails immediately rather than being served.

Before any live testing, **populate a class list** (§6.1). It is still the
single thing standing between this system and a session that records
something.

The user is running these phases in separate sessions and asked for this
document to bridge them. Assume the next reader has no memory of this one.
