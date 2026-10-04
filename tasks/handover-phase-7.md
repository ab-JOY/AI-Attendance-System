# Handover — Phase 7 → next

**Sprint:** Phase 7 — academic structure (departments, programs, sections), and
everything an instructor sees scoped to their own classes
**Date:** 2026-10-04
**Status:** ✅ **Implemented, tested, and applied to the deployed database.**
Migration 009 is live. **Nothing is committed** — the user did not ask for a
commit, and the working tree also carries five files of theirs that were
already modified at session start (§7).
**Read with:** [`todo.md`](todo.md) §10 — the findings (DM-1…DM-6) and §7.7,
the open decisions · [`lessons.md`](lessons.md) L34, L35

---

## 0. What changed, in one table

| Finding | Change | Files |
|---|---|---|
| **DM-1** | `departments` and `programs` existed on the deployed database, made by hand, in no migration and read by no code. 009 adopts them (`IF NOT EXISTS`, `INSERT IGNORE`) and seeds a fresh install with the same 9 + 9 rows | `migrations/009_academic_structure.sql` |
| **DM-2** | New `sections` table (program + year level + name). `students.section_id` replaces four free-text columns as the thing that is read; department, program and year come through the joins | `migrations/009…`, `repositories/academics.py`, `repositories/students.py`, `repositories/enrolments.py`, `web/placement.py`, `web/students.py`, `web/enrolment.py` |
| **DM-2** | Student lists grouped under one heading per section and filterable by department, program, year and section | `templates/manage_students.html`, `students.html`, `student_group_row.html` |
| **DM-3** | `instructors.department_id`; instructor lists grouped and filterable by department | `repositories/instructors.py`, `web/instructors.py`, three templates |
| **DM-4** | The subject form picks an instructor **account**; 009 links existing subjects whose typed name matches exactly one account | `repositories/subjects.py`, `web/subjects.py`, `templates/subjects.html`, `edit_subject.html` |
| **DM-5** | Reports, export, attendance page, start/end session, corrections and the dashboard are limited to the signed-in instructor's own subjects. Admin sees all, or one instructor via a filter | `security/access.py`, `web/reports.py`, `web/sessions.py`, `web/dashboard.py`, `web/auth.py`, `web/api.py`, `repositories/attendance.py` |
| — | `/reports` gains a **Sessions** table, an Instructor column and the missing **Total Late** card; the export gains an Instructor column (last) | `templates/reports.html`, `services/reporting.py` |

| Gate | Before | After |
|---|---|---|
| `pytest` (fast) | 1390 passed, 43 skipped | **1462 passed, 70 skipped** |
| `pytest tests/integration` (scratch DB) | not re-measured this session before the change | **67 passed** |
| `ruff check .` | 20 errors, all `web/api.py` | 18 errors, all `web/api.py` — **pre-existing, not from this sprint** (§6) |
| `eval_heldout_accuracy.py` | — | **100/100, avg 35.73** |
| Mutation run, 11 guards | — | **11 caught, 0 survived** |

⚠️ **The held-out figure is not comparable with 6f's 80/80 avg 33.46.** The
dataset has grown since; recognition code was not touched this sprint and the
run is quoted only to show it still works. Read `docs/walkthrough.md` §4–§5
before quoting it anywhere.

---

## 1. The deployed database, as reviewed (MariaDB 10.4.32, 2026-10-04)

Before 009: students 4, instructors 3, subjects 3, enrolments 12, attendance
46, sessions 74, audit 0. Integrity was clean — 0 orphaned attendance rows, 0
attendance rows for a student not on that class list, 0 classless students.

After 009, measured on the live database:

```
departments 9 · programs 9 · sections 2
sections:  CCIT / BSCS / year 1 / "B"   -> 3 students
           CCIT / BSCS / year 4 / "4B"  -> 1 student
students placed 4, unassigned 0; placement disagrees with what was typed: 0
subjects linked to an instructor account: 3 of 3 (was 0 of 3)
instructors with a department: 0 of 3   <- deliberately not guessed, §5
every pre-existing row unchanged (row counts + content hash of students
and attendance, before vs after)
```

**Rehearsed first** on `test_live_copy_009`, a scratch database loaded from a
dump of the live one, with the same checks and the same result; then all 12
changed pages were rendered against it as admin and as each instructor; then
the live run. The scratch copy has been dropped.

**Two dumps are in `logs/db-backups/`** (gitignored — they hold names and
password hashes). `attendancesystem_db_before_009_final.sql` is the one taken
immediately before the live migration. Restore with
`mysql -u root attendancesystem_db < that-file`.

### Found in the review and NOT changed

- **DM-6** — three sessions from 2026-09-01 (ids 12, 24, 28) were never ended.
  They show as "Not ended" in the new Sessions table. Data; the user's call.
- **A stray identity in the model.** `scripts/preflight.py` reports
  `trained but not a student: test-111` — 1 of 5 checks failing, and it was
  failing before this sprint. Remedy is in its own message.
- **Section names are inconsistent** — `B` (year 1) and `4B` (year 4), and the
  three year-1 students are on the class lists of two subjects labelled `4B`.
  Migrated as typed. It reads like test data, but that is a guess.
