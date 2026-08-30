# Handover — Phase 5 → Phase 6

**Sprint:** Phase 5 — the web layer, the UX findings, and the last two pieces of
the refactor
**Date:** 2026-08-16
**Status:** ✅ **8 of 8, plus the two items `handover-phase-4b.md` §7 left
open.** The refactor is finished. What is left before Phase 6 is work only a
person can do — §6.
**Branch:** `phase-5-web-and-ux`, 7 commits
**Read with:** [`todo.md`](todo.md), [`lessons.md`](lessons.md) —
[L13](lessons.md) is new and came out of this sprint

---

## 1. Read this first — what will bite you

`handover-phase-4.md` §1 and `handover-phase-4b.md` §1 still apply and are
**not** repeated. Re-read them. Six things are new or changed.

### 1.1 ⚠️ The endpoint names all changed. The URLs did not

`app.py` is 161 lines and every route lives in a blueprint, so
`url_for('manage_students')` is now `url_for('students.manage_students')`.
Paths are byte-identical, so `href="/students"` still works and no bookmark
broke.

**The trap is that a stale endpoint name fails at *render* time, not at import
or start-up.** Four `url_for` calls spanned two lines, the regex that rewrote
the templates matched only single-line ones, and 852 tests plus a clean `ruff`
said nothing — the pages 500'd the moment they were loaded. Found by driving
them in a test client (L5, again).

`tests/test_template_endpoints.py` now resolves every `url_for` in every
template against `app.view_functions`. **If you add a template, that test
covers it automatically.**

### 1.2 ⚠️ Two source-level bans were about to scan nothing

`tests/test_db_access.py` listed `("app.py", "recognize_face.py")` with a
comment admitting the gap. After the split, `app.py` has no database access
left in it — so the ban would have been checking an empty file while eight
blueprints and seven repositories went unchecked. `test_dataset_paths.py` was
the same shape.

Both derive their file list from `tests/conftest.py::project_python_files()`
now. **If you add a package, add it to `SOURCE_PACKAGES` there** or it is
silently uncovered. Widening them surfaced two legitimate exceptions nobody had
thought about (`infra/db.py` *is* the pool; `eval_accuracy.py` uses SQLite),
which is the argument for deriving rather than listing.

### 1.3 ⚠️ A repository takes a cursor. It never opens one

This is the rule the whole `repositories/` package rests on, and it is about
transactions rather than tidiness. `db_cursor()` is **one transaction per
`with` block**. A repository that opened its own would put each statement in
its own transaction — and FS-4's absent register and FS-10's
correction-plus-audit-row would silently stop being atomic, with no visible
change at any call site.

`test_no_repository_opens_its_own_cursor` enforces it. It is mutation-tested.

### 1.4 ⚠️ A test that greps a template will match the comment about it

L12, in its most literal form yet. `templates/manage_students.html` now
contains the sentence *"This was `onsubmit="return confirm(...)"`"*, explaining
the US-5 bug it no longer has. A text search for `onsubmit=` matches that
prose.

`tests/test_templates.py` parses with `html.parser` throughout. Do not add a
grep-based check to it.

### 1.5 ⚠️ Do not undo a mutation test with `git checkout --`

New lesson [L13](lessons.md). I mutation-test every new assertion; for one of
them I reverted the mutation with `git checkout -- templates/subjects.html`,
which restored **HEAD** and silently discarded that file's uncommitted Phase 5
work. Caught only because the ban sweeps every template rather than the one
under test.

Copy the file aside and copy it back. `git stash` has the same trap and moves
everything else too.

### 1.6 ~~⚠️ MariaDB stopped on its own again — third occurrence~~

> ### ✅ Corrected 2026-08-16 — this was never a defect
>
> **XAMPP's MySQL does not start automatically after a Windows restart, and the
> user starts it by hand.** Every "occurrence" recorded here and in
> `handover-phase-4.md` §1.3 was a session that began after a reboot — which is
> also why no shutdown was ever logged: the process was not running to log one.
>
> Three handovers repeated this as an unexplained anomaly because each read the
> previous one as evidence. Nobody asked the person operating the machine. See
> [`handover-phase-5b.md`](handover-phase-5b.md) §1.6 and
> [L15](lessons.md). **Do not carry it forward, and do not put it in the
> thesis as a reliability finding.**

The original note follows, kept so the correction has something to correct.

