# Handover — Phase 5b → Phase 6

**Sprint:** Phase 5b — clearing the Phase 5 defect register before the UAT
**Date:** 2026-08-16
**Status:** ✅ **All fourteen findings in [`review-phase-5.md`](review-phase-5.md)
resolved.** No new feature work. The UAT in `handover-phase-5.md` §6 is still
the critical path and is now runnable — two of the fourteen made it
meaningless.
**Branch:** `phase-5-web-and-ux`, 6 commits on top of `81ecad9`
**Read with:** [`review-phase-5.md`](review-phase-5.md) (has a resolution table
at the end), [`todo.md`](todo.md) §8 row 5b, [`lessons.md`](lessons.md) —
[L14](lessons.md) is new

⚠️ **`handover-phase-5.md` is still required reading and its §1 still applies.**
This document does not replace it. It corrects two things in it and adds five.

---

## 1. Read this first — what will bite you

### 1.1 ⚠️ `handover-phase-5.md` §2 said students 5. It is 4, and always was

Measured against `attendancesystem_db`, with four matching `dataset/` folders.
Nothing went missing — the figure was **carried** from `handover-phase-4.md`
through `-4b.md`, and Phase 5 "confirmed" it with the sentence *"The live row
counts are byte-identical to `handover-phase-4b.md` §2"*, which checks a
handover against a handover.

That is [L8](lessons.md) happening inside the document L8 exists to protect.
Corrected in place, with the correction left visible rather than the number
quietly edited.

**Every figure in §2 of *this* document was re-measured today.**

### 1.2 ⚠️ A review's *fix* is not evidence, even when its *finding* is

New lesson [L14](lessons.md), and the most useful thing this sprint produced.

R4 said absences carry a fabricated `time_in = NOW()` — true, read straight off
the SQL literal — and prescribed "write `NULL`". `attendance.time_in` was
`TIME NOT NULL` and this server's `sql_mode` is
`NO_ZERO_IN_DATE,NO_ZERO_DATE,NO_ENGINE_SUBSTITUTION`, **no
`STRICT_TRANS_TABLES`**. NULL is not refused there; it is silently stored as
`00:00:00`. The prescribed fix would have swapped a fabricated end-of-class
time for a fabricated midnight, on an INSERT reporting success — and the
register would have looked *more* plausible than before.

Migration **007** widens the column first.

**Before writing a value a column has never held — NULL above all — run
`SHOW COLUMNS`.** Two seconds. The review had verified the writer and both
readers; it could not see the column, because a review reads files and the
column lives on the server.

### 1.3 ⚠️ Schema is at **007** now. Phase 5 said "no migration this sprint"

`007_absence_has_no_arrival_time.sql`, applied to the deployment.
`auto_migrate` is on, so `python app.py` handles a stale copy. Anything that
quotes "schema at 006" is out of date.

### 1.4 ⚠️ An administrator signed in before this build gets one 403

`session['admin_id']` is set at login now (R8). A cookie created before that
change has no `admin_id`, so `/settings`, `/update_admin` and
`/change_password` refuse with a 403 and a logged reason until the
administrator signs in again.

**This is deliberate and the alternative is the bug.** The fallback it
replaced was `admin WHERE id=1` — a silent write to whichever row happened to
be first. `docs/uat_manual.md` **A8** tells the tester about it so it is not
reported as a defect.

### 1.5 ⚠️ `save_attendance()` returns a **string** now, not a bool

`"Present"` / `"Late"` on success, `None` on failure. `if saved:` still works;
`is True` does not. The `status` parameter is **gone** — see §3.

### 1.6 ✅ **The "MariaDB stops on its own" warning is wrong. Delete it from your model**

**Corrected by the user, 2026-08-16.** Four handovers now carry some version of
*"the database server stops on its own, with nothing written to its error
log"* — `handover-phase-4.md` §1.3, `-4b.md` §1, `-5.md` §1.6, and this
document's first draft, which escalated it to "four occurrences is a pattern,
not bad luck".

There is no pattern. **XAMPP's MySQL does not start automatically after a
Windows restart, and the user starts it by hand.** Every "occurrence" is a
session that began after a reboot. That is also why `mysql_error.log` shows no
shutdown: the process was not running to log one, because the machine had been
restarted.

**The evidence was never evidence.** A missing shutdown line is exactly what a
clean OS restart looks like, and each agent read it as an anomaly because the
previous handover had already framed it as one. Nobody asked the person
operating the machine — a one-sentence question that would have closed it four
sprints ago. New lesson [L15](lessons.md).

Start it from the XAMPP Control Panel. Nothing here needs investigation, no
data has ever been lost, and it should not appear in the thesis as a
reliability finding.

### 1.7 ⚠️ The suite no longer writes to `logs/app.log`, and did not use to