- **`test_attendancesystem_db`** exists on the server. Not created this
  session; left alone.

---

## 2. Migration 009 — traps

- ⚠️ **It drops nothing.** `students.college_department`, `program`,
  `year_level` and `section` are still on the table and **no longer read or
  written**. They are stale by design from now on. Dropping them is §7.7.1.
- ⚠️ **Never `SELECT s.*` from `students` beside the joins.** The joined
  aliases reuse the legacy names (`program`, `section`, …) so templates and the
  mobile API did not have to change; `s.*` would return both and a dictionary
  cursor keeps whichever came last. `students_repo.ACADEMIC_COLUMNS` says so.
- ⚠️ **A student is placed by `program` alone.** The old department text was
  never evidence — the form let any department pair with any program, and the
  deployed value (`College of Computing`) is not a department. Consequence:
  on another database, a student typed as `BSIT` lands under **CIT —
  Industrial Technology**, because that is what `BSIT` is in the user's
  `programs` table. `BSIS` and `BSEd`, which the old dropdown offered, are not
  programs at all; those students come out **Unassigned** with their text
  intact.
- ⚠️ **Not replay-safe past the first `ALTER`.** Same as 005: if it fails after
  `ADD COLUMN section_id`, a re-run fails on the duplicate column. Nothing in
  it is data-dependent enough to fail there, and it ran clean three times
  (integration, rehearsal, live).
- **The instructor backfill only links an unambiguous name** (`HAVING COUNT(*)
  = 1`). Two accounts sharing a fullname are left unlinked. Pinned by a test
  and by a mutation.
- **CI is MySQL 8, the deployment is MariaDB 10.4.** The program seeds are nine
  single statements rather than a join against a derived table of literals,
  which can raise "Illegal mix of collations" on MySQL 8. ⚠️ **Not run against
  MySQL 8 here** — there is none on this machine. CI will be the first.

---

## 3. Placement — how a student gets a section

`web/placement.py::parse_placement()` is the one definition of a valid
placement (program id, year level 1–6, section name ≤ 100 chars), used by web
registration, the edit screen and the mobile API.
`academics_repo.section_for()` finds or creates the section in **one
statement** (`ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(id)`); the unique
key is case-insensitive, so `4b` finds `4B`.

- ⚠️ **`/enrol/finish` parses the placement again.** `record` round-trips the
  browser as JSON, so it is a request body by the time it comes back. An
  unusable one does **not** fail the enrolment — the images are already on
  disk — the student is written Unassigned and a WARNING is logged.
- ⚠️ **The edit screen refuses instead** ("That program no longer exists"),
  because there the operator chose a placement and would not be told it was
  dropped. The two differ on purpose.
- **The mobile API now requires year level and section when a program is
  given** (400 otherwise), and ignores the `college_department` it is sent.
  `mobile/src/screens/StudentRegistrationScreen.js` validates the same.
  ⚠️ **The mobile change was not run** — there is no React Native harness here.
- `/api/colleges_programs` reads the tables now and keeps its two flat lists;
  it also returns `structure`, pairing each program with its department, which
  the app does not use yet. **The app's two pickers are still independent**, so
  a student can pick a college that does not own their program. Harmless — the
  server ignores the college — but the screen should use `structure`.
- **Fixed in passing:** `edit_students.html` compared `student.year_level ==
  "1"` (int vs string), so no year was ever preselected and **saving any edit
  moved the student to 1st year**.

---

## 4. DM-5 — the instructor scope, and its traps

`security.access.instructor_scope()` returns `None` for an administrator
(unrestricted) or the signed-in instructor's `instructors.id`. Every scoped
query takes it as `instructor_id`.

- ⚠️ **It fails closed.** A session with no `instructor_pk` gets
  `NO_INSTRUCTOR` (0), which matches no subject. **An instructor who was signed
  in across this deployment sees an empty system until they sign in again** —
  and so does a mobile token issued before the `instructor_pk` claim (24 h).
  That is the intended failure; the alternative reads "I do not know who you
  are" as "unrestricted".
- ⚠️ **`session['instructor_pk']` is not `session['instructor_id']`.** The
  second is the login identifier the instructor types, and what
  `/change_password` addresses the account by. Subjects reference the numeric
  key.
- ⚠️ **`?instructor=` is read only for an administrator.** For an instructor
  the value comes from the session. `web/reports.py::_filters()` serves both
  the screen and the export, so the two cannot disagree about whose register
  it is.
- ⚠️ **The filtered dropdown is the convenience; `_may_use_subject()` is the
  guard.** `/start-attendance` and `/end-attendance` refuse a subject that is
  not the instructor's even when the request names it by hand. Unlike the
  class-list gate beside it, this one **refuses on a database error** — it
  decides whose register is written.
- **Corrections answer 404, not 403**, for another instructor's record, on GET
  and on POST.
- ⚠️ **An offering with no instructor account belongs to nobody**, so no
  instructor sees it or can run it — only an administrator. Assign it under
  Subjects. On the deployed database all three are assigned.
