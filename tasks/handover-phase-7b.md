# Handover — Phase 7b → next

**Sprint:** Phase 7b — a subject belongs to a section; `BSIT` is Information
Technology; the Classes list follows the student's program
**Date:** 2026-10-14
**Status:** ✅ **Implemented, tested, and applied to the deployed database.**
Migration 010 is live. **Nothing is committed** — the user did not ask for a
commit, and the working tree still carries the five files of theirs that were
modified before Phase 7 (§6).
**Read with:** [`todo.md`](todo.md) §11 — the findings (DM-7, DM-8) and the
open decisions · [`handover-phase-7.md`](handover-phase-7.md), which this
extends and does not replace

---

## 0. What changed, in one table

| Finding | Change | Files |
|---|---|---|
| **DM-7** | `subjects.section_id` → `sections`. An offering is taught to a section, and its program and year level are the section's — the same shape as a student's placement | `migrations/010_subject_sections.sql`, `repositories/subjects.py` |
| **DM-7** | The subject form takes **Program, Year Level, Section** instead of two text boxes, parsed by `web/placement.py::parse_placement()` — the function the student forms use | `web/subjects.py`, `templates/subjects.html`, `edit_subject.html` |
| **DM-7** | The Classes list on the registration form is one group per section; only the chosen program's groups are shown and the student's own section is selected. `/enrol/finish` refuses a subject of another program | `templates/students.html`, `static/js/students.js` (new), `web/students.py`, `web/enrolment.py` |
| **DM-7** | Class List offers only the subject's own program, its own section first, with one button to add the whole section (`POST /enrol_section`, admin) | `web/subjects.py`, `repositories/enrolments.py`, `templates/subject_enrolments.html` |
| **DM-8** | Industrial Technology re-coded `BSINDTECH`; `BSIT` added as **Bachelor of Science in Information Technology** under CCIT | `migrations/010_subject_sections.sql` |
| — | Programs are shown by full name on the Manage Students filter and on the mobile registration screen, which also stops asking for a college | `templates/manage_students.html`, `mobile/src/screens/StudentRegistrationScreen.js` |

| Gate | Before (re-measured this session) | After |
|---|---|---|
| `pytest` (fast) | 1472 passed, 76 skipped | **1487 passed, 86 skipped** |
| `pytest tests/integration` (scratch DB) | 73 passed (carried from 7 §8, not re-run before the change) | **83 passed** |
| `ruff check .` | 18, all `web/api.py` (carried from 7) | **18**, unchanged |
| Mutation run, 19 guards | — | **19 caught, 0 survived** |

`eval_heldout_accuracy.py` was **not run**: nothing under `vision/`,
`recognize_face.py`, `train_model.py` or any threshold was touched.

---

## 1. The deployed database (MariaDB 10.4.32, 2026-10-14)

It had grown since Phase 7's handover: 12 students, 7 sections, 3 subjects,
22 enrolments, 74 attendance rows, 84 sessions.

After 010, measured on the live database:

```
subjects linked to a section: 3 of 3
    IA     BSCS / 4B  -> BSCS, year 4, section 4B
    SE101  BSCS / 4B  -> BSCS, year 4, section 4B
    AL102  BSCS / B   -> BSCS, year 1, section B
programs: 10 (was 9)
    id 4   CIT   BSINDTECH  Bachelor of Science in Industrial Technology
    id 19  CCIT  BSIT       Bachelor of Science in Information Technology
every pre-existing row unchanged: row counts + content hash of students,
attendance, enrolments, sessions, sections, departments, instructors, the
pre-010 columns of subjects, and the eight other programs - before vs after
```

**Rehearsed first** on `test_live_copy_010`, loaded from a dump of the live
database: the migration, then all the changed pages rendered as admin, then
the new writes (`/add_subject`, `/update_subject`, `/enrol_section`). The copy
has been dropped. After the live run the pages were rendered again against the
live database, **read only**, and the hash re-checked.