Mid-sprint, `mysql_error.log` ends at a normal start-up line with **no shutdown
recorded**. Same as `handover-phase-4.md` described. Restarted with
`C:\xampp\mysql\bin\mysqld.exe --defaults-file=C:\xampp\mysql\bin\my.ini`;
nothing was lost and the row counts were identical afterwards.

If a test run suddenly reports `2003 (HY000): Can't connect`, this is why.
Check `netstat -ano | grep 3306` before assuming your change broke something.

---

## 2. Verified current state

Every number below was measured today, on this branch, in this order.

| Check | Result |
|---|---|
| `ruff check .` | **clean** |
| `pytest tests/ -q` | **1,101 passed, 40 skipped** (647 at the branch point) |
| `pytest tests/integration/ -q` (gated) | **37 passed** (29 at the branch point) |
| `eval_heldout_accuracy.py` | **60/60, avg distance 34.95** — unchanged since Phase 0 |
| `eval_accuracy.py` | **300/300** |
| `scripts/migrate.py --status` | **0 pending** |
| Schema version | `schema_migrations` at **006** — **this sprint added no migration** |
| Database | **MariaDB 10.4.32**, XAMPP, `attendancesystem_db` |
| Live rows | students **4** ⚠️ *corrected — see below*, subjects **1**, attendance **0**, enrolments **0**, attendance_sessions **0**, attendance_audit **0**, instructors **0**, admin **1** |
| `git check-ignore dataset dataset_staging trainer certs` | 4 matches — SAFE |
| Routes | 46 rules |
| Python | 3.11.5 |

**The live row counts are byte-identical to `handover-phase-4b.md` §2.**

### ⚠️ Correction, 2026-08-16 (R13) — students is 4, and always was

The table above originally read **students 5**. Measured directly against
`attendancesystem_db` while clearing the defect register:

```
students 4, subjects 1, attendance 0, enrolments 0,
attendance_sessions 0, attendance_audit 0, instructors 0, admin 1
```

`dataset/` holds exactly four folders — `12345`, `23-1-1-0559`, `23-1-1-0918`,
`23-1-1-0920` — one per row, so nothing went missing: **the 5 was carried
forward, not measured.** It appears in `handover-phase-4.md` §2, again in
`handover-phase-4b.md` §2, and this document's own sentence directly above
asserting the counts were "byte-identical" to 4b is what let it through — the
figure was checked for *agreement with the previous handover* rather than
against the database.

That is [L8](lessons.md) exactly, in the document L8 was written to protect:
inherited claims are evidence, not axioms. It is also why L8 asks a handover to
mark which numbers were re-measured. Every other number in this table was, and
this one had been quietly inherited across three sprints.

### Code size, before and after

| | Phase 4b | Phase 5 |
|---|---:|---:|
| `app.py` | 2,890 | **161** |
| `capture_dataset.py` | 1,586 | **deleted** |
| `web/` | — | 2,265 |
| `repositories/` | — | 884 |
| `services/` | — | 371 |
| `static/js/` | 0 | 980 |

### Re-establish the baseline

```bash
git checkout phase-5-web-and-ux
.venv/Scripts/python.exe -m ruff check .
.venv/Scripts/python.exe -m pytest tests/ -q                # 1101 passed, 40 skipped
INTEGRATION_DB_NAME=test_attendance_scratch \
  .venv/Scripts/python.exe -m pytest tests/integration/ -q  # 37 passed
.venv/Scripts/python.exe eval_heldout_accuracy.py           # 60/60, avg 34.95
.venv/Scripts/python.exe scripts/migrate.py --status        # 0 pending
```

All five were run today in that order and produced those numbers.

---

## 3. What changed

| commit | what |
|---|---|
| `8e521d6` | Delete the server-side capture path (MA-2, CO-1, CO-2, PO-6) |
| ⭐ `86fc94d` | Name dataset folders by student ID alone (§7.5) |
| ⭐ `6b06cca` | Split `app.py` into blueprints, services and repositories (MA-1) |
| `955d7f2` | Rewrite the Excel export without pandas (PE-8, FS-11) |
| ⭐ `1d72b90` | One layout, real confirmations, no JavaScript in the templates |
| `5897d31` | Serve over HTTPS, and make the Secure cookie follow it (CO-3, SE-11) |
| this one | Documentation |

### ⭐ The `dataset/{student_id}` migration, and what came out of it

The clearest evidence that the old scheme was a real coupling is what the
migration *deleted* from `/update_student`: seventy lines containing an
`os.rename()` under a database cursor, a hand-written compensating rename in
the `except` branch to undo it when the UPDATE failed, a 409 for a colliding
folder name, and a 500 explaining that File Explorer might be holding a
directory open. Every one of those existed to keep a *display name* and a
filesystem path in step. A rename is one UPDATE now.