`tests/conftest.py` redirects `settings.log_dir` at **module** scope. It has to
be module scope: `app.py` calls `configure_logging()` at import, pytest imports
test modules during *collection*, and that is before any fixture — including a
session-scoped autouse one — has run. A fixture here reads better and does
nothing.

The existing `logs/app.log` (3.1 MB, mostly test output) was left alone. It is
the user's artefact; deleting it is their call.

---

## 2. Verified current state

Every number below was measured today, on this branch, in this order.

| Check | Result |
|---|---|
| `ruff check .` | **clean** |
| `pytest tests/ -q` | **1,143 passed, 42 skipped** (1,101 / 40 at the branch point) |
| `pytest tests/integration/ -q` (gated) | **39 passed** (37 at the branch point) |
| `eval_heldout_accuracy.py` | **60/60, avg distance 34.95** — unchanged since Phase 0 |
| `scripts/migrate.py --status` | **0 pending** |
| Schema version | `schema_migrations` at **007** — ⚠️ **this sprint added one** |
| Database | **MariaDB 10.4.32**, XAMPP, `attendancesystem_db` |
| `attendance.time_in` | `time`, **`Null: YES`** — the 007 change, confirmed on the deployment |
| `@@sql_mode` | `NO_ZERO_IN_DATE,NO_ZERO_DATE,NO_ENGINE_SUBSTITUTION` — **still not strict** |
| Live rows | students **4**, subjects **1**, attendance **0**, enrolments **0**, attendance_sessions **0**, attendance_audit **0**, instructors **0**, admin **1** |
| `dataset/` folders | **4** — `12345`, `23-1-1-0559`, `23-1-1-0918`, `23-1-1-0920`; one per student row |
| `git check-ignore dataset dataset_staging trainer certs` | 4 matches — SAFE |
| Routes | 46 rules — unchanged |
| Python | 3.11.5 |

**The row counts are unchanged by this sprint** — no test wrote to the
deployment, which was checked afterwards rather than assumed ([L6](lessons.md)).

### Re-establish the baseline

```bash
git checkout phase-5-web-and-ux
.venv/Scripts/python.exe -m ruff check .
.venv/Scripts/python.exe -m pytest tests/ -q                # 1143 passed, 42 skipped
INTEGRATION_DB_NAME=test_attendance_scratch \
  .venv/Scripts/python.exe -m pytest tests/integration/ -q  # 39 passed
.venv/Scripts/python.exe eval_heldout_accuracy.py           # 60/60, avg 34.95
.venv/Scripts/python.exe scripts/migrate.py --status        # 0 pending
```

All five were run today in that order and produced those numbers.

---

## 3. What changed

| commit | what |
|---|---|
| ⭐ `ccb5214` | Make the live list work, and let a JSON route fail as JSON (R1, R7) |
| ⭐ `5b5b118` | Let the schedule decide the status, and show what it decided (R2, FS-8) |
| ⭐ `c8d86f7` | An absence has no arrival time (R4, migration 007) |
| `a1872b6` | The running session decides the subject, not the End form (R3) |
| `c3c3139` | Make the export's "server-side cursor" claim true (R6, PE-8) |
| `2767184` | Clear the rest: R5, R8, R9, R10, R11, R12, R14 |

The per-finding detail is the resolution table at the end of
[`review-phase-5.md`](review-phase-5.md). Three are worth understanding rather
than skimming.

### ⭐ R1 — the live list had never worked

```python
student_ids = sorted(recognition_session.recognized_ids)
# TypeError: 'method' object is not iterable
```

500 on every poll, in every session, since the route was written. US-3 exists
because FS-14 was a session that recorded nothing while looking healthy — so
**the one instrument built to tell a working session from a silently broken one
was itself silently broken.**

It survived 1,101 tests and a clean `ruff` because the guard above it returns
early unless a session is **running**, and nothing in the suite had ever
constructed that state. `grep -rl "attendance_live" tests/` returned nothing at
all.

### ⭐ R2 — FS-8 was closed in Phase 4 and reopened by an argument

`save_attendance()` resolved `status or derive_status(now, scheduled_start)`.
The parameter was added so a *correction* could force a value. The recognition
path was then given one too — `save_attendance(locked_id, subject, "Present")` —
and that literal short-circuited the derivation for the **only production
caller there is**. Every row written since said Present; `/reports` rendered a
Late counter that was structurally 0, which is the shape of FS-4.

The parameter is **deleted**, not merely unused at the call site. Its claimed
consumer, the FS-10 correction route, does not go through this function — it
writes through `repositories.attendance.set_status()`, beside the audit row.
With no parameter the defect is unrepresentable, which is worth more than a
test forbidding it.