**Two more dumps are in `logs/db-backups/`** (gitignored).
`attendancesystem_db_before_010_final.sql` is the one taken immediately before
the live migration. Restore with
`mysql -u root attendancesystem_db < that-file`.

---

## 2. Migration 010 — traps

- ⚠️ **It drops nothing.** `subjects.course` and `subjects.section` are still
  on the table. For a linked offering nothing reads or writes them; for an
  unlinked one they are what the screens show. A subject created by the form
  now has both NULL.
- ⚠️ **Never `SELECT s.*` from `subjects` beside the joins** — the same trap
  as `students` (7 §2). The joined values are aliased `course` and `section`,
  the legacy column names, so no template or API consumer had to change.
  `all_subjects()` and `get()` used `SELECT *` and list their columns now.
- ⚠️ **The aliases for an offering's section are `osec` and `op`**, not `sec`
  and `p`. The register joins the offering's section and the student's in one
  query, and `students.ACADEMIC_JOIN` already owns `sec`, `p` and `d`.
- ⚠️ **Only an unambiguous section name is linked.** The typed text has no year
  level, so "BSCS, section B" with a first-year B and a second-year B is left
  unlinked. It shows as *(not linked)* on Subjects, and Edit shows what was
  typed. Pinned by a test and a mutation.
- ⚠️ **The backfill runs before the re-code.** A typed course is read against
  the codes that existed when it was typed, so a legacy subject typed `BSIT`
  links to what is now `BSINDTECH` — consistent with how 009 placed a student
  typed `BSIT`. On this database nothing was typed `BSIT`.
- ⚠️ **The re-code matches the name as well as the code.** On a database where
  `BSIT` already means something else, the `UPDATE` matches nothing and the
  `INSERT IGNORE` is refused by the unique code: 010 then adds no program at
  all, silently. Check `programs` after migrating any other database.
- **Not replay-safe past the first `ALTER`**, like 005 and 009.
- **Not run against MySQL 8** (CI). The grouped picker query groups by
  `s.id, osec.id, op.id` rather than `s.id` alone for that reason; 009's roster
  query still groups by `s.student_id` only and has the same exposure.

---

## 3. A subject's section — how it is written and read

- **Written:** `web/subjects.py::_subject_form()` returns the six text fields
  and a parsed placement; `_with_section()` resolves it with
  `section_id_for()` **inside the write's transaction** and splices the id in
  after `day`. ⚠️ `insert()` and `update()` are positional — `SECTION_AT` is
  the one place that position is stated, and a test pins the whole tuple.
- **Required.** Program, year level and section are refused by name when
  missing; the instructor and the times are still optional. An unlinked
  subject can now only come from the migration.
- ⚠️ **Type the section the way it is typed for its students.** A subject for
  `BSCS / 4 / B` and students in `BSCS / 4 / 4B` are two different sections,
  and the class list is filled from the subject's. The lookup is
  case-insensitive (`4b` finds `4B`) and that is all.
- **Read:** `subjects_repo.SELECTION_COLUMNS` now carries `section_id`,
  `program_id` and `year_level` as well, so every picker has them.
  `repositories/attendance.py` and the two raw queries in `web/api.py` use
  `offering_join('sub')` / `offering_section('sub')`.

---

## 4. The Classes list, and its traps

- ⚠️ **The filter is JavaScript; the guard is `subjects_repo.existing_ids(...,
  program_id=)`.** `static/js/students.js` sets `hidden` and `disabled` on the
  `<optgroup>`s of other programs. `/enrol/finish` asks for the chosen ids
  that exist **and** are offered to the student's program, and logs the rest.
- ⚠️ **Changing program, year level or section resets the selection** to the
  student's own section's subjects. Deliberate: those three fields come before
  the list. Anything the operator selects by hand afterwards is kept.