`labels.txt` is `{label},{student_id}` and the display name comes from the
`students` table at model load. An un-migrated `labels.txt` is **refused with
the offending line quoted**, rather than loading: taken as an ID,
`23-1-1-0559_Chrizol D. Evangelista` matches no student row, so every
recognition of that person would have been refused as "not enrolled" by a model
that loaded cleanly and logged nothing.

**Retrained and re-verified: 60/60, avg 34.95, model byte-size identical.**

### ⭐ MA-1

```
web/          8 blueprints - HTTP only
services/     enrolment slot, training job, session lifecycle, reporting
repositories/ every SQL statement; each takes a cursor, none opens one
```

Three things came *out* of the split rather than going into it, and each was a
duplication the old file hid:

* **`EnrolmentSlot`** — a capture is three resources (session, MediaPipe
  detector, staging folder) that `app.py` released through two module-level
  helpers, each having to remember all three, differing by one line 1,500 lines
  apart. `abandon()` throws the images away; `release()` keeps them.
* **`dataset_store.remove_folder()`** — `app.py` carried a byte-identical copy
  of the read-only rmtree hook beside the store's. That is the MA-4 shape.
* **`_class_list_form()`** — the two class-list routes validated identically
  and one of them was missing its log line.

### ⭐ The UX commit

US-1, US-2, US-3, US-5, US-6, US-7 and US-8 landed together because they are
one problem: twenty templates each carrying their own document shell, which is
what made US-1 impossible to fix *once*.

**US-5 is worth understanding rather than skimming.** It is not a broken
confirmation, it is a *missing* one:
`onsubmit="return confirm('Delete {{ student.name }}?')"` renders for `O'Brien`
as a JavaScript syntax error, so the handler never compiles, so `onsubmit` is
absent, so the delete submits **with no confirmation at all** — on exactly the
records where a mis-click is least recoverable.

---

## 4. Corrections

### 4.1 — to `todo.md` §4's target architecture

It shows everything under `src/attendance/`. **Phase 5 did not do that, by the
user's decision**, and the note in §4 saying the tree is out of date now covers
the layout as well as the recognition backend. The layers exist; the prefix
does not. `pyproject.toml` records why.

### 4.2 — to my own first attempt at the export

The commit that closed PE-8 originally used a temporary file deleted by a
`response.call_on_close` hook. **The hook does not fire under the test client**,
so every export left a complete attendance register in the system temp
directory. The integration test written for exactly that caught it, and the
export is an in-memory buffer now. A cleanup path that can silently not run is
worse than not needing one.

### 4.3 — "streamed export" is not claimed anywhere

`todo.md` Phase 5 asked for a streamed download. An `.xlsx` is a ZIP container
whose central directory is written last, so **no implementation can hand out
bytes while the sheet is still being produced**. What is bounded is the row
buffer (openpyxl write-only) and the query (a server-side cursor). The
docstrings say that rather than the word "streamed", because the word would be
a claim about a file format.

### 4.4 — CO-1 and PO-6 are narrowed, not closed

Earlier documents said deleting `capture_dataset.py` would close them.
`camera_utils.py` keeps `cv2.CAP_DSHOW`, because **recognition still opens the
classroom camera on the server** — which is by design and was never in scope to
change. The honest statement is: the server must be Windows; the clients need
not be.

### 4.5 — US-6 is a review, not an audit

No accessibility tool was run and no assistive technology was used. The changes
are real and were made against WCAG 1.4.1 / 2.4.7 / 1.3.1 by inspection. **The
OpenCV overlay is untouched and cannot be fixed with ARIA** — it is pixels in
an MJPEG stream, and the answer for it is the live list (US-3).

---

## 5. What the new tests protect

