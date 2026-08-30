# End-to-end review of the Phase 5 refactor

> ## ✅ Resolved — all fourteen, 2026-08-16
>
> Cleared before the UAT, on this branch. Every finding below reproduced
> exactly as written; the resolution of each is in the table at the end of this
> document, and each has a commit of its own.
>
> **One correction to the review itself, and it matters.** R4's prescribed fix —
> "write `NULL` for `time_in`" — would not have worked. `attendance.time_in` was
> `TIME NOT NULL` and this server has no `STRICT_TRANS_TABLES`, so the NULL
> would have been silently stored as `00:00:00`: a fabricated midnight in place
> of a fabricated end-of-class time, on an INSERT reporting success. R4 needed
> **migration 007** as well as the code change. Written up as
> [L14](lessons.md).
>
> **Two decisions were the user's** and were taken on 2026-08-16: R3 refuses a
> mismatched subject with a 409 rather than silently preferring the session's,
> and R2 carries the recorded status through to the camera overlay rather than
> fixing the register alone.

**Date:** 2026-08-16 · **Branch:** `phase-5-web-and-ux` at `81ecad9`
**Baseline re-measured first:** `pytest tests/ -q` → 1,101 passed, 40 skipped;
`ruff check .` → clean. **None of the fourteen findings below is caught by
either.**

Read with [`handover-phase-5.md`](handover-phase-5.md), [`todo.md`](todo.md)
and [`lessons.md`](lessons.md). No source file was modified during this review
and nothing was written to the database.

---

## Summary

| Priority | Count | What it means |
|---|---:|---|
| **P1 Critical** | 2 | A shipped feature is dead, or every row it writes is wrong |
| **P2 High** | 2 | The register can be written with data that is not true |
| **P3 Medium** | 5 | Wrong failure mode, latent bug, or a claim the code does not support |
| **P4 Low** | 5 | Stale prose, hygiene, small inconsistencies |

---

## P1 — Critical

### R1 — `/attendance/live` returns 500 on every poll (US-3)