- ⚠️ **An unlinked subject is offered to every program** — in the list (a
  last group, "Not linked to a section yet") and by the guard. Refusing it
  would make a subject 010 could not link impossible to enrol into.
- ⚠️ **An Unassigned student is not narrowed.** No usable placement means no
  program to compare with; narrowing to "none" would drop every subject.
- **The section is the group heading**, not a suffix on each subject. The
  attendance dropdown, the report filter and the mobile API still print the
  section after the subject name — they were not asked about, and two
  offerings of one subject have to be told apart there. 
- **Class List:** an offering with a section lists only its program's
  students. ⚠️ **An Unassigned student cannot be added to a linked subject**
  until they are given a program from Manage Students; the page says so.
  `enrol_section()` uses `INSERT IGNORE` so `rowcount` is the number actually
  added, which is what the flash reports.
- **Moving a student to another program on the edit screen does not touch
  their class lists.** They stay on the subjects of the old program until
  removed. Not changed: removing them silently would also be wrong.

---

## 5. Decisions taken without asking (L35), for overruling

| Choice | Alternative not taken |
|---|---|
| `BSIT` becomes Information Technology; Industrial Technology becomes `BSINDTECH` | a second code (`BSINFOTECH`) beside `BSIT` = Industrial Technology |
| A subject is taught to **one section** (program + year + name) | a subject linked to a program only, with the section left as text |
| The section is **required** on the subject form | optional, leaving "offered to every program" reachable from the form |
| Classes: grouped by section, own section pre-selected | a flat list filtered by program only |
| Class List limited to the subject's program | every student still listed, own program first |
| "Fix the BSCS" read as **show the full name wherever only the code showed** — the stored name was already correct | changing any stored value |
| Mobile screen: one program picker showing names, college derived | keeping the separate college picker |

---

## 6. Verified state, and what was NOT touched

**Untouched:** every recognition threshold, `LBPH_PARAMS`, the model,
`vision/`, `recognize_face.py`, `train_model.py`, the capture path.
`dataset/` and `trainer/` are still ignored (`SAFE`, two-path form).

**Every new guard was mutation-tested from a copy (L13), 19 of 19 caught:**
the program guard skipped at finish; the guard dropping its condition; the
guard refusing unlinked subjects; the section id one position left; the
placement not required; a deleted program writing anyway; the class list
unlimited; own section not first; add-section enrolling the whole program;
add-section on an unlinked subject; 010 linking an ambiguous name; 010 not
trimming; 010 not re-coding; 010 unplacing students of the re-coded program;
the register and the pickers reading the typed text; the section printed
after the subject name; the groups carrying no program; unlinked subjects
left out of the list. The working tree was compared byte for byte with its
state before the run.

⚠️ **Nothing was run in a browser.** Pages were rendered through
`app.test_client()` against the real database, and `static/js/students.js` was
driven in Node against stand-in elements — that checks which groups it hides
and which options it selects, **not** how a browser draws a `<select multiple>`
with hidden `<optgroup>`s. Chrome and Firefox hide them; where a browser does
not, `disabled` still keeps them out of the form.

⚠️ **The mobile change was not run.** No React Native harness here, the same
as Phase 7. It reads `structure` from `/api/colleges_programs`, which was
already being sent.

⚠️ **"Add all of the section" has not been seen on the live database** — every
student of both linked sections is already on those class lists, so the button
is not drawn. It is covered by an integration test against a scratch database.

⚠️ **One fast-suite run reported `1 failed, 1486 passed` and took 598 s**
instead of ~80 s. It was the last run of the session, started in the
background with only the final line of output kept, so **the failing test's
name was not captured**. Three runs immediately afterwards, on the same tree,
were `1487 passed` in 79–95 s. Not reproduced and not explained — the tenfold
duration points at the machine rather than the code, but that is a guess
(L15). If a timing-sensitive test fails for the next agent, this may be it.