- ⚠️ **`subjects.instructor` (the typed name) is no longer written.** Screens
  show `COALESCE(account name, typed name)`. It survives only for an offering
  with no account.
- **Not scoped:** `/attendance/live` and `/video_feed`. There is one camera and
  one running session; a second instructor loading the page sees that a
  session is running. Their End control cannot end it (403).
- **The Sessions table's tallies count rows that session wrote**
  (`attendance.session_id`). The register is one row per student per subject
  per day, so a second session the same day shows only what it added.

**Fixed in passing:** the Export link was a bare `/export_excel`, so the route
that honours the filters (FS-11) was never sent any and the download was always
the whole register. It carries the filters now, and a test parses the link out
of the rendered page.

---

## 5. Decisions taken without asking, and why

The user dismissed a clarification dialog and re-sent the request (L35), so
these were chosen and are listed for overruling:

| Choice | Alternative not taken |
|---|---|
| Schema **and** screens | screens only, over the old text columns |
| An instructor belongs to a **department** only | department + program, or an advisory section |
| Departments and programs are the 9 + 9 rows already in the database | an admin screen to manage them — **there is none; adding a program is SQL** |
| Sections are created on first use | an admin-managed section list |
| Instructor departments left **unset** | inferring one from the subjects taught |
| Subjects keep free-text `course` / `section` | linking offerings to `sections` (§7.7.2) |

---

## 6. Verified state, and what was NOT touched

**Untouched:** every recognition threshold, `LBPH_PARAMS`, the model,
`vision/`, `recognize_face.py`, `train_model.py`, the enrolment capture path.
`dataset/` and `trainer/` are still ignored (`SAFE`, checked with the
two-path form).

**`ruff` is not clean, and was not at session start.** 18 errors, all in
`web/api.py`, which arrived with the mobile work (`8d061c3`) after 6f's "clean"
was measured — module-level imports half way down the file and long lines. Two
fewer than at `HEAD` because this sprint removed two of the lines. Not fixed:
it is a different change.

**Every new guard was mutation-tested** from a copy (L13), 11 of 11 caught:
scope failing open, the URL choosing the instructor, the register unscoped,
start/end/correct unchecked, the dropdown unfiltered, the two-accounts backfill,
the untrimmed join, `section_for` always inserting, and a session with no rows
being dropped by an inner join.

**Two defects in this sprint's own work, both found by running rather than
reading:**

1. **The section heading 500'd every non-empty student list.** It was drawn by
   an included template reading `loop.changed()`; an include cannot see the
   caller's `loop`. 1,457 tests were green because every one that reached the
   page stubbed an empty list. Found by rendering the page against the migrated
   copy (L5). `tests/test_academic_lists.py` renders with rows and fails
   against the original.
2. **The 009 tests hung instead of failing** (L34) — a connection closed only
   on the test's last line held a metadata lock through teardown's
   `DROP DATABASE`. Found by the mutation run.

⚠️ **MariaDB stopped once during the first integration run.** Process gone,
nothing in `mysql_error.log`, XAMPP's panel and Apache still up. Restarted with
XAMPP's own command (`mysqld --defaults-file=mysql\bin\my.ini --standalone`);
InnoDB recovered 135 pages; the deployed database was at 008 and untouched at
the time, verified by row counts. **Not reproduced** in six further integration
runs, and the cause is unknown — L15 applies: do not build a theory on it. The
user may simply know.

⚠️ **Nothing was run in a browser or against a camera.** Pages were rendered
through `app.test_client()` — status codes, row counts and headings — against
the real database. Layout, the grouped dropdowns and the heading rows have not
been *looked at*.

**Update, 2026-10-04 (later session):** the user tested `/reports` themselves
and confirmed DM-5 behaves as intended — the administrator sees every report
and can filter by instructor; an instructor sees only their own. That is the
only screen confirmed by hand; the student and instructor lists, the subject
form, the dashboard and the export have still not been looked at. The fast
suite (1462 passed, 70 skipped) and the integration suite (67 passed) were
re-run in that session and match the table in §0.

---

## 7. Open

- **Commit.** Nothing is committed. `.gitignore`, `.vscode/settings.json`,
  `config/settings.py` (`flask_host` → `0.0.0.0`), `mobile/package.json` and
  `package-lock.json` were modified before this session and are **not this
  sprint's** — keep them out of its commit, or ask.
- **§7.7.1 — drop the four legacy columns** (migration 010). Safe here. Needs a
  gate elsewhere: an Unassigned student's typed program exists only there.
- **§7.7.2 — link `subjects` to `sections`**, which would let a class list be
  filled from a section.
- **§7.7.3 — DM-6**, the three unclosed sessions.
- **§7.7.4 — set the three instructors' departments** on Manage Instructors.
- **Look at the pages in a browser**, admin and instructor.
- **Mobile:** run the registration screen; switch its pickers to `structure`.
- **An admin screen for departments, programs and sections** — none exists.
- Everything still open from 6f: the 70–95 px dead band (the user's), SE-18
  and R1 measured on the demo camera.