**Why the suite missed it, and this is [L3](lessons.md) in its purest form.**
`tests/integration/test_attendance_schema.py` asserts the derivation works — by
calling `save_attendance(student, subject)` with no status. A correct test of a
signature production did not use, green while the system ran the other branch.
It now calls the only signature there is.

**The guard that would have caught it is in the loop smoke test.** Its
`save_attendance` stub was `(student_id, subject, status)` — written to match
the call site, so it *documented* the defect. It takes `*args` now and
`test_the_loop_does_not_dictate_the_status` asserts the loop passes two
arguments. A stub shaped to its caller can never disagree with it.

### ⭐ R4 — see §1.2

---

## 4. Corrections

### 4.1 — to `handover-phase-5.md` §2

Students is **4**. See §1.1.

### 4.2 — to `handover-phase-5.md` §4.3 and the `services/reporting.py` docstring

Both said export rows "come from a server-side cursor one at a time". They came
from `cursor.fetchall()`. The openpyxl half of the claim was always true; the
query half was prose written up alongside it. It is `iter_filtered()` now, so
the sentence describes the code and the documents need no further correction.
[L9](lessons.md).

### 4.3 — to `handover-phase-5.md` §2's "schema still at 006"

**007.** See §1.3.

### 4.4 — to `config/settings.py`'s note on the export

It said the export "builds a temporary file, streams it and deletes it". That
was the *first* implementation, replaced within the same sprint because the
`call_on_close` hook does not fire under the test client. `handover-phase-5.md`
§4.2 recorded the correction; the source comment did not get it until now.

---

## 5. What the new tests protect

| Finding | Protected by |
|---|---|
| R1 | `test_attendance_live.py` — every case stubs a **running** session; the stub keeps `recognized_ids` a method, so the file cannot pass against the broken route |
| R2 | `test_the_loop_does_not_dictate_the_status` (loop smoke) — the loop passes two arguments; plus four `TrackState.recorded_status` cases |
| R3 | `test_end_attendance_subject.py` — 7 cases with the session machine running |
| R4 | two integration tests: `time_in IS NULL` **evaluated in SQL**, and `SHOW COLUMNS` reporting `Null: YES` |
| R5, R10, R14 | `test_subject_routes.py` — including that the log names the subject that failed |
| R6 | `test_register_streaming.py` — a fake cursor whose `fetchall()` **raises** |
| R7 | `test_a_fault_answers_json_rather_than_an_html_page`, plus the existing 413 test with the `/enrol/` hack removed |
| R8 | `test_admin_identity.py` — 9 cases, including that `update_admin` refuses **before** the UPDATE |

**Every one was mutation-tested before being trusted**, restoring the defect
and watching the right tests fail, undone from a **copy** rather than with
`git checkout --` ([L13](lessons.md)). Two results are worth recording:

- **A mutation that correctly fails nothing.** Replacing `active.subject_id`
  with `submitted_id` on R3's success path changes no behaviour — past the 409
  guard the two are provably equal. A test failing there would have been
  testing the code's shape rather than what it does.
- **Two assertions are guards, not tests**, and both exist because a
  naturally-written check would have passed for the wrong reason: the R4
  assertion had to be evaluated in SQL (the Python value reads a coerced
  `00:00:00` as a `timedelta` and passes), and the R6 check had to drive a
  cursor rather than grep `reporting.py`, whose docstring names
  `iter_filtered` three times ([L12](lessons.md)).

---

## 6. Handed to you — unchanged, and now actually runnable

`handover-phase-5.md` §6 stands in full. **Nothing in it was done this sprint**
— it is all work only a person can do. What changed is that two items were
untestable before:

1. **Populate a class list.** `enrolments` is still **0 rows**. First item on
   four consecutive handovers now.
2. **A live browser enrolment run.** Unchanged.
3. **A live recognition run.** ⚠️ **Both instruments this depends on were
   broken.** The live list (US-3) answered 500 on every poll, and Late could
   not be recorded at all. Running this before 5b would have produced a clean
   sheet from a broken system.
4. **A remote enrolment over HTTPS.** Unchanged.
5. **`tasks/notes.txt`** is still uncommitted, still yours.

`docs/uat_manual.md` was revised and then **rewritten**, on the user's
instruction, to about half its length. New cases **E10b**, **E11b**, **E11c**
and **A8**; **G8** and **F4b** changed their expected values; the sign-off
totals were wrong (56, against 71 actual cases, with section X missing
entirely) and are fixed.

⚠️ **The rewrite is the part to understand, because I got it wrong first.** My
revision explained the fixes to the tester — a banner naming four defects,
cases annotated *"never passed before 2026-08-16"*, a priority note describing
how invisibly the routes had been broken. The user's correction: *"UAT does not
need bug explanation, testers do not need to be informed what was fixed before
the testing."*