| Finding | Protected by |
|---|---|
| MA-1 | `test_db_access.py` — no repository opens a cursor; no module outside four named exceptions connects by hand |
| §7.5 | `test_label_map.py` — an old-format `labels.txt` is refused; the display name is resolved from the database, **and the loader is proven to call the resolver** |
| US-5 | `test_templates.py` — no inline event handler anywhere, **and** the five destructive forms still carry `data-confirm` |
| US-8 | `test_templates.py` — no inline `<script>` in any template |
| US-1 | `test_templates.py` — every page extends the layout, and the layout renders flashed messages |
| US-6 | `test_templates.py` — skip link, `#main`, `<nav>` with a name, alt text on every image |
| MA-1 (routing) | `test_template_endpoints.py` — every `url_for` resolves, and none is unqualified |
| SE-3 | `test_dataset_paths.py` — swept over every module, plus a new ban on `settings.dataset_dir / student_id` |
| PE-8/FS-11 | `tests/integration/test_export.py` — filters honoured, `timedelta` survives the round trip, **nothing left on disk** |
| CO-3/SE-11 | `test_tls_settings.py` — cert and key are a pair, and `Secure` follows TLS |

**Every one of these was mutation-tested before being trusted**, and one
mutation found a real gap rather than confirming a test: replacing
`_attach_display_names(...)` with `pass` left all ten label tests green, because
each called the function directly and none asserted that the loader *calls* it.

---

## 6. Handed to you — what only a person can do

**Nothing in the refactor is waiting on these, and they are now the critical
path.** In this order:

1. **Populate a class list.** `enrolments` is still **0 rows** on the live
   database. Until somebody uses Subjects → Class List, a session records
   nothing at all — this has been the first item on three consecutive
   handovers.
2. **A live browser enrolment run.** Camera permission, all nine stages at a
   real distance, cancel mid-way leaving nothing behind, a second tab getting
   409. ⚠️ **`capture_dataset.py` is gone, so this is the only enrolment path.**
   That was your decision and it is the right one, but it means a failure here
   blocks enrolment until it is fixed rather than falling back.
3. **A live recognition run**, to confirm FS-14 actually lets a real student
   mark attendance. Outstanding since `handover-phase-3b.md`. **The live list
   (US-3) is what makes this observable** — you should see names appear while
   the session is running, not after ending it.
4. **A remote enrolment over HTTPS**, from a second machine. This is what
   closes CO-3. Run `python scripts/make_dev_cert.py`, put the two paths in
   `.env`, and accept the warning once per browser. Verified from this end:
   the server serves HTTPS and sets `Secure`. Not verified: that a browser
   accepts *this* certificate and hands over the camera.
5. **`tasks/notes.txt` is still uncommitted.** Six handovers have now left it
   alone because it is yours.

---

## 7. What is NOT done

- **PO-3** — `selected_camera.txt` is still a root-level text file.
  `active_session.txt` was deleted this sprint (it was dead), which is half of
  it. Not in any phase's scope yet.
- **PO-4** — still the Flask development server. TLS is configured on it, which
  is not the same as a WSGI server behind a reverse proxy.
- **SE-9's implementation half** — no encryption at rest for `dataset/` and
  `trainer/`, no consent record in the schema, no automated retention. The
  export no longer *adds* to the problem, which is not the same as solving it.
- **US-6 beyond the web UI** — see §4.5.
- **CO-3** — see §6.4.

---

## 8. Open decisions

**Nothing in Phase 6 is blocked on a decision.** Three were taken this sprint,
all with the user on 2026-08-16:

1. ✅ **Root packages (`web/`, `services/`, `repositories/`), not
   `src/attendance/`.** §4's tree describes the layers, not the paths.
2. ✅ **`capture_dataset.py` deleted now**, rather than after a live browser
   run. Accepted consequence: no fallback enrolment path — see §6.2.
3. ✅ **The `dataset/{student_id}` migration done now**, before the Phase 6
   recapture, so the new data lands in the final scheme rather than being
   migrated twice.

Everything in `handover-phase-4b.md` §8 still stands.

---

## 9. Suggested first move

**Phase 6 is evidence, and its cheapest item does not need the recapture.**
FAR is measurable today against the current model: the Georgia Tech impostor
set is on disk at `gt_db/`, and 750 of its images were already scored at 62.2
(closest) during the FS-14 investigation — that number is in
`docs/benchmarks.md` §4a and it is a real open-set result waiting to be written
up properly.

The recapture is needed for the FRR side and the DET curve. **Sequence it after
§6.2**, because the recapture *is* the acceptance test for browser enrolment —
one exercise, two results. And collect the written consent first (todo.md
Phase 6, first item); nothing in the schema records it, so it is paper, and it
happens before the capture session rather than after.

⚠️ **Before quoting any accuracy figure**, read `docs/limitations.md` and
`docs/walkthrough.md` §4–§5. The 60/60 is a same-session split and cannot be
quoted without its four caveats.

The user is running these phases in separate sessions and asked for this
document to bridge them. Assume the next reader has no memory of this one.