**Where:** [web/sessions.py:273](../web/sessions.py#L273)

**Symptom.** The moment a session is running, `GET /attendance/live` answers
500. `attendance.js` sees the non-OK status, calls `stopPolling()` and shows
*"The live list stopped updating (status 500). The session is unaffected;
reload to resume."* Reloading fails identically. The live list has never
worked.

**Root cause.** `RecognitionSession.recognized_ids` is a **method**, not a
property, and the route uses it without calling it:

```python
student_ids = sorted(recognition_session.recognized_ids)
# TypeError: 'method' object is not iterable
```

The guard above returns early unless a session is running, so the defect is
unreachable in the state every test leaves the session in — and
`grep -rl "attendance_live" tests/` returns nothing. All six other call sites
in the repo (all in tests) write `recognized_ids()`.

**Evidence.** Reproduced through `app.test_client()` with a stubbed running
session, no camera and no model: **500**, with the `TypeError` logged by
`web.errors:107`.

**Why it matters.** US-3 exists because FS-14 was a session that recorded
nothing while looking healthy. The instrument built to tell a working session
from a silently broken one is itself silently broken.

**Fix.** `sorted(recognition_session.recognized_ids())`, plus a route test that
stubs a *running* session — the state no existing test constructs.

---

### R2 — "Late" can never be recorded (FS-8)

**Where:** [recognize_face.py:825](../recognize_face.py#L825), against
[recognize_face.py:664](../recognize_face.py#L664)

**Symptom.** Every row recognition writes is `Present`, whatever the subject's
scheduled start. `/reports` renders a *Late* counter that is structurally 0 —
the same shape as FS-4, which Phase 4 existed to remove.

**Root cause.** `save_attendance()` resolves the status as
`status or derive_status(now.time(), scheduled_start)`. Its **only** production
caller passes a literal:

```python
# process_confirmed_track()
attendance_saved = save_attendance(locked_id, subject, "Present")
```

The `status` parameter was added so a *correction* could force a value; the
recognition path was given one too, and `derive_status()` became unreachable in
production.

**Why the tests miss it.** `tests/integration/test_attendance_schema.py`
asserts the derivation works — by calling `save_attendance(STUDENT,
active_subject(id))` with **no status**. It exercises a signature production
never uses. This is [L3](lessons.md) exactly: a harness that shares no path
with production measures nothing while printing a clean result.

**Fix.** Drop the argument at the call site; keep the parameter for the
correction route. Then either make the integration test call the production
path, or add an `ast` check that no caller outside `web/` passes a status — the
style already used by `tests/test_db_access.py`.

---

## P2 — High

### R3 — Ending a session trusts the form's subject, not the running session's

**Where:** [web/sessions.py:176-215](../web/sessions.py#L176-L215)

**Symptom.** The End form carries its own subject dropdown, defaulting to
"Choose a subject…". Pick a different offering than the one running and the
absent register is written for *that* offering, each row stamped with the
*running* session's `session_id` — which belongs to a different subject. The
running session is closed regardless.

**Root cause.** Two independent sources of one fact, never reconciled:

```python
active = recognition_session.subject          # what is running
subject_id = request.form.get('subject_id')   # what was submitted

register = attendance_repo.register_for(cursor, subject_id)
attendance_repo.insert_absences(
    cursor, [(row['student_id'], subject_id, active.session_id) ...]
)
```

The template comment says a dropdown means "the two can no longer disagree by a
typo". True — and they can still disagree by a mis-selection, which nothing
checks.

**Why it matters.** The result passes the schema: the FK to
`attendance_sessions` resolves, so nothing errors. It produces a referentially
valid register that is factually wrong — students marked absent from a class
that was never held, attributed to another subject's session.

**Fix.** Prefer `active.subject_id` when a session is running and refuse a
submitted `subject_id` that disagrees (409, naming both). Fall back to the form
only when no session is active.

---

### R4 — Absent rows carry a fabricated arrival time

**Where:** [repositories/attendance.py:173](../repositories/attendance.py#L173)

**Symptom.** Every Absent row gets `time_in = NOW()` — the moment End was
pressed. Reports and the Excel export show an arrival time, identical across
the cohort, for students who never arrived.

**Root cause.** `insert_absences()` reuses the Present row's column list rather
than writing NULL:

```sql
VALUES (%s, %s, %s, CURDATE(), NOW(), 'Absent')
```

This contradicts the principle stated two hundred lines below it in
`set_status()`: *"a fabricated timestamp in a biometric register is worse than
an obviously untouched field."* The rule was written for corrections and never
applied to the write that creates the rows corrections operate on.

**Fix.** Write `NULL`. `services/reporting.py::_cell()` already renders `None`
as an empty cell and `attendance_live` already guards on it, so nothing on the
display side changes.

---

## P3 — Medium

### R5 — A missing subject 500s where every sibling route 404s

**Where:** [web/subjects.py:71](../web/subjects.py#L71)

`GET /edit_subject/999999` → **500**, `jinja2.UndefinedError: 'None' has no
attribute 'id'` (measured against the live database). The route passes
`subjects_repo.get()`'s result to the template without a None check, and
`edit_subject.html` calls `url_for(..., id=subject.id)`.
`subject_enrolments`, `edit_student` and `edit_instructor` all handle this
case. **Fix:** `if subject is None: return error_page(404, ...)`.

### R6 — The export's "server-side cursor" is a claim the code does not support

**Where:** [services/reporting.py](../services/reporting.py),
[repositories/attendance.py:203](../repositories/attendance.py#L203)

The module docstring, `todo.md` PE-8 and `handover-phase-5.md` §4.3 all state
that rows "come from a server-side cursor one at a time" and "the sheet never
holds them". But `register_workbook()` iterates
`attendance_repo.filtered(...)`, whose last line is `return cursor.fetchall()` —
the entire filtered register is materialised as a list of dicts before the
first row reaches the sheet. The openpyxl half of the claim is true; the query
half is not. This is [L9](lessons.md), and the figure is bound for the
manuscript. **Fix:** make it true (a repository function that `yield`s from
`fetchmany()`) or correct all three documents. The first is small and worth
more.

### R7 — A JSON route that faults answers with an HTML page

**Where:** [web/errors.py:95-108](../web/errors.py#L95-L108)

`@json_api` is honoured for 401/403 (in the access hook) and for 413 on
`/enrol/` paths. Everything else — 400, 404, 500 — renders `error.html`, so a
`fetch()` caller is handed markup to `JSON.parse`. The marker is read by the
access hook but not by the error handlers, and the 413 case was fixed by
special-casing a URL prefix rather than consulting the marker. R1 lands here.
**Fix:** look up `app.view_functions[request.endpoint]` in the handlers and
return JSON when the view carries the marker; delete the prefix test.

### R8 — Admin password change still targets `id = 1`, whoever is signed in

**Where:** [web/auth.py:188](../web/auth.py#L188),
[repositories/credentials.py:53](../repositories/credentials.py#L53)

Latent today — one `admin` row. Login authenticates *any* row by username, but
`_credential_target()` returns a hardcoded `1` and `rename_admin()` writes
`WHERE id=1`, while `/settings` shows whichever row `SELECT ... LIMIT 1`
returns. SE-16's write-up says the route "used to update `admin WHERE id=1` no
matter who was signed in"; the role handling was fixed and the hardcoded key
carried over. **Fix:** store the admin's key in the session at login, or key on
`username`.

### R9 — The test suite writes into the deployment's operational log

**Where:** [app.py:33](../app.py#L33)

`logs/app.log` is 2.7 MB and 25,000 lines, most of it test output — fabricated
sessions ("Refusing to start CS402: a session for CS401 is still running"), a
job named `t` raising on purpose, repeated camera scans. `app.py` calls
`configure_logging()` at module scope and several test modules import `app`.
The log is the artefact you would read after a failed demo, and it cannot
currently be trusted to describe production. **Fix:** point `log_dir` at
`tmp_path` from a session fixture, or skip the file handler when
`PYTEST_CURRENT_TEST` is set.

---

## P4 — Low

| ID | Finding | Root cause | Where |
|---|---|---|---|
| **R10** | Subject forms fail with a bare 400 naming no field | `_subject_form()` uses `request.form['x']`; a missing field becomes `BadRequestKeyError`. Every other form route validates and names the field. | [web/subjects.py:29](../web/subjects.py#L29) |
| **R11** | Two docstrings describe code that no longer exists | `settings.py` still says the export "builds a temporary file, streams it and deletes it" — corrected in the handover, not in the source. `conftest.py` still quotes pandas import cost; pandas was removed in Phase 5. | [config/settings.py:193](../config/settings.py#L193), [tests/conftest.py:17](../tests/conftest.py#L17) |
| **R12** | A `pageshow` listener accumulates on every form submit | `markBusy()` registers a fresh window listener per call instead of once per button. | [static/js/app.js:79](../static/js/app.js#L79) |
| **R13** | The handover's live row count is off by one | §2 records **students 5**; the database holds **4**, all with matching dataset folders. Either a row went since, or the figure was carried rather than measured — worth resolving, since [L8](lessons.md) is about exactly this. | [handover-phase-5.md](handover-phase-5.md) §2 |
| **R14** | Subject and instructor writes have no database error handling | Six routes rely on the catch-all 500 while their neighbours in `students.py` and the class-list routes catch `mysql.connector.Error` and log context. | [web/subjects.py:52-106](../web/subjects.py#L52-L106), [web/instructors.py](../web/instructors.py) |

---

## Why a green suite missed all of this

The four defects that matter share one shape, and it is the shape this project
keeps producing: **the check sits one layer away from where the failure lives.**

- **R1** is unreachable unless a session is *running*, and no test constructs
  that state.
- **R2** is covered by a test that calls the function with a different
  signature than production does.
- **R3** and **R4** write rows the schema accepts, so nothing raises.

In each case a test exists, is green, and is testing something adjacent to the
defect. [L3](lessons.md), [L5](lessons.md) and [L12](lessons.md) all describe
this, and the Phase 5 bans (`test_template_endpoints.py`, `test_db_access.py`)
are the right answer applied to routing and SQL. **The gap is that no
equivalent sweep exists for route behaviour with the session machine in a
non-default state** — which is where both P1s live.

---

## Suggested order

1. **R1 and R2 before the live runs in `handover-phase-5.md` §6.** Both are
   one-line changes, and both silently invalidate those runs — you would be
   watching a broken live list and collecting a register that cannot say Late.
2. **R7 with R1**, since it is why R1 presented as a status code rather than a
   message.
3. **R4, then R3.** R4 is one SQL literal. R3 needs a decision about what to do
   when the two subjects disagree; refusing is defensible, but it is the user's
   call.
4. **R6 before anything is quoted in the manuscript.** A performance claim in
   three documents is cheaper to make true than to defend.
5. **The rest at leisure.** R9 early anyway — it makes every later log readable.

---

## Resolution — 2026-08-16

Cleared in the order suggested above, six commits on `phase-5-web-and-ux`.
Baseline before: 1,101 fast + 37 integration, `ruff` clean. After: **1,143 fast
(42 skipped) + 39 integration**, `ruff` clean, held-out **60/60 avg 34.95**
(unchanged), schema **007**, live row counts unchanged.

| ID | Resolution | Where | Guarded by |
|---|---|---|---|
| **R1** | `recognized_ids()` is called. | `web/sessions.py` | `tests/test_attendance_live.py` — 7 cases, all with a **running** session stubbed. The stub keeps `recognized_ids` a *method*, so the file cannot pass against the broken route. |
| **R2** | The `status` parameter is **deleted**, not merely unused — its claimed consumer (the correction route) goes through `attendance_repo.set_status()`. `save_attendance()` returns the status it wrote; `TrackState.recorded_status` puts it on the overlay; `status_color()` gains an amber Late band (without it, "Late" fell through to the red of a *refusal*). | `recognize_face.py`, `vision/tracking.py` | `test_the_loop_does_not_dictate_the_status` in the loop smoke test, plus four `recorded_status` cases. ⚠️ The old stub was `(student_id, subject, status)` — shaped to the call site, so it *documented* the defect. It takes `*args` now. |
| **R3** | The running session's `subject_id` wins; a mismatched submission is refused **409** naming both, before `stop_camera()` and before the transaction, so nothing is written and the session survives. The form is trusted only when no session is running. | `web/sessions.py`, `templates/attendance.html` | `tests/test_end_attendance_subject.py` — 7 cases. |
| **R4** | ⚠️ **The prescribed fix was wrong.** See the banner at the top. Migration **007** makes `attendance.time_in` nullable, *then* `insert_absences()` writes NULL. Templates render an em-dash rather than Jinja's literal `None`. | `migrations/007_…`, `repositories/attendance.py`, two templates | Two integration tests, deliberately different claims: `time_in IS NULL` **evaluated in SQL** (the Python value would read a coerced `00:00:00` as a time and pass), and `SHOW COLUMNS` reporting `Null: YES`. |
| **R5** | 404, matching `edit_instructor`. | `web/subjects.py` | `tests/test_subject_routes.py` |
| **R6** | Made true. `iter_filtered()` yields from `fetchmany()` on the unbuffered cursor; `filtered()` stays for `/reports`; both share one `_filtered_query()` so the screen and the spreadsheet cannot drift (which is what FS-11 was). The three documents now describe the code. | `repositories/attendance.py`, `services/reporting.py` | `tests/test_register_streaming.py` — a fake cursor whose `fetchall()` **raises**. A grep for `iter_filtered` would have matched the docstring three times over ([L12](lessons.md)). |
| **R7** | `error_page()` consults the `@json_api` marker via a new `wants_json()` shared with the access hook. The `/enrol/` prefix hack is deleted. **Verified rather than assumed:** `request.endpoint` *is* still set for a 413, because Werkzeug refuses the body during form parsing, after routing — the existing oversized-body test passes with the hack gone. | `web/errors.py`, `security/access.py` | `test_a_fault_answers_json_rather_than_an_html_page`, plus the existing 413 test |
| **R8** | `session['admin_id']` at login; `admin_by_id()` replaces `the_admin()`'s `LIMIT 1`; `rename_admin()` is keyed. A session with no key is a **403 with the reason logged** — falling back to row 1 *is* the defect. | `web/auth.py`, `web/account.py`, `repositories/credentials.py` | `tests/test_admin_identity.py` — 9 cases, including that `update_admin` refuses **before** the UPDATE |
| **R9** | `tests/conftest.py` redirects `settings.log_dir` at **module** scope — the handler is attached during collection, before any fixture could run. Verified: `logs/app.log` is byte-identical across a suite run. `logs/app.log` itself is left alone; it is the user's artefact. | `tests/conftest.py` | measured, not asserted |
| **R10** | Validates and names the field, like `_class_list_form()` below it. Only `subject_code` and `subject_name` are required — a subject with no instructor or no scheduled time is a state the schema allows on purpose. | `web/subjects.py` | `tests/test_subject_routes.py` |
| **R11** | Both corrected. | `config/settings.py`, `tests/conftest.py` | — |
| **R12** | One `pageshow` listener at module scope, finding the disabled buttons when it fires — which also covers a button disabled by a submission the old closure never saw. | `static/js/app.js` | — |
| **R13** | **students is 4.** Four rows, four `dataset/` folders, no mismatch — the 5 was carried through `handover-phase-4.md` and `-4b.md` and confirmed here only by *agreeing with the previous handover*, which is what [L8](lessons.md) is about. Corrected in place in `handover-phase-5.md` §2. | `tasks/handover-phase-5.md` | — |
| **R14** | Six routes catch `mysql.connector.Error` and log context; `add_instructor` answers 409 on a duplicate ID. | `web/subjects.py`, `web/instructors.py` | `tests/test_subject_routes.py` asserts the log names the subject |

### What the review itself got wrong, and what that is worth

Thirteen of fourteen findings reproduced exactly as written, including every
line number. The fourteenth, R4, named a real defect and prescribed a fix that
would have replaced it with a better-disguised one — because a review that
reads files can see the writer and the readers but not the **column**, and the
column is the only participant whose behaviour depends on server configuration
rather than on this repository.

That is [L14](lessons.md): *a finding and its fix have different evidentiary
standing, and only one of them was measured.*

### Still true, and still the point

The review's closing section says the four defects that matter share one shape —
**the check sits one layer away from where the failure lives** — and that no
sweep exists for route behaviour with the session machine in a non-default
state. Two files now do that (`test_attendance_live.py`,
`test_end_attendance_subject.py`), and both found their defects only because
they construct the running state deliberately. **A general sweep still does not
exist.** That is the honest position for Phase 6 to inherit.