**The five files modified before Phase 7 are still modified and still not this
work's:** `.gitignore`, `.vscode/settings.json`, `config/settings.py`,
`mobile/package.json`, `mobile/package-lock.json`.

---

## 7. Open

- **Commit** — nothing is committed. Keep the five files above out of it, or
  ask.
- **Look at `/students`, `/subjects` and a Class List in a browser.**
- **Run the mobile registration screen.**
- `todo.md` §11 open items 1–3: drop the two typed columns; the `BSIT`
  decision; the test-shaped section names on this database.
- Everything still open in `handover-phase-7.md` §7 except §7.7.2, which this
  sprint did.

---

## 8. Added the same day — the four text columns on `students` are filled again (DM-9)

**Asked for by the user**, looking at `students` in phpMyAdmin: 8 of 12 rows
had NULL in `college_department`, `program`, `year_level` and `section`, and
the other 4 said `College of Computing`. Nothing was lost — 009 stopped
writing those columns and every student resolved through `section_id` — but
the table read as if eight students had chosen nothing. Offered the drop
(`todo.md` §7.7.1), the user chose to have them filled.

| Change | Files |
|---|---|
| Migration **011** backfills the four columns from each student's section | `migrations/011_student_placement_text.sql` |
| `_copy_placement()` does the same after `insert()`, `update_details()` and `update_details_and_password()` | `repositories/students.py` |
| Three integration tests | `tests/integration/test_academic_structure.py` |

**Applied to the deployed database** (dump first:
`logs/db-backups/attendancesystem_db_before_011.sql`). Measured afterwards:
12 of 12 students have all four columns, 0 rows where the text disagrees with
the joins, `schema_migrations` at 011.

| Gate | Result |
|---|---|
| `pytest` (fast) | 1487 passed, 89 skipped |
| `pytest tests/integration` (scratch DB) | **86 passed** (was 83) |
| `ruff check .` | 18, unchanged |
| Mutations, from a copy (L13) | 4 of 4 caught — the first draft of the row-count test let one survive and was rewritten |

- ⚠️ **The columns are a copy and nothing reads them.** Every screen and the
  API still read the joins. Do not start reading the text.
- ⚠️ **`college_department` holds the department's full name**, the value the
  joins alias under that key — not the code. The user wrote "it should be
  CCIT"; taken as naming the college, not the stored string (my reading, L35).
  One word in `_copy_placement()` and in 011 if they meant the code.
- ⚠️ **A migration that renames a department, re-codes a program or renames a
  section must re-run 011's `UPDATE`.** No code path edits those rows today;
  010's re-code is the kind of change that would leave the copy stale.
- ⚠️ **`update_details()` returns the row count of its own `UPDATE`, read
  before the copy runs.** The edit route uses it to tell a no-op from a
  missing student, and the two counts differ both ways.
- **An Unassigned student keeps whatever text they had** (inner joins), NULL
  for one registered since 009. The columns are still nullable for that reason.
- **Not run in a browser.** The edit and registration routes were not driven;
  the repository functions they call were, against a scratch database.

**Correction to §0 and §7 above:** "nothing is committed" is no longer true.
Phase 7b is commit `96d62cd`; `git status` at the start of this work showed
only the five files of the user's. **This section's work is not committed.**

### 8.1 CCIT renamed (migration 012)

**Asked for by the user:** CCIT is the **College of Communication and
Information Technology**, not "Computing". Migration 012 renames the
`departments` row (same id 5, same code) and re-runs 011's copy, as §8 warned
a rename must. Applied to the deployed database after a dump
(`attendancesystem_db_before_012.sql`): 12 of 12 students carry the new name,
0 rows where the text disagrees with the joins. The mobile screen's offline
fallback list was changed to match (not run). Fast suite 1487 passed,
integration 86 passed, the missing re-copy caught as a mutation.

- 009 still seeds the old name; a fresh install gets the new one from 012.
- The user tested §8's registration and edit in a browser and reported it
  working. Their report, not re-run here.