That is not a style preference. **Telling a tester what was recently broken
tells them what to expect**, which is the one thing an acceptance test must not
supply — it primes them to accept a marginal result as success, or to
attribute an unrelated failure to the thing they were told about. New lesson
[L16](lessons.md): write each document for who reads it. The history belongs
here, in `review-phase-5.md` and in `todo.md` §8, and nowhere near the manual.

**Keep the manual free of it.** `Expected` states correct behaviour, full stop
— no finding IDs, no "it used to", no dates.

### ⚠️ Two documents the tester relies on were telling them not to report real defects

Found while checking whether the fixes made anything stale. Outside the
fourteen findings, and worth more than several of them.

**`docs/uat_manual.md` §7 — "Known limitations, do NOT raise these as
defects".** Four of its ten entries had stopped being true, and two
**contradicted test cases in the same document**: it said the layout was not
responsive while **X8** tests that it is, and that status is shown by colour
while **X7** tests that it is not. It also said *"renaming a student breaks
their face data — do not rename an enrolled student"*, which the Phase 5
`dataset/{student_id}` migration made false; a rename is one UPDATE now, and
renaming an enrolled student is a **valid thing to test**. And it said the
system is not on HTTPS, which CO-3 changed.

A stale entry in a "do not report this" list is worse than a missing one — it
suppresses a real defect report during the one run that was meant to find them.
The list is rewritten, the removed entries are kept in a second table saying
what they are now, and the section says explicitly that **if a test case and a
limitation disagree, the case wins.**

**`docs/limitations.md` §6 — "Functional gaps still outstanding … These are
defects, not design decisions."** Written during the Phase 0 audit and never
updated: **eight of its nine entries had been closed** — FS-3, FS-4, FS-5,
FS-8, FS-10, FS-11, PE-4 and the training job. It was telling a reader the
system cannot record an absence, count a dashboard, or correct a mistake. Split
into 6.1 (five genuinely outstanding, now including the SE-9 items) and 6.2
(closed, with the finding ID for each, because the write-up needs the before as
well as the after).

⚠️ **The general lesson is the same one as [L8](lessons.md):** a document is
only as trustworthy as its last measurement, and these two were being *used* as
authoritative while nobody had re-read them since the features landed. Both are
worth a pass at the end of every phase, not just when something looks wrong.

---

## 7. What is NOT done

Everything in `handover-phase-5.md` §7 still stands — PO-3, PO-4, SE-9's
implementation half, US-6 beyond the web UI, CO-3. Plus:

- **There is still no sweep for route behaviour with the session machine in a
  non-default state.** Two files now construct it deliberately, and each found
  its defect only because of that. The review's closing section named this as
  the gap and it is still the gap — a `test_template_endpoints.py`-style ban
  for *state* rather than routing does not exist.
- **The absences already recorded with a fabricated `NOW()` were not
  rewritten.** There are none on the deployment (`attendance` is 0 rows), so
  this is theoretical — but the rule is that correcting them would be inventing
  a different fiction, and `attendance_audit` is where a deliberate change to a
  record belongs.
- **`logs/app.log` still contains 3.1 MB of test output** from before R9. New
  entries are clean. Truncating it is the user's call.

---

## 8. Open decisions

**Nothing is blocked on a decision.** Two were taken this sprint, both with the
user on 2026-08-16:

1. ✅ **R3 refuses a mismatched subject with a 409** naming both, rather than
   silently preferring the running session's. The operator being told they
   mis-selected is worth more than the write succeeding quietly.
2. ✅ **R2 carries the recorded status to the camera overlay**, not just the
   register. A student marked Late in the register and "Present" on the screen
   is two screens disagreeing about one session, which is FS-7 one layer up.

`handover-phase-5.md` §8 and `handover-phase-4b.md` §8 both still stand.

---

## 9. Suggested first move

**Run the UAT.** That was `handover-phase-5.md` §9's advice too, and it is more
true now: the two P1s meant a UAT run before today would have recorded a clean
sheet from a system whose live list had never worked and which could not record
a Late.

`handover-phase-5.md` §9's FAR note is unaffected by this sprint — the impostor
figures in `docs/benchmarks.md` §4a are still there and still the cheapest
Phase 6 item, and it still does not need the recapture.

⚠️ **Before quoting any accuracy figure**, read `docs/limitations.md` and
`docs/walkthrough.md` §4–§5. Unchanged by this sprint: 60/60 is a same-session
split and cannot be quoted without its four caveats.

**And take §1.2 into Phase 6.** The next thing that reads like a
well-evidenced instruction — a review, an earlier handover, a docstring — is
evidence about a defect and a *hypothesis* about its remedy. This project has
now produced that failure at the parameter level (L2), the evaluator level
(L3), the benchmark level (L9), the migration level (L11), the test level
(L12), and the schema level (L14).

Assume the next reader has no memory of this session.
