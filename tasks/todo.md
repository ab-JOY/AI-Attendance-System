# AI Attendance System — Codebase Audit & Refactoring Plan

**Audit date:** 2026-08-07
**Scope:** Full codebase (excluding `.venv/`)
**Evaluation frame:** ISO/IEC 25010:2023 product quality model
**Status:** Phases 0–4 complete · **Phase 5 complete (2026-08-16, 8 of 8)** ·
Phase 6 outstanding
**Last updated:** 2026-08-29 (FS-16, US-10 and US-11 — all three from live runs)

> **How to read this document.** §2's findings tables are the original audit
> with each row struck through and annotated as it was fixed, so the history
> stays legible; a row with no annotation is still open. §5 is the phased plan
> with the same convention. §7 records decisions the user has made. §8 is the
> per-phase review.
>
> **Start with the newest `handover-*.md` instead if you are picking this up
> cold** — it carries the verified current state and the traps. This file is
> the plan; the handover is the briefing.

---

## 0. Headline: the system was non-functional and could not self-heal

> ✅ **Resolved in Phase 0 (2026-08-08). Kept as the audit record, in the
> present tense it was written in.** The root cause turned out to be deeper
> than the interrupted write described below — `neighbors=12` made the model
> both unpersistable and unmatchable (PE-0). The system has been demonstrable
> since; held-out accuracy has read **60/60, avg 34.95** through every phase
> since, which is the evidence each one was recognition-neutral.

Three verified facts, in causal order:

1. `dataset/test-id_test student/` contains **0 images**.
2. `train_model()` requires ≥70 usable images per folder and **returns `False` for the whole run** if any single folder falls short ([train_model.py:273-284](../train_model.py#L273-L284)). So **every retrain attempt now fails** — via the UI, via `/train_model`, and via CLI.
3. `trainer/labels.txt` **does not exist**, while `trainer/trainer.yml` exists at **878 MB**. `load_model_and_labels()` raises `FileNotFoundError` on the missing labels file ([recognize_face.py:72-75](../recognize_face.py#L72-L75)), so `start_camera()` returns `False` and **no attendance session can start**.

The cause of (3) is a non-atomic write: `train_model()` deletes both artifacts *before* writing new ones ([train_model.py:320-340](../train_model.py#L320-L340)). A run that dies during the multi-minute `recognizer.write()` leaves the model destroyed and irrecoverable — which is exactly the current state, and (2) means the recovery path is also closed.

> **Confirmed by the Phase 0 retrain.** A complete model for this dataset is **1.83 GB**. The 878 MB file found on disk was a **partial write, truncated at 48%** — direct physical evidence that `recognizer.write()` was interrupted mid-stream, exactly as diagnosed. All model-size figures below use the true 1.83 GB.

**A live demo is not possible today.** Fixing this is Phase 0 and takes under an hour.

---

## 1. Verified system inventory

| Area | Detail |
|---|---|
| Backend | Flask 1 file, 1466 lines, 40 routes, no blueprints |
| Recognition | MediaPipe FaceMesh + OpenCV LBPH, 1817 lines |
| Enrolment | `capture_dataset.py`, 1983 lines, **all logic at module scope** |
| Database | MySQL, 5 tables, no foreign keys, plaintext passwords |
| Dataset | 3 real students × 100 images + 1 empty folder |
| Model | LBPH `radius=2, neighbors=12, grid 8×8` → 262,144-bin histograms × 1200 = **1.83 GB** |
| Tests | 4 `test_*.py` files — all print-only scripts, **zero assertions**, not pytest-collectable |
| Version control | **Not a git repository.** No `.gitignore`. |
| Dead weight | `hello_flutter/` (stock counter demo, unrelated), `.venv/` committed, generated CSV/XLSX/DB at repo root |

---

## 2. Findings by ISO/IEC 25010:2023 characteristic

Severity: **P0** blocker · **P1** critical · **P2** major · **P3** minor

### 2.1 Functional Suitability

| ID | Sev | Finding | Evidence |
|---|---|---|---|
| FS-1 | P0 | Recognition cannot start — `labels.txt` missing | [recognize_face.py:72](../recognize_face.py#L72) |
| FS-2 | P0 | Retraining hard-blocked by one empty dataset folder; one bad folder fails all students | [train_model.py:273](../train_model.py#L273) |
| FS-3 | P1 | **No enrolment model.** `/end-attendance` marks *every student in the database* absent for *every subject*. There is no student↔subject relation anywhere in the schema. **Demonstrated 2026-08-08:** with subject `CS401` (BSCS/section B), the unrelated `test-id` student was reported Absent for it. ✅ **Fixed 2026-08-11 (Phase 4).** `enrolments` (migration 002) is the missing relation, the Class List screen populates it, and `/end-attendance` now scopes the register to it. Verified against the deployed database: an unenrolled student is refused and does not appear in the register at all. | [app.py:1200-1240](../app.py#L1200-L1240), [schema.sql:35-45](../database/schema.sql#L35-L45) |
| FS-4 | P1 | **"Absent" is never persisted.** `/end-attendance` computes absences in memory only; `/reports` counts `status == 'Absent'` from the `attendance` table, which will always be **0**. Two screens report contradictory numbers. **Demonstrated 2026-08-08** on the same simulated session: `/end-attendance` → `Present=2 Absent=2`; `/reports` for that same subject → `Present=2 Absent=0`. ✅ **Fixed 2026-08-11 (Phase 4).** Absent rows are written on session end by one `executemany` inside a single transaction. `/reports` counts them because they now exist; `total_absent` was structurally 0 before. | [app.py:1216-1219](../app.py#L1216-L1219) vs [app.py:1313](../app.py#L1313) |
| FS-5 | P1 | **Dashboard is fake** — Total Students, Total Subjects, Today's Attendance, Active Sessions are hardcoded `0` in the template. ✅ **Fixed 2026-08-11 (Phase 4).** The route had no database access whatsoever - four lines returning a render - so this was a route change rather than a template fix. A failed counter query logs and renders zeros instead of an error page, because a dashboard that cannot count is still usable navigation. | [dashboard.html:25-45](../templates/dashboard.html) |
| FS-6 | P1 | ~~`/edit_instructor/<id>` renders `edit_instructor.html`, which **does not exist**~~ ✅ **Fixed 2026-08-08 (Phase 2)**, in passing — a route with authentication added that still 500s for the legitimate user is not meaningfully secured. Two further faults on the same screen were fixed with it: the template's `url_for` passed `id` where the route takes `instructor_id` (a `BuildError`), and it rendered `instructor.username`, a column the schema has never had. | [app.py](../app.py), [edit_instructors.html](../templates/edit_instructors.html) |
| FS-7 | P2 | Subject code is a **free-text field typed twice** (start + end of session), not a dropdown bound to the `subjects` table. A typo silently creates an orphan attendance session that no report will find. ✅ **Fixed 2026-08-11 (Phase 4).** Both boxes are dropdowns bound to `subjects`. Forced rather than chosen: attendance keys on `subject_id` now, and a typed code cannot identify an offering when two sections share one. | [attendance.html:50-77](../templates/attendance.html#L50-L77) |
| FS-8 | P2 | No late/absent policy despite `subjects.time_in`/`time_out` existing and `admin_panel.py` offering a "Late" status. Everything recorded is `"Present"`. ✅ **Fixed 2026-08-11 (Phase 4).** Migration 005 makes `subjects.time_in` a real TIME and `derive_status()` uses it. **The system recorded its first ever non-"Present" status.** | [recognize_face.py:1296](../recognize_face.py#L1296) |
| FS-9 | P2 | `capture_face` **inserts the student row before capture succeeds**. Cancel the capture (ESC) and you get a student with no dataset — which then permanently breaks training (FS-2). This is how the current outage was created. ✅ **Fixed 2026-08-11 (Phase 4).** `infra/dataset_store.py`: images are staged, promoted only when complete, and the student row is written *after* that. A test arms the database with a tripwire and drives a capture to prove nothing is written during one. | [app.py:180-231](../app.py#L180-L231) |
| FS-10 | P2 | ~~No attendance edit/override UI. A false negative cannot be corrected by the instructor.~~ ✅ **Fixed 2026-08-15 (Phase 4b).** `/attendance/<id>/correct`. **`@authenticated` rather than admin-only** (decided with the user): the finding is about *the instructor*, who is the person who watched the miss happen. **The audit trail is what makes that safe to open up.** Migration 006 created `attendance_audit` in Phase 4 and nothing wrote to it; it does now, and the write that changes the status and the write that records it are **one transaction**, so a correction that is not recorded cannot happen. An integration test injects an audit-insert failure and asserts the register is unchanged afterwards; an `ast` test asserts the two statements stay in one `db_cursor` block, because splitting them leaves both working and silently removes the property. ⚠️ **The reason's length is checked in Python, not by the column** — no `STRICT_TRANS_TABLES` means `VARCHAR(255)` truncates instead of refusing, and a half-stored explanation of why a biometric determination was overridden is a corrupt record reporting success (L11). ⚠️ **`time_in` is never written:** correcting an Absent to Present would have to invent an arrival time, and fabricated evidence is worse than an obviously untouched field. | [app.py](../app.py), [templates/correct_attendance.html](../templates/correct_attendance.html) |
| FS-11 | P3 | `/export_excel` ignores all report filters and always exports the entire table to a fixed filename. ◧ **Partly fixed 2026-08-11 (Phase 4).** The export honours the report filters and writes a timestamped file into `reports/` rather than a fixed name at the repository root. The download is still not streamed and PE-8 is untouched. | [app.py:1332-1360](../app.py#L1332-L1360) |
| **FS-13** | P2 | ~~**The Delete button on the Students page did nothing.**~~ ✅ **Fixed 2026-08-08 (Phase 2).** `templates/students.html:134` linked to `delete_student` with an `<a href>` (GET) while the route is POST-only, so it answered **405**. Two divergent delete UIs existed and only `manage_students.html`'s worked. Now a POST form with a CSRF token, matching the other page. | [students.html](../templates/students.html) |
| **FS-14** | **P0** | ~~**A recognised student could never be marked present if their distance landed between 52.0 and 58.0.**~~ ✅ **Reported by the user from a live run and fixed 2026-08-11 (Phase 4).** The overlay read `Verifying 20/20 100% (56.1)` indefinitely: twenty of twenty frames agreeing unanimously on the right student, nothing recorded, nothing logged, no error anywhere. Two thresholds on one LBPH distance scale, written down separately — a frame votes at or under `RECOGNITION_THRESHOLD` (58.0), but the window *average* had to beat `TrackConfig.confirmation_confidence` (52.0) to lock the identity — so the band between them was recognised-every-frame and unmarkable-forever. **Invisible to the evaluator by construction:** held-out scores same-session crops at avg 34.95 and never reaches the band; only a live face in a real room does, which is the §3 leakage caveat becoming an outage instead of a footnote. **Measured before changing anything** (`docs/benchmarks.md` §4a): 750 impostor images from 50 unenrolled Georgia Tech subjects score **62.2 at the closest**, lowest subject mean 69.9, so the band protected against nothing that exists. `confirmation_confidence` is now derived from `RECOGNITION_THRESHOLD` instead of restated, `can_confirm()` is expressed through a new `confirmation_blocker()` that names the unmet condition, a stuck track logs once and tells the operator "Match too weak - move closer / more light", and a test fails if a gap ever reopens. Held-out unchanged at 60/60. Third instance of this shape after L2 and SE-12 — see [`lessons.md` L10](lessons.md). | [vision/tracking.py](../vision/tracking.py), [recognize_face.py](../recognize_face.py) |
| **FS-15** | P2 | ~~**A failed recapture destroyed the student's existing dataset.**~~ ✅ **Found and fixed 2026-08-11 (Phase 4)** while diagnosing the 500 the user reported from `/recapture_face`. The route `shutil.rmtree()`s the existing folder and *then* runs the capture subprocess, so any failure to capture left a student with no images at all — and the capture cannot start while an attendance session holds the camera, because it runs in a separate process. The operator saw only "Face capture did not complete. See the log for details." on a blank 500. Both enrolment routes now refuse with a **409 and a sentence naming the cause** before anything is deleted. **This is a mitigation, not the fix:** the whole subprocess design is what makes the camera contendable, and browser enrolment removes it (CO-3, §7 Q2). | [app.py](../app.py) |
| **FS-12** | **P1** | ~~**A fatal failure during enrolment is reported to the operator as success.**~~ ✅ **Fixed 2026-08-08.** Every bare `sys.exit()` in `capture_dataset.py` exited with status **0** — missing `face_preprocessing`, missing arguments, an empty student ID, an unopenable camera, and a camera read error part-way through all exited "cleanly". `app.py` treats 0 as success and runs an automatic retrain, so the operator saw a completed enrolment for a student with no usable dataset, feeding FS-2/FS-9. **Root cause was an implicit contract:** the exit codes were literals on both sides and nothing tied them together. Now `config/exit_codes.py` defines `EXIT_SUCCESS`/`EXIT_FAILURE`/`EXIT_CANCELLED` and both modules import it; the code is decided in one place after cleanup, and **only a complete capture (`count == MAX_IMAGES`) reports success**. Covered by `tests/test_capture_exit_codes.py`, including a ban on bare `sys.exit()`. | [capture_dataset.py](../capture_dataset.py), [config/exit_codes.py](../config/exit_codes.py), [app.py:242](../app.py#L242) |
| **FS-16** | **P1** | ~~**Ending a session with the wrong subject selected is refused by a guard that never runs.**~~ ✅ **Fixed 2026-08-29.** Reported by the user 2026-08-29. `end_attendance()` compares the submitted `subject_id` against the running session's and answers 409 (R3) - but `static/js/attendance.js` posts `/stop_camera` **first** and submits the form only in its `.then()`, and `RecognitionSession.stop()` sets `_subject = None`. So `active` is **always** `None` by the time the route reads it: the comparison is skipped, the form's subject is trusted, and the mis-selection goes through exactly as it did before R3 was written. **Reproduced 2026-08-29** by driving both routes in the browser's order (`/stop_camera`, then the form) against the real `RecognitionSession.stop()` behaviour: **HTTP 200**, register read for the subject that was *not* taught, absence row written as `('…', 2, None)` — wrong subject, `session_id` NULL — and `close_session` never called, so the running session's row keeps `ended_at IS NULL` and the dashboard's Active Sessions counter shows it for the rest of the day. The class that **was** taught gets no register at all. **The test cannot see it** - `tests/test_end_attendance_subject.py` sets `FakeSession.subject` and posts the form directly, never reproducing the stop-then-submit order that is the only order production has. The guard was correct; where it read its state was not (`lessons.md` L18, and now **L28**). **The fix is server-side, not just the deleted fetch:** the authority is now the open `attendance_sessions` row - written before the camera opens, so it survives a stop, a restart and a different operator's browser - and the form is trusted only when nothing is open. That also stamps the absent rows with the real `session_id` and closes the right session on the restart path, which used to write NULLs and leave the row open. The pre-flight `/stop_camera` is gone from the page as well, so an ordinary end no longer takes the recovery path. Four new tests: two driving both routes in the browser's order (both fail against the old route), two banning the fetch's return, comment-stripped per L12/L21. **1255 passed, 50 skipped; ruff clean.** | [web/sessions.py:202](../web/sessions.py#L202), [static/js/attendance.js:229-247](../static/js/attendance.js#L229-L247), [vision/session.py:395](../vision/session.py#L395) |

### 2.2 Performance Efficiency

| ID | Sev | Finding | Evidence |
|---|---|---|---|
| **PE-0** | **P0** | **The model OpenCV writes at `neighbors=12` cannot be read back by OpenCV.** `recognizer.write()` succeeds (1.835 GB), `recognizer.read()` then dies on `persistence.cpp:1613 (-215) ofs == fs_data_blksz[blockIdx]` in `cv::FileStorage`. Measured ceiling is between 0.57 GB and 1.84 GB. **This is a correctness blocker, not a performance one: the configured pipeline cannot produce a usable model at 3 students.** See §2.9. | Phase 0 diagnostic |
| PE-1 | P0 | **`neighbors=12` produces 4096-bin cell histograms (262,144 dims/image).** Verified in the model header. With 24×24 effective cells, **at most 576 of 4096 bins per cell can ever be non-zero — ≥86% of the model is structurally zero.** The parameter choice is not merely slow, it is statistically degenerate: chi-square distance is computed over overwhelmingly empty bins. | `trainer.yml` header: `cols: 262144` |
| PE-2 | P0 | **Model size scales linearly with students: 1.83 GB for 3.** ~611 MB/student → 30 students ≈ **18 GB**, 100 students ≈ **61 GB**. The system cannot scale past a handful of enrolees, and a single class already exceeds practical limits. ⚠️ **Mitigated, and now permanently so.** At `neighbors=8` the cost is **~18.3 MB/student** (55 MB for 3, measured), which is 33× better but still linear. **The user decided on 2026-08-08 to keep LBPH** (§7 Q1), so this will not be eliminated. It becomes a **stated limitation of the system**: the `cv::FileStorage` read ceiling sits between 0.57 GB and 1.84 GB (§3a), putting the hard limit at roughly **31–100 students — use 31 as the planning figure**. Exceeding it reproduces the Phase 0 outage exactly: OpenCV writes a model it then cannot read. **Do not "fix" this in Phase 3; measure it and write it up in Phase 6.** | measured (Phase 0 retrain) |
| PE-3 | P1 | `recognizer.predict()` compares the query against **all 1200 stored histograms × 262,144 dims ≈ 314M float ops, per face, per frame.** This runs inside the MJPEG generator loop. | [recognize_face.py:1555](../recognize_face.py#L1555) |
| PE-4 | P1 | `load_model_and_labels()` runs **at import time** (so Flask startup parses 1.83 GB of YAML) **and again on every `start_camera()`**. Multi-minute startup, multi-GB RSS. | [recognize_face.py:166](../recognize_face.py#L166), [recognize_face.py:911](../recognize_face.py#L911) |
| PE-5 | P1 | `train_model()` runs **synchronously inside the HTTP request** after every capture. The request blocks for minutes with no progress feedback and will hit any proxy/browser timeout. | [app.py:219](../app.py#L219) |
| PE-6 | P2 | `CameraReader._reader()` is a **tight unthrottled loop** with no sleep and no frame-ready signalling — it spins a core at 100% and re-copies frames the consumer never reads. | [recognize_face.py:300-305](../recognize_face.py#L300-L305) |
| PE-7 | P2 | New MySQL connection opened **per recognised face event** in `save_attendance()`; no pooling anywhere in the app. | [recognize_face.py:795](../recognize_face.py#L795) |
| PE-8 | P2 | ~~`pd.read_sql` given a raw `mysql.connector` object (unsupported by pandas; DBAPI2 fallback, emits warnings) and materialises the entire attendance table into memory.~~ ✅ **Fixed 2026-08-16 (Phase 5).** openpyxl in **write-only** mode from a server-side cursor: each row is serialised to the sheet's XML stream and dropped rather than held in a cell grid. 5,000 rows produce a 72 KB workbook. **pandas is removed from the dependencies** — one call site, and 1.94 s of import cost against openpyxl's 1.43 s (median of five cold imports). ⚠️ "Streamed" is *not* claimed: an .xlsx is a ZIP whose central directory is written last, so no implementation can hand out bytes mid-sheet. What is bounded is the row buffer. | [services/reporting.py](../services/reporting.py) |
| PE-9 | P3 | Training augmentation ×4 in memory with no batching — `faces` list holds every augmented 200×200 image at once. | [train_model.py:267](../train_model.py#L267) |

### 2.9 Hardware acquisition *(new section, Phase 3)*

| ID | Sev | Finding | Evidence |
|---|---|---|---|
| **CAM-1** | **P1** | ~~**A camera that opens, reads, and shows nothing is accepted as working.**~~ ✅ **Found and fixed 2026-08-09 (Phase 3).** `open_camera_by_index()` checked only `ret and frame is not None`. The deployment machine's built-in webcam (`HD User Facing`, index 0) opens, reads successfully, and returns **black** — and `selected_camera.txt` named it. So `start_camera()` returned True, `/video_feed` streamed black, MediaPipe found no faces, and the operator sat in front of "No valid face detected" indefinitely **with nothing in any log**. The scan made it worse: index 0 was accepted, so no later index was ever reached. Now a device is probed over nine frames and judged on the **median**. **Two false starts, both corrected by running it against the real hardware:** judging on the *best* frame opened the dead camera (a noise burst had Laplacian variance 1258, twice the working camera's 618), and judging on variation and edge content alone also opened it (a second probe of the same device gave median std dev 5.2 and detail 138.7, clearing both) — only a brightness floor rejects it, at median mean 0.3. Deliberately conservative: a dim room is a bad picture and must still open, and a test asserts that. | [camera_utils.py](../camera_utils.py), [tests/test_camera_selection.py](../tests/test_camera_selection.py) |

### 2.3 Compatibility

| ID | Sev | Finding | Evidence |
|---|---|---|---|
| CO-1 | P1 | **`cv2.CAP_DSHOW` is Windows-only.** Camera acquisition fails outright on Linux/macOS. ◨ **Narrowed 2026-08-16 (Phase 5), not closed.** `capture_dataset.py` is deleted, so the Windows-only *enrolment* path is gone — enrolment is `getUserMedia` in the operator's browser and runs anywhere. `camera_utils.py` keeps `CAP_DSHOW` because **recognition still opens the classroom camera on the server**, which is by design. So CO-1 now reads: the server must be Windows; the clients need not be. | [camera_utils.py:30,41](../camera_utils.py#L30) |
| CO-2 | P1 | ~~`ctypes.windll` for window placement — Windows-only (guarded, but the whole capture UX assumes Windows).~~ ✅ **Fixed 2026-08-16 (Phase 5).** `capture_dataset.py` is deleted; there is no server-side capture window to place. | — |
| CO-3 | P1 | **The architecture is single-machine only.** ⚠️ **Resolved by the §7 Q2 decision (2026-08-08): enrolment moves into the browser.** The capture path that contains this is being replaced in Phase 5, not refactored. `/capture_face` spawns an OpenCV GUI window *on the server*. If the browser is not on the server desktop, the operator sees nothing and the request hangs until someone at the server presses ESC. ◧ **Reduced 2026-08-11 (Phase 4), not closed.** Enrolment runs in the operator's browser, so the server-side OpenCV window is no longer how a student is captured - but only over `http://localhost`, because `getUserMedia` needs a secure context and TLS was deliberately out of scope. Enrolling from another machine still does not work, and recognition still opens the camera on the server by design. | [app.py:209](../app.py#L209) |
| CO-4 | P2 | `mediapipe==0.10.14` pins the project to Python ≤3.11 (venv is 3.11). No documented interpreter constraint. | [requirements.txt:5](../requirements.txt#L5) |
| CO-5 | P2 | MySQL-specific SQL (`CURDATE()`) while the test harness uses SQLite with a *different* schema — tests never exercise production queries. | [app.py:1188](../app.py#L1188), [test_accuracy.py:26-48](../test_accuracy.py#L26-L48) |
| CO-6 | P3 | `hello_flutter/` is the unmodified Flutter counter demo — no integration point, pure dead weight. | [main.dart:14](../hello_flutter/lib/main.dart#L14) |

### 2.4 Usability

| ID | Sev | Finding | Evidence |
|---|---|---|---|
| US-1 | P1 | ~~Errors return **raw text on a blank 500 page** with no navigation — every failure is a dead end. ~15 occurrences.~~ ✅ **Fixed in two halves.** Phase 2 replaced the raw text with `error.html` and navigation; **Phase 5 (2026-08-16) added the flash-message system that was the other half** — every outcome that was not a failure had been *silent*, so a form that had quietly done nothing looked exactly like one that had worked. `templates/base.html` renders the region, and twelve confirmations name what happened **and what did not** (unenrolling keeps attendance; deleting an instructor keeps their subjects). | [templates/base.html](../templates/base.html) |
| US-2 | P1 | ~~No flash-message system, no success confirmations, no loading indicators for the two multi-minute operations (capture, training).~~ ✅ **Fixed 2026-08-16 (Phase 5),** the training indicator in Phase 3. `alert()` is gone from the attendance page — modal, unreadable to a screen reader until dismissed, and gone the moment it is, so "a session is already running" had to be *remembered* rather than read. Replaced by an on-page `role="status"` region. Submit buttons disable and relabel, and re-enable on a bfcache restore so a Back navigation does not leave a dead form. | [static/js/app.js](../static/js/app.js) |
| US-3 | P1 | ~~Recognised-students list **does not update live** — the operator must end the session to learn whether anyone was recorded.~~ ✅ **Fixed 2026-08-16 (Phase 5).** `GET /attendance/live`, `@authenticated` **and** `@json_api`, polled every 2 s; it reads the confirmed identities from `RecognitionSession` rather than from the register, because the question is "is the camera doing anything?" and a row from an earlier session today would answer it wrongly. **This is FS-14's failure mode made visible:** twenty of twenty frames agreeing on the right student, nothing written, nothing logged — reported by a person watching the screen, because the screen had no way to disagree with itself. | [static/js/attendance.js](../static/js/attendance.js) |
| US-4 | P2 | Subject code typed twice, free-text, must match exactly (see FS-7). | [attendance.html](../templates/attendance.html) |
| US-5 | P2 | ~~`onsubmit="return confirm('… {{ student.name }} …')"` — Jinja escapes `'` to `&#39;`, which decodes back to `'` inside the attribute and **breaks the JS string**. A student named e.g. `O'Brien` makes the handler fail to compile and **the delete submits with no confirmation**.~~ ✅ **Fixed 2026-08-16 (Phase 5).** Five of these. `data-confirm` carries the text as an attribute *value*, which is never parsed as code, and one delegated listener in `static/js/app.js` covers every form on every page including ones added later. ⚠️ The test that keeps this closed **parses** the HTML with `html.parser`: `manage_students.html` now contains the sentence *"This was `onsubmit=...`"* describing the bug, so a text search matches the prose (L12). | [static/js/app.js](../static/js/app.js) |
| US-6 | P2 | Status conveyed by colour alone (green/red boxes, coloured OpenCV overlays); no ARIA, no text alternative, emoji `👤` as avatar. Fails WCAG 1.4.1. ◨ **Largely fixed 2026-08-16 (Phase 5) — in the web UI.** Status is a word before it is a colour; skip link; `<nav>` landmark with a name; `aria-current` on the current page; `:focus-visible` rings; table `caption` and `scope`; alt text on every image; `prefers-reduced-motion`. ⚠️ **Reviewed against WCAG 1.4.1 / 2.4.7 / 1.3.1 by inspection, not audited with a tool, and no assistive technology was used** — an honest scope statement, not a compliance claim. ⚠️ **The OpenCV overlay is untouched**, because it is pixels in an MJPEG stream: no screen reader can reach it at all, and the answer there is the live list (US-3), not ARIA. | templates, [static/css/style.css](../static/css/style.css) |
| US-7 | P3 | ~~No responsive layout; fixed sidebar. Unusable on tablet/phone.~~ ✅ **Fixed 2026-08-16 (Phase 5).** Breakpoints existed by Phase 4 but turned the sidebar into a full-width block, so the whole nav list pushed the content down on every screen. It is a disclosure now, with `aria-expanded` kept in step by `static/js/app.js`, and tables scroll inside their own container rather than widening the page. ⚠️ Verified by reading the rules, **not on a physical device**. | [style.css](../static/css/style.css) |
| US-8 | P3 | ~~`static/js/script.js` is **empty**; all JS is inlined in templates.~~ ✅ **Fixed 2026-08-16 (Phase 5).** `app.js`, `attendance.js`, `enrol.js`, `training.js` — 980 lines, none of it in a template. Server values arrive as `data-` attributes, which is also what removes the construction US-5 depends on: an inline script is the only place a rendered value ends up *inside* a JavaScript string literal. A parsed test bans both inline handlers and inline `<script>`. | [static/js/](../static/js/) |
| US-9 | P3 | No password reset, no account lockout feedback, no session-timeout notice. | — |
| **US-10** | **P2** | ~~**Enrolling a student puts them in no class, and nothing says so until the camera refuses them.**~~ ✅ **Fixed 2026-08-29** (§7 Q6 decided: **C, built as B first**). Reported by the user 2026-08-29. `/enrol/finish` writes the `students` row and starts a retrain; it never writes `enrolments`. That relation is created **only** from Subjects -> Class List (`/subject_enrolments/<id>`), a screen the enrolment flow does not link to, mention, or require. So the obvious path - enrol a student, start a session, stand them in front of the camera - ends in **"Not Enrolled"** for a student who was just enrolled, and "enrolled" means two different things on the two screens. ⚠️ **The check is right and must stay** (FS-3: without it any recognised face is recorded against whatever subject is running). What was missing was the step that satisfies it. **B:** `/enrol/finish` flashes what did *not* happen — a flash rather than the JSON message, because the capture page shows that for ~1.5 s and then navigates away — and both student list screens carry a **Classes** column reading *"None — attendance cannot be recorded"*, which is the only half that reaches a student unenrolled *later*. **A:** the registration form offers the first class list, and the ids ride inside the opaque `record` through the whole capture so they are written **in the same transaction as the student row** — a student cannot commit without the class list that makes them recordable. Recapture deliberately does not touch it, and a subject deleted mid-capture is dropped with a log line rather than rolling back a student whose images are already on disk. **Also fixed:** `save_attendance()` returned a bare `None` for *not a student*, *not in this class* **and** *the database raised*, and the overlay drew one orange "Not Enrolled" for all three — a MySQL outage read as a roster problem. Now a falsy `NotRecorded` enum, three messages, and the outage is red rather than the actionable orange. 17 new tests; the LEFT JOIN's `COUNT(e.subject_id)` is pinned by an integration test because `COUNT(*)` would report 1 for a student in no class. | [web/enrolment.py:220](../web/enrolment.py#L220), [recognize_face.py:717](../recognize_face.py#L717) |
| **US-11** | **P2** | ~~**Names ending in a period were refused: `Juan Dela Cruz Jr.` could not be enrolled.**~~ ✅ **Reported by the user and fixed 2026-08-29.** `validate_student_name()` rejected any name whose last character was `.`, so every `Jr.` and `Sr.` was turned away at enrolment, at `/enrol/start`, and when editing an existing student. **The rule was correct when it was written and its reason had since been removed:** under the old `dataset/{student_id}_{student_name}` scheme the name was a path component, and Windows silently strips trailing dots from those - `Jose Jr.` would have been created as `Jose Jr` and never found again by a lookup that rebuilt the path from the database. **Phase 5 made the folder `dataset/{student_id}` (§7.5), so the name stopped being part of any path** - and the guard went on rejecting real students to protect a filesystem behaviour nothing in the codebase could still reach. Measured rather than assumed: the check was positional, on the **last** character only, so `Ma. Teresa Santos` and `Jose P. Rizal` were always accepted and the cases that actually broke are exactly the suffixes. Replaced by a rule about names rather than paths - a name must contain at least one letter or digit - which still refuses `.`, `..` and `...`, the only inputs the old check was usefully catching. ⚠️ **`validate_student_id()` still refuses a trailing dot and must: an ID *is* the folder name.** The two validators are deliberately inconsistent. SE-3 is not relaxed - a test drives `../etc/passwd` and `Jose\Cruz` through the enrolment route and expects 400. 35 new tests, including all three entry points, because the reported symptom was "the system will not accept my name" and only a request shows the whole chain (L5). Six of them fail against the old rule. | [security/paths.py:110](../security/paths.py#L110), [tests/test_student_names.py](../tests/test_student_names.py) |

### 2.5 Reliability

| ID | Sev | Finding | Evidence |
|---|---|---|---|
| RE-1 | P0 | **Non-atomic model write** destroys the working model on any failed retrain, with no backup and no rollback. Root cause of the current outage. | [train_model.py:320-340](../train_model.py#L320-L340) |
| RE-2 | P1 | **Recognition state is module-level mutable globals** (`tracks`, `track_verification`, `recognized`, `cap`, `next_track_id`) mutated from the Flask worker thread and the camera thread with **no locking**. Two `/video_feed` requests share and corrupt one state machine; whichever generator exits first calls `cap.release()` and kills the other. | [recognize_face.py:341-350](../recognize_face.py#L341-L350), [recognize_face.py:1816](../recognize_face.py#L1816) |
| RE-3 | P1 | **No unique constraint** on `attendance(student_id, subject_code, attendance_date)`. Duplicate suppression is a read-then-write check in Python — a classic TOCTOU race under concurrent recognition. ✅ **Fixed 2026-08-11 (Phase 4).** `UNIQUE(student_id, subject_id, attendance_date)` plus `INSERT ... ON DUPLICATE KEY UPDATE`; the read-then-write is deleted, not kept beside it. ⚠️ The trap was the blanket `except mysql.connector.Error`: `IntegrityError` is one, so a legitimate duplicate would have read as failure and retried every frame. | [schema.sql:48-58](../database/schema.sql#L48-L58), [recognize_face.py:825-868](../recognize_face.py#L825-L868) |
| RE-4 | P1 | **No foreign keys** anywhere. Deleting a student leaves orphan attendance rows unless the app remembers to cascade manually (it does, in one of two paths). ✅ **Fixed 2026-08-11 (Phase 4).** **8 foreign keys**, the first in this database. `ON DELETE CASCADE` replaces the hand-rolled cascade in `/delete_student`; `subjects.instructor_id` is `SET NULL` so deleting an account does not delete the subjects it taught. | [schema.sql](../database/schema.sql) |
| RE-5 | P1 | **No logging framework.** ~200 `print()` calls. No log file, no levels, no timestamps, no audit trail — a biometric attendance system with no forensic record. | app-wide |
| RE-6 | P1 | **Zero automated tests.** The four `test_*.py` files contain no assertions and are print-only demo scripts; `pytest` collects nothing. No CI. | `test_*.py` |
| RE-7 | P2 | Student create = DB insert + subprocess + train, with **no transaction and no compensating rollback** (see FS-9). ✅ **Fixed 2026-08-11 (Phase 4).** Closed with FS-9: nothing is inserted until a complete capture is on disk, so there is no partial state to compensate for. | [app.py:180-231](../app.py#L180-L231) |
| RE-8 | P2 | Broad `except Exception` swallowing across all three main modules, several returning the raw exception to the user. | [app.py:375](../app.py#L375), [recognize_face.py:1565](../recognize_face.py#L1565) |
| RE-9 | P2 | `finally: cursor.close(); conn.close()` in `setup_db.py` runs even when the connect step failed → `NameError` masks the real error. | [setup_db.py:118-120](../setup_db.py#L118-L120) |
| RE-10 | P2 | `generate_frames()` releases the shared camera on exit, so a browser tab closing silently ends the session for everyone. | [recognize_face.py:1816-1818](../recognize_face.py#L1816-L1818) |

### 2.6 Security

| ID | Sev | Finding | Evidence |
|---|---|---|---|
| SE-1 | P0 | ~~**Passwords stored and compared in plaintext** for both admin and instructors; schema seeds `admin`/`admin`.~~ ✅ **Fixed 2026-08-08 (Phase 2).** bcrypt hashes, verified in Python, never in a SQL `WHERE` clause. `must_change_password` on `admin` and `instructors`; `setup_db.py` seeds a hash with the flag set; `scripts/migrate_passwords.py` rehashes existing rows and is idempotent. The seeded credential is still `admin`/`admin` and still public — the flag is what stops it being a working way in. | [app.py](../app.py), [schema.sql](../database/schema.sql), [security/passwords.py](../security/passwords.py) |
| SE-2 | P0 | ~~**`/video_feed` requires no authentication** — anyone who can reach the host gets the live camera stream of a classroom.~~ ✅ **Fixed 2026-08-08 (Phase 2).** `@authenticated`. The browser sends the session cookie with an `<img src>`, so nothing about the stream itself had to change. | [app.py](../app.py) |
| SE-3 | P0 | ~~**Path traversal → arbitrary directory deletion.**~~ ✅ **Fixed 2026-08-08 (Phase 2).** `security/paths.py` allowlists the ID and name, then resolves the path and proves it is a direct child of `dataset/`. All five hand-built joins replaced; a source-level test bans the pattern. **The folder scheme is unchanged**, so the rename coupling in handover §1.6 survives. Was: `student_id`/`name` come from an unvalidated form, are stored, then concatenated into a path passed to `shutil.rmtree()` and `os.rename()`. A student named `..\..\Windows\Temp` gives an authenticated user directory deletion outside `dataset/`. | [app.py:320-328](../app.py#L320-L328), [app.py:506-535](../app.py#L506-L535), [app.py:666-681](../app.py#L666-L681) |
| SE-4 | P1 | ~~**Missing auth checks** on `/edit_instructor`, `/update_instructor`, `/delete_instructor`, `/export_excel`, `/start-attendance`, `/stop_camera`, `/video_feed` — unauthenticated instructor CRUD and camera control.~~ ✅ **Fixed 2026-08-08 (Phase 2).** Default-deny `before_request` hook: an unclassified route is refused with a logged error, not served. `tests/test_route_security.py` asserts every endpoint in `app.url_map` carries a marker, so route 36 cannot reopen this. | [security/access.py](../security/access.py) |
| SE-5 | P1 | ~~**No role authorization.**~~ ✅ **Fixed 2026-08-08 (Phase 2).** 22 admin-only routes carry `@role_required('admin')`. Was: Every protected route checks only `'user' in session`. A logged-in *instructor* can call `/delete_student`, `/manage_instructors`, `/update_admin`, `/change_password`. The sidebar hides links; the routes do not enforce. | [app.py](../app.py) app-wide |
| SE-6 | P1 | ~~**Destructive operations exposed over GET**: `/delete_subject/<id>`, `/delete_instructor/<id>`. Triggerable by an `<img src>` or a link prefetch.~~ ✅ **Fixed 2026-08-08 (Phase 2).** Both are POST-only and their template links became inline POST forms with CSRF tokens. Fixing this surfaced **FS-13** — the Students page had the opposite bug, a GET link to a POST-only route. | [app.py](../app.py), [templates/](../templates/) |
| SE-7 | P1 | ~~**No CSRF protection**~~ ✅ **Fixed 2026-08-08 (Phase 2).** `CSRFProtect` globally, tokens in all 15 forms, `X-CSRFToken` on the two `fetch()` calls. Was: on any of the ~15 state-changing POST routes. No Flask-WTF, no token. | app-wide |
| SE-8 | P1 | **Hardcoded credentials in source**, duplicated in 5 files: MySQL `root` with empty password, and `app.secret_key = "ai_attendance_secret_key"` — a known secret key means forgeable session cookies. | [app.py:21,27-33](../app.py#L21), [recognize_face.py:795](../recognize_face.py#L795), [db_connection.py](../db_connection.py), [setup_db.py](../setup_db.py), [test_db.py](../test_db.py) |
| SE-9 | P1 | ~~**Biometric data governance absent.**~~ ⚠️ **Documented 2026-08-08 (Phase 2)** in [`docs/data_privacy.md`](../docs/data_privacy.md) — consent, retention, a three-step erasure procedure, access control and an RA 10173 mapping. **Documentation is not implementation:** §7 of that file lists what is still absent — no encryption at rest, no consent record in the schema, no automated retention. Original finding: Face images stored unencrypted on disk, no consent record, no retention/erasure policy, no access log. Directly relevant to RA 10173 (Philippine Data Privacy Act) — sensitive personal information. An examiner will ask. | `dataset/` |
| SE-10 | P2 | ~~Raw database errors returned to the client~~ ✅ **Fixed 2026-08-08 (Phase 2).** `templates/error.html` plus handlers for 400/403/404/405/409/500 and a catch-all. An AST test bans returning an interpolated exception. Was: (`f"Database error: {error}"`) — schema/version disclosure. | [app.py:365,430,601,1253](../app.py#L365) |
| SE-11 | P2 | ~~No session hardening~~ ✅ **Fixed 2026-08-08 (Phase 2).** `HTTPONLY`, `SAMESITE=Lax`, 30-minute lifetime. `SESSION_COOKIE_SECURE` is configurable and **defaults to False** — this deployment is HTTP on localhost (PO-4) and `True` would prevent login entirely. Was: no `SESSION_COOKIE_SECURE`, no `SAMESITE`, no `PERMANENT_SESSION_LIFETIME`, no idle timeout. | [app.py:20-21](../app.py#L20-L21) |
| SE-12 | P2 | **Liveness is defeatable by a video replay** — the challenge is one of two fixed poses (`TURN_LEFT`/`TURN_RIGHT`) with no depth, texture, or blink component. Presenting a phone playing a recording passes. Material for a system claiming anti-spoofing. | [recognize_face.py:323-334](../recognize_face.py#L323-L334) |
| SE-13 | P2 | ~~No rate limiting or lockout on `/login`~~ ✅ **Fixed 2026-08-08 (Phase 2).** Five consecutive failures per (username, address) lock the account for 15 minutes. In-memory, so cleared by a restart — an accurate model of a single-process deployment, not a shortcut. Was: — unbounded credential brute force. | [app.py:60](../app.py#L60) |
| SE-14 | P3 | ~~`admin_panel.py` executes `admin_login()` at import time~~ — moot; the file was deleted in Phase 1. | — |
| **SE-15** | **P1** | ~~**Passwords are compared case-insensitively, and trailing whitespace is ignored.** The `password` columns use `utf8mb4_general_ci` — a case-insensitive, PAD SPACE collation — and the app compares passwords *in SQL* rather than in Python. **Measured against the live database:** password `admin` is accepted as `ADMIN`, `AdMiN`, and `admin   `. Usernames behave the same way. This collapses an alphanumeric alphabet from 62 symbols to 36: an 8-character password drops from ~2.2×10¹⁴ to ~2.8×10¹² combinations, ~79× weaker, on top of already being stored in plaintext (SE-1).~~ ✅ **Fixed 2026-08-08 (Phase 2).** **Not by hashing — by moving verification into Python.** A bcrypt hash compared in SQL would have kept the defect; `verify_password()` is what removes collation from the decision. The *username* is now compared in Python too, which bcrypt does nothing for. Re-measured against the live database: `ADMIN`, `AdMiN` and `admin   ` are all rejected. | [security/passwords.py](../security/passwords.py), [tests/test_password_hashing.py](../tests/test_password_hashing.py) |
| **SE-16** | P2 | ~~**`/change_password` updated `admin WHERE id=1` regardless of who was signed in**, and there was no way for an instructor to change their own password at all.~~ ✅ **Found and fixed 2026-08-08 (Phase 2).** Not directly exploitable — the route still required the admin's current password — but it made the forced-change flow impossible, since that has to work for both roles. Now role-aware, and it answers GET so the redirect from the access-control hook has somewhere to land. | [app.py](../app.py), [templates/change_password.html](../templates/change_password.html) |
| **SE-17** | P2 | ~~**Both instructor tables rendered the `password` column.**~~ ✅ **Found and fixed 2026-08-08 (Phase 2).** `instructors.html` and `manage_instructors.html` printed `{{ instructor.password }}`. While passwords were plaintext that put every instructor credential on screen — visible over a shoulder, in a screenshot, in a projected demo. With bcrypt it would instead hand out hashes to crack offline. Replaced with a "Set / Must be changed at next login" status. | [templates/instructors.html](../templates/instructors.html), [templates/manage_instructors.html](../templates/manage_instructors.html) |

### 2.7 Maintainability

| ID | Sev | Finding | Evidence |
|---|---|---|---|
| MA-1 | P1 | ~~**Three god files**: `app.py` 1466, `recognize_face.py` 1817, `capture_dataset.py` 1983 lines. No blueprints, no service layer, no repository layer. Routes mix HTTP, SQL, filesystem, and subprocess management.~~ ✅ **Fixed 2026-08-16 (Phase 5).** `capture_dataset.py` deleted; `recognize_face.py` 1817 → 1399 across Phase 3; **`app.py` 2,890 → 161 lines**, split into `web/` (8 blueprints, 2,265), `services/` (371) and `repositories/` (884). ⚠️ **Root packages, not `src/attendance/` as §4's tree shows** — the user's decision. The finding is about layering, not a directory prefix, and an `src/` move would rewrite every import in the tests, both evaluators and the scripts for no measurable difference. | [web/__init__.py](../web/__init__.py) |
| MA-2 | P1 | ~~`capture_dataset.py` has **all logic at module scope** — no functions wrapping the flow, no `if __name__ == "__main__"`. Importing it runs the camera. **Untestable by construction.**~~ ✅ **Fixed 2026-08-16 (Phase 5): the file is deleted.** Browser enrolment replaced it in Phase 4 and it was kept as the only *proven* path until somebody had driven the new one; the user decided on 2026-08-16 that live testing happens after the refactor, so carrying 1,586 lines of Windows-only, import-executes-a-camera code through it bought nothing. | — |
| MA-3 | P1 | **Dead code targeting a schema that does not exist.** `attendance_system.py`, `admin_panel.py`, `db_connection.py` reference `attendance_sessions`, `attendance.session_id`, `attendance.date/time`, `students.course` — none present in `schema.sql`. `attendance_system.py` also reads a root-level `trainer.yml` that isn't there and **opens the camera at import**. | [attendance_system.py:28,111](../attendance_system.py#L28), [admin_panel.py:272](../admin_panel.py#L272) |
| MA-4 | P1 | **Duplicated, divergent logic.** `get_face_box()` and `is_valid_face_candidate()` exist in *both* `recognize_face.py` and `capture_dataset.py` with different signatures and different thresholds (aspect ratio 0.55–1.20 vs 0.55–1.15; eye-distance 0.17–0.68w vs 0.18–0.70w). Enrolment and recognition therefore accept **different populations of faces** — a silent accuracy leak. | [recognize_face.py:394,481](../recognize_face.py#L394) vs [capture_dataset.py:319,387](../capture_dataset.py#L319) |
| MA-5 | P1 | **Not under version control.** No `.git`, no `.gitignore`. No history, no diff, no rollback for a thesis artefact. | — |
| MA-6 | P2 | **~60 magic numbers** as module constants with no configuration layer, no environment overrides, no documented derivation. Thresholds (`RECOGNITION_THRESHOLD = 58.0`, `MIN_LABEL_AGREEMENT = 0.85`) are marked in-comment as "safe starting values … should later be calibrated" — that calibration never happened. | [recognize_face.py:221-334](../recognize_face.py#L221-L334) |
| MA-7 | P2 | Empty files committed: `main.py` (0 bytes), `face_detect.py` (0 bytes), `static/js/script.js` (0 bytes). | — |
| MA-8 | P2 | Generated artefacts committed at repo root: `attendance.csv`, `attendance_report.xlsx`, 3 timestamped report CSVs, `test_attendance.db`, `active_session.txt`, `selected_camera.txt`, `.venv/`, `trainer.yml` (878 MB), `__pycache__/`. | — |
| MA-9 | P2 | Stale UI string: status shows `"Verifying 0/30"` while the buffer is 20. | [recognize_face.py:1681](../recognize_face.py#L1681) |
| MA-10 | P2 | Debug artefacts in production paths: `print("🔥 APP.PY IS RUNNING")`, `print(students)  # <-- ADD THIS`. | [app.py:1,248](../app.py#L1) |
| MA-11 | P3 | No type hints, no docstring convention, no linter/formatter config, inconsistent naming (`instructors.html` vs `edit_instructor.html`), inconsistent code style (dense one-liners in `camera_utils.py` vs one-argument-per-line elsewhere). | — |
| MA-12 | P3 | `recognize_face.py` state machine (`generate_frames`, lines 1479–1782) is ~300 lines with **7 levels of nesting** — the single hardest block in the codebase to reason about or modify. | [recognize_face.py:1479-1782](../recognize_face.py#L1479-L1782) |

### 2.8 Portability

| ID | Sev | Finding | Evidence |
|---|---|---|---|
| PO-1 | P1 | **No installable package**: no `pyproject.toml`/`setup.py`, no lockfile, no pinned transitive deps, no Docker, no documented Python version. `requirements.txt` uses `>=` for 5 of 7 deps — builds are not reproducible. | [requirements.txt](../requirements.txt) |
| PO-2 | P1 | **No configuration layer.** Host, credentials, database name, camera index, thresholds, and paths are all hardcoded across 5+ files. Moving to another machine requires editing source. | app-wide |
| PO-3 | P1 | Runtime state in **root-level text files** (`selected_camera.txt`, `active_session.txt`) rather than config or DB — breaks under any multi-process/multi-user deployment. | [camera_utils.py:4](../camera_utils.py#L4) |
| PO-4 | P2 | `app.run(debug=False)` — the **Flask development server** is the deployment. No WSGI server, no reverse proxy, no `host`/`port` config, binds loopback only. | [app.py:1466-1467](../app.py#L1466) |
| PO-5 | P2 | No database migration mechanism; `schema.sql` and `setup_db.py` **duplicate the schema** and must be kept in sync manually. ✅ **Fixed 2026-08-11 (Phase 4).** `migrations/` is the single source of schema truth, with a ledger, a runner and a CLI. `database/schema.sql` is deleted and `setup_db.py` no longer restates any DDL. | [schema.sql](../database/schema.sql), [setup_db.py](../setup_db.py) |
| PO-6 | P2 | Windows-only camera backend and GUI assumptions (see CO-1, CO-2). ◨ **Narrowed 2026-08-16 (Phase 5).** The GUI assumption is gone with `capture_dataset.py`; the camera backend survives for recognition on the server. Same shape as CO-1: the server must be Windows, the clients need not be. | — |
| PO-7 | P3 | Absolute local paths embedded in `docs/walkthrough.md` (`file:///c:/Users/user/Downloads/...`). | [walkthrough.md](../docs/walkthrough.md) |

---

## 3. Cross-cutting: the accuracy claims will not survive examination

`docs/walkthrough.md` reports **100.00% held-out accuracy**. That number is not defensible, for four independent reasons:

1. **Data leakage.** All 100 images per student come from **one continuous capture session**. `test_heldout_accuracy.py` shuffles and splits *within* that session, so "unseen" test images are near-duplicate consecutive video frames of the training images ([test_heldout_accuracy.py:65-68](../test_heldout_accuracy.py#L65-L68)). This is not out-of-sample; it is a same-session split.
2. **Closed-set only.** Every test image belongs to an enrolled student. There is **no impostor/unknown set**, so the false-acceptance rate — the security-critical metric for an attendance system — was never measured. `RECOGNITION_THRESHOLD = 58.0` is therefore uncalibrated.
3. **N = 3.** Three identities makes a 3-way classification problem. Discriminative difficulty grows sharply with enrolment size; the result does not generalise.
4. **Threshold circularity.** The same 58.0 used in production is used to score the evaluation, with no ROC/DET curve and no reported operating point.

Reporting **FAR, FRR, and an equal-error-rate operating point** across a *separate-session* test set is the single highest-value change to the thesis's empirical chapter.

---

## 3a. PE-0 in detail — the configured model is unloadable

Discovered during the Phase 0 retrain. `train_model.py` writes a model that
`recognize_face.py` then cannot load:

```
OpenCV(4.10.0) persistence.cpp:1613: error: (-215:Assertion failed)
ofs == fs_data_blksz[blockIdx] in cv::FileStorage::Impl::normalizeNodeOfs
```

Round-trip test, identical dataset (1200 samples, 3 identities), varying only `neighbors`:

| neighbors | dims/histogram | file size | write | read | round trip |
|---:|---:|---:|---:|---:|---|
| **12 (current)** | 262,144 | **1.835 GB** | 155.5 s | 182.9 s | ❌ **read fails** |
| 10 | 65,536 | 0.572 GB | 49.3 s | 53.7 s | ✅ 15/15 probes correct |
| 8 (OpenCV default) | 16,384 | **0.220 GB** | 19.6 s | 18.1 s | ✅ 15/15 probes correct |

**Implications**

1. The system has been unable to load a model since `neighbors` was raised to 12 — the outage is not one unlucky interrupted write.
2. `docs/walkthrough.md` reports "Method A: In-Sample Verification — 100.00% (300/300)" from `test_accuracy.py`, which calls `recognizer.read(TRAINER_FILE)` on this same file and would hit this same assertion. **That documented result is not reproducible from the committed code and dataset.** This needs correcting before the defense.
3. The `neighbors=12` change is presented in `docs/walkthrough.md` as an accuracy optimisation. It is in fact the defect that took the system down. The walkthrough's claims need re-verification across the board, not just this one.
4. Even at `neighbors=10` (0.57 GB for 3 students) the ceiling would be reached again at roughly 10 students. **Only `neighbors=8` leaves usable headroom, and even that only defers the problem** — the real fix is a constant-size embedding backend (§7 Q1).

---

## 4. Target architecture

```
ai_attendance/
├── pyproject.toml
├── .gitignore / .env.example / README.md
├── config/settings.py                 # pydantic-settings, env-driven
├── src/attendance/
│   ├── app.py                         # factory only
│   ├── web/                           # blueprints: auth, students, subjects,
│   │                                  #   instructors, sessions, reports
│   ├── services/                      # enrolment, attendance, training, reporting
│   ├── repositories/                  # all SQL; parameterised; pooled
│   ├── domain/                        # dataclasses: Student, Subject, Enrolment,
│   │                                  #   AttendanceRecord, Session
│   ├── vision/
│   │   ├── detector.py                # MediaPipe wrapper
│   │   ├── validation.py              # SINGLE shared face-geometry gate  (fixes MA-4)
│   │   ├── preprocessing.py           # align + CLAHE (exists, keep)
│   │   ├── recognizer.py              # embedding backend behind an interface
│   │   ├── liveness.py                # challenge + passive checks
│   │   └── tracking.py                # tracker + per-track state machine
│   ├── security/                      # hashing, decorators, CSRF, validators
│   └── infra/                         # logging, db pool, camera, atomic model store
├── migrations/                        # single source of schema truth
├── tests/{unit,integration,fixtures}/
└── docs/
```

**Note on `vision/recognizer.py` above.** That box says "embedding backend
behind an interface". **It is out of date:** the backend question was settled
on 2026-08-08 in favour of keeping LBPH, with no second backend (§7 Q1). Read
it as "the recognition backend, behind an interface" — the interface is still
worth having for testability; the second implementation is not happening.

**All three §7 decisions were answered on 2026-08-08.** Two of them change the
target architecture above:

- **Enrolment moves into the browser** (Q2), so `capture_dataset.py` and its
  server-side OpenCV window are being replaced, not refactored. `vision/` keeps
  the quality gates — the browser supplies pixels, the server judges them.
  **This requires HTTPS**; `getUserMedia` does not work on a plain-HTTP origin
  served to another machine.
- **The dataset will be recaptured** across multiple sessions with an impostor
  set (Q3), so Phase 6 produces a real FAR/FRR/EER.

---

## 5. Phased plan

Each phase ends in a verifiable state. Phases 0–2 are non-negotiable; 3–6 are scoped to thesis value.

### Phase 0 — Restore a working system ✅ *(done 2026-08-08)*
- [x] Delete the empty `dataset/test-id_test student/` folder
- [x] Make `train_model()` **skip** under-populated folders with a warning instead of failing the whole run (FS-2)
- [x] Make model persistence **atomic**: temp write → `.bak` rotation → `os.replace()`, with rollback (RE-1) — verified against 4 cases incl. the exact failure that caused the outage
- [x] **PE-0 (discovered mid-phase):** `neighbors=12` → `8`, augmentation removed, both centralised in `LBPH_PARAMS` and imported by the evaluators so they cannot drift
- [x] Fix `test_accuracy.py` variable-aliasing bug that made it silently score **0 images**
- [x] Retrain; verified **write → read → predict** round trip
- [x] Re-run both evaluators; rewrite `docs/walkthrough.md` with reproducible numbers + a correction log
- [ ] ~~Confirm `/attendance` end-to-end~~ → **blocked on data, not code** (see below)

**Measured outcome**

| | Before | After |
|---|---|---|
| Model size | 1.835 GB | **55 MB** (33× smaller) |
| Model load | 183 s → **assertion failure** | **9.2 s** |
| Held-out accuracy | **0 / 60** | **60 / 60** |
| ms per prediction | 1691.8 | **64.2** (26× faster) |
| Training samples | 1200 | 300 |
| `start_camera()` model gate | fails | **passes** |
| Dataset predictions | n/a | **30 / 30 correct** |

**Phase 0 residual — data blockers**

1. ~~Trained students missing from the `students` table~~ ✅ **cleared
   2026-08-08.** All three inserted (College of computing / BSCS / Y4 / B).
   IDs and names were derived from `trainer/labels.txt` rather than retyped,
   because `app.py` builds the dataset path as `{student_id}_{name}` — a
   mismatched name would silently break delete / edit / recapture.
   **Verified:** `save_attendance()` writes the row with the official DB name,
   suppresses a duplicate second call, and still rejects an unenrolled ID.
   Test row removed; `attendance` is back to 0 rows.
2. ~~`subjects` table empty~~ ✅ **cleared 2026-08-08** with placeholder data:
   `CS401 / Software Engineering / Prof. Test Instructor / Monday / BSCS /
   B / 08:00-10:00`. Course and section deliberately match the three students
   so the data stays coherent once enrolment (FS-3) exists. **Replace with
   real subject data before any evaluation run.**

**Both blockers cleared — the system is demonstrable.** A simulated session
(2 of 3 students recognised) was run through the real `/end-attendance` and
`/reports` queries and then deleted; `attendance` is back to 0 rows so the
first live session starts clean. That simulation is what produced the live
evidence now attached to FS-3 and FS-4.

**Still empty:** `instructors` (0 rows), so instructor login cannot be tested.
`subjects.instructor` is free-text with no foreign key, so the placeholder
subject works regardless — but add an instructor row before testing that role.

**Two data-hygiene items noticed while seeding**

- ~~Department string inconsistency~~ ✅ **normalised 2026-08-08** to
  `College of Computing` (title case, matching the pre-existing convention).
  All 4 rows are now byte-identical.
  **Correction to the original note:** these two spellings would *not* have
  split a group-by-department report. The column collation is
  `utf8mb4_general_ci`, so `GROUP BY college_department` merged them into a
  single group of 4 — the impact was cosmetic (inconsistent display, with
  GROUP BY arbitrarily picking one spelling as the label), not analytical.
  Chasing that down is what surfaced **SE-15**: the same case-insensitive
  collation also governs password comparison.
- **`test-id` is now a dangling row.** Its dataset folder was deleted in
  Phase 0, so it can never be recognised — but `/end-attendance` counts *every*
  row in `students` (FS-3), so it will be reported Absent in every session and
  inflate Total Students. Worth deleting once it has served its purpose.

**Disk reclaimed automatically.** The 1.835 GB unloadable model was rotated
out by a later retrain: `.bak` holds only the previous generation, so
`trainer/trainer.yml.bak` is now a 55 MB copy of the working model. Nothing
to clean up by hand.

### Phase 1 — Foundation ✅ *(done 2026-08-08)*
- [x] `git init`; `.gitignore` for `.venv/`, `trainer/`, `dataset/`, `__pycache__/`, `*.db`, `*.csv`, `*.xlsx`, `.env`; commit the current state as the baseline
- [x] Delete dead code: `attendance_system.py`, `admin_panel.py`, `db_connection.py`, `main.py`, `face_detect.py`, `camera_test.py`, `find_camera.py`, `test_webcam.py`, `test_db.py`, `mediapipe_test.py`, `hello_flutter/`, root-level generated CSV/XLSX/DB (MA-3, MA-7, MA-8, CO-6) — **143 files**, plus `haarcascade/` and the empty `static/js/script.js`
- [x] Add `pyproject.toml`, pin all deps exactly, document Python 3.11 (PO-1) — `requirements.txt` deleted; `requires-python = ">=3.11,<3.12"` because `mediapipe==0.10.14` has no 3.12 wheels
- [x] Add `config/settings.py` + `.env.example`; remove every hardcoded credential and path (SE-8, PO-2) — pydantic-settings; `SECRET_KEY` required with no default; `RECOGNITION_THRESHOLD` centralised (value unchanged at 58.0); `LBPH_PARAMS` deliberately left as a code constant so a `.env` edit cannot reintroduce PE-0
- [x] Replace all `print()` with `logging` (rotating file + console, levels) (RE-5) — 178 call sites; hot-path recognition diagnostics routed to `DEBUG`
- [x] Add `pytest`, `ruff`, `pytest-cov` — **35 tests, 3.0 s**; evaluators renamed `test_*.py` → `eval_*.py`
- [ ] ~~CI workflow running lint + tests~~ → **deferred at the user's request** (2026-08-08). No git remote exists yet. `ruff` and `pytest` are configured in `pyproject.toml` and run locally; add `.github/workflows/ci.yml` when there is somewhere to push.
- [x] **Verify:** `ruff check` clean, `pytest` runs, app still boots — all seven checks pass, and `eval_heldout_accuracy.py` still reports **60/60 at avg distance 34.95**, identical to the Phase 0 baseline

**Bug found and fixed during Phase 1 verification.** `app.py` imported the
config object as a bare `settings`, and the `def settings()` route handler
lower in the file shadowed it. Import succeeded, the app booted, 27 tests
passed and `ruff` was clean — but `get_db_connection()` runs per request, so
**every database call would have raised `AttributeError`**. Fixed by aliasing
the import to `app_config`; `tests/test_no_import_shadowing.py` now checks all
eight modules for this class of bug by parsing them with `ast`. See
[`lessons.md` L5](lessons.md).

**New finding:** FS-12 (§2.1) — enrolment reported fatal errors as success.
**Fixed immediately after Phase 1 merged**, on its own branch so the
foundation work stayed behaviour-neutral and reviewable on its own. See the
FS-12 row in §2.1 and `config/exit_codes.py`.

### Phase 2 — Security ✅ *(done 2026-08-08)*
- [x] Hash passwords with `bcrypt`; migration to rehash existing rows; force change of the seeded `admin`/`admin` (SE-1) — `security/passwords.py`, `scripts/migrate_passwords.py`, `must_change_password` on both credential tables. **The decisive part was moving verification into Python** (see SE-15).
- [x] `@login_required` + `@role_required('admin')` decorators; **apply to every route**, audit the full route table (SE-4, SE-5) — implemented as a **default-deny `before_request` hook** with `@public`/`@authenticated`/`@role_required` declarations, because decorating 35 routes by hand fixes the problem exactly once. 4 public / 9 authenticated / 22 admin.
- [x] Convert all destructive routes to POST (SE-6) — and found **FS-13**, the same bug inverted, while editing the templates.
- [x] Flask-WTF CSRF on all forms (SE-7) — global `CSRFProtect`, 15 forms, `X-CSRFToken` on the two `fetch()` calls, plus a dedicated `CSRFError` handler.
- [x] **Path-traversal fix** (SE-3) — `security/paths.py`. **Decision (user, 2026-08-08): sanitise + containment, not a surrogate key.** The `{id}_{name}` folder scheme is unchanged, so no dataset migration and no retrain — and the rename coupling in handover §1.6 **survives this phase**.
- [x] Protect `/video_feed` and all camera-control routes (SE-2)
- [x] Generic error pages; log details server-side only (SE-10, US-1) — `templates/error.html`, six handlers plus a catch-all, ~15 raw-error returns removed.
- [x] Session hardening (SE-11) — `HTTPONLY`, `SAMESITE=Lax`, 30-minute lifetime. `SESSION_COOKIE_SECURE` is configurable and **defaults to False**; see the finding for why.
- [x] Login rate limiting / lockout (SE-13) — `security/rate_limit.py`, 5 failures → 15 minutes, keyed on (username, address).
- [x] Write `docs/data_privacy.md` (SE-9) — including a §7 that states plainly what the system still does *not* do.
- [x] **Verify:** security test suite — **205 tests** (43 → 205), and **48 of the 74 route tests fail against `main`**, mapping onto the findings. A regression test that passes before the fix is not a regression test.
- [x] **CI** (deferred from Phase 1) — `.github/workflows/ci.yml`. A git remote exists now; the Phase 1 handover said otherwise.

**Found during the phase, fixed in it:** FS-6, FS-13, SE-16, SE-17 (all §2).
**One incident:** running the new route tests against `main`, to prove they
caught the vulnerabilities, executed two of the unguarded destructive routes
for real — deleting a student row and the `CS401` subject from the live
database. Both restored; see [`lessons.md` L6](lessons.md).

### Phase 3 — Recognition engine *(≈3 days)*
- [x] ~~**Decide the recognition backend**~~ ✅ **Decided 2026-08-08 (user): keep LBPH, no second backend** — see §7 Q1. `neighbors=8` is already in place from Phase 0 (**measured** 1.835 GB → 0.220 GB, load 183 s → 18 s, dims 262,144 → 16,384), so **no code change is needed for this item**. PE-0 is fixed; PE-1 and PE-2 are now documented limitations rather than open defects. The rest of Phase 3 is unblocked and proceeds as written.
- [x] Extract `vision/validation.py` as the **single** face-geometry gate used by both enrolment and recognition; delete the duplicates (MA-4) ✅ **2026-08-08.** One implementation, two declared profiles. **Deviation from the literal wording, decided with the user:** `RECOGNITION_PROFILE` and `ENROLMENT_PROFILE` sit side by side in one file rather than collapsing into a single threshold set, because `capture_dataset.py` collects 50 of its 100 images per student at LEFT/RIGHT/UP/DOWN poses and recognition's frontal-only nose rule (0.28 × width) would make those stages uncollectable. The duplicate *code* is deleted; the divergence is now declared, documented and tested (`test_turned_head_separates_the_profiles`). The image-quality gate was duplicated the same way and went with it. **Verified behaviour-preserving by differential test** against the pre-MA-4 functions pulled from the `phase-2-security` blob with `ast`: 40,000 randomised synthetic meshes, recognition **0 mismatches**, enrolment **105 (0.263%)** — all of them the original's integer truncation, proven by re-running the original logic with the `int()` calls removed (**0 mismatches**). Worst case 0.865 px from a band edge.
- [x] Extract the per-track state machine from `generate_frames()` into a testable `TrackState` class; target <60 lines and ≤3 nesting levels in the loop (MA-12) ✅ **2026-08-08.** `vision/tracking.py`: `TrackConfig`, `TrackState`, `FaceTracker`. **Measured: `generate_frames()` 405 lines / nesting depth 10 → 115 lines / depth 3**; `recognize_face.py` 1817 → 1261 lines. Depth target met; the per-face loop body is ~60 lines, the whole generator is 115 (the rest is frame acquisition and the MJPEG yield). 28 new tests, none of which needs a camera. Also driven headless end-to-end with a fake camera over real dataset crops — 60 frames, correct identity confirmed, `save_attendance` stubbed per [`lessons.md` L6](lessons.md).
- [x] Encapsulate all camera/recognition globals in a `RecognitionSession` object with an `RLock`; **one session at a time**, enforced (RE-2) ✅ **2026-08-09.** `vision/session.py`. Eight pieces of state absorbed. The heavy collaborators are injected as `SessionHooks`, so `vision/` keeps its no-camera/no-MediaPipe/no-database property and the concurrency logic costs milliseconds to test — 29 tests in 0.42 s, including eight threads racing to start one session and eight racing for one viewer slot. **The lock is not held across a frame** (a `/video_feed` response outlives the operator's attention span, so `stop()` would never return); what makes the unlocked per-frame path safe is the **single-viewer invariant** — the tracker accumulates identity votes toward an attendance decision, so a second stream is refused with 409 rather than served. ✅ **RE-10 fixed with it:** the generator's `cap.release()` is gone; only `stop()` releases the camera, and the smoke harness closes a stream the way a browser tab does and asserts the camera survives. **Two deliberate behaviour changes:** starting a *different* subject mid-session is refused instead of silently overwriting `current_subject` (which mis-attributed frames already counted toward one subject), and a second `/video_feed` is 409.
- [x] Load the model **once** at session start, not at import and not per call; cache by file mtime (PE-4) ✅ **2026-08-09.** Cached against `(mtime, size)` of both files — size as well as mtime because Phase 0's outage was a *partial* write and a truncated `trainer.yml` can share an mtime with the write that produced it (L1). Measured, median of 3 cold subprocess runs: `import recognize_face` **9.15 s / 168 MB → 1.57 s / 55 MB**, `import app` **12.16 s / 204 MB → 3.18 s / 95 MB**. The mediapipe import moved inside `make_detector()` too, per the warning below. The module-scope `sys.exit(1)` behind the OpenCV-contrib check moved into the model loader — a library import must not be able to kill the interpreter. **Payoff taken:** `app.py`'s two E402 suppressions gone, `conftest.py`'s import ban gone with its reason, and `test_route_security.py` un-`slow` — 74 access-control tests, 5.2 s, no database. Fast suite 202 → 421 tests.
- [x] Throttle `CameraReader` with a condition variable / frame-ready event (PE-6) ✅ **2026-08-09.** Moved to `infra/camera.py` with a `threading.Condition`. Measured against the old class lifted from git with `ast`: a **failing** camera went from **94.7 % of one core** (642,216 reads/s) to **0.0 %** (47 reads/s) — "spins a core at 100 %" was literal, and an unplugged camera mid-session is when it mattered. ⚠️ **The consumer-side half of this finding does not pay off on this machine, and the PE-6 commit message overstates it.** The recognition loop costs 48.7–147.3 ms per frame against a 33.3 ms camera cap, so it never outran the camera and never re-read a frame. See `docs/benchmarks.md` §3 and [`lessons.md` L9](lessons.md). The signal still removed the consumer's 20 ms poll-sleep and made `stop()` wake a blocked reader immediately.
- [x] Move training to a **background job** with a status endpoint; UI polls and shows progress (PE-5, US-2) ✅ **2026-08-09.** `infra/jobs.py`. `/train_model` answers **202**, a second start **409**, and `/train_status` is polled by `templates/training_status.html` on the two pages that cause a retrain. **Stages, not a percentage:** training is ~20 s at three students, so the UI shows which stage the run is in and how long it has taken — both true — rather than a percentage that would have to be invented (L7). `/train_status` carries `@authenticated` **and** `@json_api`; a test asserts the JSON marker, because without it an expired session hands the poller an HTML login page to `JSON.parse`. `/train_model` stays admin-only. The two enrolment callers now promise "enrolment is complete, and the model is being rebuilt" instead of branching on a result they no longer wait for.
- [x] Add MySQL connection pooling; drop per-event connects (PE-7) ✅ **2026-08-09.** `infra/db.py`. ⚠️ **The audit found something bigger than the finding, and it reversed the order of the work:** 21 of the 27 call sites were not inside a `try`/`finally`. Against raw connections a leak is invisible; behind a fixed-size pool it is permanent, and after `pool_size` exceptions every request blocks forever. Pooling first would have turned an invisible inefficiency into a hang. So `db_cursor()` — guaranteed close, commit only on clean exit, rollback otherwise — came first, and the pool behind it. All 27 sites converted plus `save_attendance()`. `tests/test_db_access.py` holds four AST bans, verified non-vacuous against the pre-PE-7 file. Also moved bcrypt off a held connection in `/login` and `update_instructor`.
- [x] Strengthen liveness: add blink detection and/or a randomised multi-step challenge; document residual replay risk honestly (SE-12) ✅ **2026-08-09.** `vision/liveness.py` — an ordered sequence of two distinct steps from LEFT/RIGHT/CENTER, six orderings, per-step timeouts, redraw on timeout. **Blink detection declined by the user** (§7.6 of `docs/data_privacy.md` records why). ⚠️ **A false-rejection bug was found and fixed while doing this, and it is the more important half.** The old thresholds compared *frame-normalised* yaw against a constant while the geometry gate works in *face widths*; past roughly a 137 px face box the challenge demanded more turn than the gate would accept, so **a student standing a normal distance back could never pass liveness**, with the box still green and nothing logged. Measured: at a 208 px face box it required 0.185w and a real turn is 0.136w. Thresholds are now in face-width units. Residual replay risk is documented honestly — a recording cycling left-centre-right-centre satisfies all six sequences given time; the per-step timeouts take the attack from *certain* to roughly one in six and retryable.
- [x] **Verify:** model size, cold-start time, and steady-state FPS measured before/after and recorded in `docs/benchmarks.md` ✅ **2026-08-09.** Written, including §3's correction to the PE-6 commit and §7's list of what is *not* measured. Recognition-loop throughput measured for the first time — **6.8 fps with a face in shot, 20.5 fps with none** — since the loop could not be driven without a camera before Phase 3a's harness.
- [x] **New in this phase — CAM-1:** refuse a camera that opens, reads, and shows nothing ✅ **2026-08-09.** See §2.9.

### Phase 4 — Data model & functional gaps ✅ *(2026-08-11 and 2026-08-15 — 12 of 12, plus browser enrolment pulled forward)*

> **Reshaped mid-sprint at the user's request:** browser enrolment was pulled
> forward from Phase 5 and landed first, so this phase carries both the data
> model and the capture rewrite. Two live bugs (FS-14, FS-15) were reported by
> the user during the sprint and fixed in it.
>
> **Finished in a second sprint (Phase 4b, 2026-08-15)** with the two items
> that did not land the first time: FS-10's override screen, and the
> integration tests. **Two of the four §7 items in `handover-phase-4.md` are
> still open and are not Phase 4 checklist items** — the
> `dataset/{student_id}` folder migration (§7.5), and deleting
> `capture_dataset.py`, which stays blocked until a person has driven the
> browser enrolment flow.

- [x] Migrations as the single schema source; delete the `schema.sql`/`setup_db.py` duplication (PO-5) ✅ `migrations/NNN_name.sql` with a `schema_migrations` ledger, a runner in `infra/migrations.py` and a `scripts/migrate.py` CLI. `database/schema.sql` deleted and `setup_db.py` reduced to the two things a migration cannot do: create the database, and seed a credential whose hash is generated in Python. **Verified on a scratch database before being applied to the deployed one**, with row counts identical before and after.
- [x] Add **`enrolments(student_id, subject_id)`** join table — the missing core relation (FS-3) ✅ migration 002, plus the **Class List screen** that populates it — a join table nobody can fill leaves every register empty, which is a different wrong answer rather than a right one.
- [x] Add FKs across `attendance`, `enrolments`, `subjects.instructor_id` (RE-4) ✅ **8 foreign keys**, the first this database has ever had. `ON DELETE CASCADE` replaces the hand-rolled cascade in `/delete_student`; `subjects.instructor_id` is `SET NULL` so deleting an account does not delete the subjects it taught.
- [x] Add `UNIQUE(student_id, subject_code, attendance_date)` and rely on it instead of the read-then-write check (RE-3) ✅ **on `subject_id`, not `subject_code`** — see the note below. The TOCTOU check is deleted, not kept beside it. ⚠️ The trap was the exception handling: `IntegrityError` is a `mysql.connector.Error`, so the existing blanket `except` would have turned an ordinary duplicate into a failure the caller renders as "Not Enrolled" and retries every frame. `ON DUPLICATE KEY UPDATE` avoids raising at all.
- [x] Add `attendance_sessions` (subject, date, start, end, instructor) so a session is a first-class record ✅ migration 003. Opened *before* the camera, so a session that starts and is never ended is still a recorded fact — without the row, "the class never met" and "the class met and nobody was recognised" are the same picture.
- [x] **Persist Absent rows** on session end, scoped to students *enrolled in that subject* (FS-3, FS-4) ✅ one `LEFT JOIN` finds the absentees and one `executemany` writes them, both inside a single `db_cursor` block because that is one transaction per `with` and a loop would leave half a register on failure.
- [x] Derive Late from `subjects.time_in`; convert `time_in`/`time_out` to `TIME` (FS-8) ✅ migration 005. **The system recorded its first ever non-"Present" status.** ⚠️ The conversion is deliberately not a `MODIFY`: this server is not in strict mode, so an unparseable time would have become **midnight** and marked a whole class Late from a migration reporting success.
- [x] Make enrolment atomic: capture first, insert only on success, with cleanup on cancel (FS-9, RE-7) ✅ `infra/dataset_store.py` — images go to `dataset_staging/`, are promoted only when complete, and *then* the student row is written. A test arms the database with a tripwire and drives start/frame/cancel to prove nothing is written during a capture.
- [x] Wire the dashboard to real aggregate queries (FS-5) ✅ the route had no database access at all before — four lines returning a render — so this was a route change, not a template tidy-up.
- [x] ~~Fix the `edit_instructor.html` template name (FS-6)~~ → **stale: Phase 2 already did this.** Struck rather than ticked.
- [x] **Add an attendance override/correction screen with an audit trail (FS-10)** ✅ **2026-08-15 (Phase 4b).** `/attendance/<id>/correct`, GET and POST, `@authenticated` so the instructor who watched the miss can fix it (decided with the user). **The audit trail is the feature and the screen is the interface to it:** the status update and the audit insert share one `db_cursor` block, so a failing audit insert rolls the correction back — there is no path that changes a biometric determination without recording who, when, from what, to what and why. ⚠️ The reason's length is checked **in Python**, not left to `VARCHAR(255)`: with no strict mode the column truncates silently, which would leave a mangled permanent record of an override reporting success (L11). `time_in` is deliberately never written — correcting an Absent to Present would have to invent an arrival time.
- [x] **Verify:** integration tests for the full session lifecycle against a real test database ✅ **2026-08-15 (Phase 4b).** `tests/integration/`, gated on `INTEGRATION_DB_NAME`, building and dropping a scratch schema. **20 tests covering FS-3, FS-4, FS-5, FS-7, FS-8, RE-3, RE-4 and PO-5**, plus 9 for FS-10 (29 integration in total, alongside 17 new fast tests for FS-10's validation rules). CI has a service container now. ⚠️ **Every test was proven non-vacuous** — six by re-introducing the defect and confirming the failure, two against a database built from migration 001 alone (0 foreign keys, no unique index). Two of the FS-10 tests were found vacuous that way and rewritten to read the SQL with `ast` instead of searching the route's text.

**Pulled forward from Phase 5 and completed (todo.md §7 Q2):**

- [x] **Browser-side enrolment** replacing `capture_dataset.py`'s server-side OpenCV window ✅ five admin-only routes, a `getUserMedia` capture page, and `vision/enrolment.py` holding the nine-stage protocol. **Every quality gate stays on the server** — reimplementing them in JavaScript would recreate MA-4. Frames are judged at a canonical 1920×1080 because two thresholds are absolute pixels and a browser has no fixed frame size; `infra/uploads.py` crops to aspect and **never squashes**, since stretching 4:3 to 16:9 widens a face by a third and fails gates for a reason unrelated to the person.
- [x] `vision/pose.py` extracted from `capture_dataset.py`, proven behaviour-preserving over **200,000 randomised trials, 0 mismatches** ([L7](lessons.md)).
- [x] **Schema self-heals on start-up** — `python app.py` applies pending migrations, because the system is deployed by copying it to another machine and `CREATE TABLE IF NOT EXISTS` is a no-op on an existing table. Called from `__main__` only, enforced by an `ast` test: the suite imports `app` and CI has no database.

**Not closed by the above, and worth being precise about:**

- **CO-3 is *reduced*, not closed.** Enrolment runs in the browser, but only over `http://localhost` — `getUserMedia` needs a secure context and TLS was deliberately out of scope. Remote enrolment still does not work.
- **CO-1, CO-2, MA-2 and PO-6 survive.** `capture_dataset.py` (1,586 lines), `/capture_face`, `/recapture_face` and `config/exit_codes.py` all still exist and still work — deliberately, since until a person has driven the browser flow they are the only working enrolment path. `camera_utils.py` keeps `cv2.CAP_DSHOW` regardless, because recognition still opens the classroom camera.
- **FS-11 is partial:** `/export_excel` honours the report filters and writes a timestamped file into `reports/` instead of a fixed name at the repository root, but the download is not streamed and **PE-8 is untouched** — pandas still receives a raw DBAPI2 connection. Phase 5.
- **The `dataset/{student_id}` folder migration** (§7.5) was agreed with the user, deliberately reordered after the data model, and then not reached.

### Phase 5 — Web layer & UX ✅ *(done 2026-08-16 — 8 of 8, plus two items carried from Phase 4)*

> **Smaller than its ≈2-day estimate at the start and larger by the end.** The
> capture rewrite, the subject dropdown and the export filters had already
> landed in Phase 4; the two items `handover-phase-4b.md` §7 left open were
> pulled in, because the user decided (2026-08-16) that **all live testing and
> re-enrolment happen after the refactor is complete** — which removed the one
> reason `capture_dataset.py` was still on disk.

- [x] **Serve the application over HTTPS** ✅ `SSL_CERT_FILE`/`SSL_KEY_FILE`,
      validated as a pair at start-up — one without the other is a refusal to
      boot, never a quiet fall back to HTTP. `SESSION_COOKIE_SECURE` is
      **derived** from whether TLS is configured rather than being a flag
      somebody has to flip (SE-11), still overridable for a terminating proxy.
      `scripts/make_dev_cert.py` writes correct SANs (`DNS:` for hosts, `IP:`
      for addresses, including the LAN address); `mkcert` documented as the
      better route. **Verified live both ways:** `https://` answers 200 with
      `Secure; HttpOnly; SameSite=Lax`, plain HTTP answers 200 without
      `Secure` and logs why enrolment will only work locally. ⚠️ **That a
      browser accepts the certificate and hands over the camera is a live
      check and is not done** — CO-3 closes when a second machine enrols
      somebody.
- [x] ~~Refactor `capture_dataset.py` into functions with a `main()` guard
      (MA-2)~~ → **superseded and now finished: the file is deleted** (Phase 5).
      MA-2 and CO-2 close with it; CO-1 and PO-6 **narrow rather than close**,
      because `camera_utils.py` keeps `cv2.CAP_DSHOW` for the classroom camera
      on the server. The server must be Windows; the clients need not be.
- [x] **Split `app.py` into blueprints + service layer + repositories (MA-1)**
      ✅ 2,890 → **161 lines**. `web/` 8 blueprints, `services/` 4 modules,
      `repositories/` 7. URLs unchanged, endpoint names blueprint-qualified.
      ⚠️ **Root packages, not `src/attendance/`** — the user's decision; see
      the MA-1 row in §2.7 and the note in `pyproject.toml`.
- [x] ~~Replace free-text subject entry with a `<select>`~~ ✅ **Done in Phase 4.**
- [x] **Flash messages, loading states, error pages with navigation (US-1,
      US-2)** ✅ `templates/base.html` — sixteen pages extend it, so a page
      cannot be *missing* the flash region. Twelve confirmations, each naming
      what happened **and what did not**. `alert()` removed from the attendance
      page in favour of an on-page `role="status"` region.
- [x] **Live recognised-students feed (US-3)** ✅ `GET /attendance/live`,
      polled every 2 s, reading the recognition session rather than the
      register. **This is the screen FS-14 needed and did not have.**
- [x] **Fix the `confirm()` quoting bug (US-5)** ✅ five of them, `data-confirm`
      plus one delegated listener. The test parses the HTML, because the file
      now *documents* the bug it no longer has (L12).
- [x] **Accessibility pass (US-6)** ✅ skip link, `<nav>` landmark, `aria-current`,
      `:focus-visible`, table `caption`/`scope`, alt text, status as text,
      `prefers-reduced-motion`. ⚠️ **Reviewed by inspection, not audited with a
      tool; no assistive technology was used.** The OpenCV overlay is untouched
      and cannot be fixed with ARIA — it is pixels.
- [x] **Responsive layout; move inline JS into `static/js/` (US-7, US-8)** ✅
      the sidebar is a disclosure below 768px instead of a full-width block;
      980 lines of JavaScript across four files, none in a template. ⚠️ Not
      tested on a physical device.
- [x] **Report filters honoured by `/export_excel`, and PE-8 (FS-11)** ✅
      openpyxl write-only from a server-side cursor; pandas dropped from the
      dependencies. **Nothing is written to disk** — the `reports/` copy was an
      accumulating pile of registers with no retention answer.

**Carried in from `handover-phase-4b.md` §7 and completed:**

- [x] **The `dataset/{student_id}` folder migration (§7.5)** ✅ with a
      dry-run-by-default rename script, a retrain, and held-out re-verified at
      **60/60, avg 34.95**. The payoff is visible in `/update_student`, which
      lost seventy lines: an `os.rename()` under a database cursor, a
      compensating rename in the `except` branch, a 409 for a colliding folder
      and a 500 about File Explorer. A rename is one UPDATE now.
- [x] **Delete `capture_dataset.py`** ✅ see above.

**Two things this phase deliberately did not do:**

- **No live camera run, browser enrolment run or recapture.** Deferred by the
  user until the refactor was complete. They are the first items in
  `handover-phase-5.md` §6.
- **PO-3 is untouched.** `selected_camera.txt` is still a root-level text file.
  `active_session.txt` was deleted (it was dead), which is half of it.

### Phase 6 — Evidence for the ISO 25010 evaluation *(≈2 days)*

> **Unblocked by §7 Q3 (decided 2026-08-08): recapture is feasible.** This
> phase now produces a real evaluation rather than a caveated one. **Run it
> after Phase 5**, so the recapture uses the new enrolment flow and doubles as
> that flow's acceptance test instead of being done twice.

- [ ] **Collect written consent first** — from the three students for the
      recapture, and from every impostor. An impostor is never enrolled but
      their face is still captured and processed, which is sensitive personal
      information under RA 10173 exactly as a student's is, and the consent
      must name the real purpose. Nothing in the schema records consent yet
      ([`docs/data_privacy.md`](../docs/data_privacy.md) §2), so this is paper,
      and it happens **before** the capture session.
- [ ] Recapture the dataset across **≥2 separate sessions** per student, different days/lighting (fixes the leakage in §3)
- [ ] ~~Build an **impostor set** (non-enrolled faces) for open-set
      evaluation~~ → **decided: the Georgia Tech Face Database** (user,
      2026-08-09), 50 subjects × 15 images, already on disk and gitignored.
      Impostors and probes only, never training data. **FAR can be measured
      before the recapture** — the impostor side does not depend on the
      leakage fix — so this is the cheapest evidence in the phase and a
      reasonable first move. Check the database's usage terms before
      publishing anything derived from it.
- [ ] **Re-derive `RECOGNITION_THRESHOLD` from the DET curve.** 58.0 is a
      measured-working value, not a calibrated one, and `tests/test_settings.py`
      asserts it so it cannot drift on a hunch ([`lessons.md` L2](lessons.md)).
      This is the one legitimate reason to change it — update that test in the
      same commit and cite the curve.
- [ ] **Measure and write up the LBPH scaling ceiling.** §7 Q1 keeps LBPH, so
      ~18.3 MB/student and the `cv::FileStorage` read failure between 0.57 GB
      and 1.84 GB become stated limitations: a hard ceiling near **31 students**,
      below one large class. Cheap to produce — the measurement exists in §3a.
- [ ] Rewrite the evaluation harness to report **FAR / FRR / EER**, a DET curve, and a *justified* operating threshold (§3)
- [ ] Benchmark suite: model size, cold start, FPS, recognition latency, memory — before vs after
- [ ] Usability instrument: SUS questionnaire + task-completion timings with real instructors
- [ ] Portability evidence: run on a second machine from a clean checkout using only `README.md`
- [ ] Maintainability metrics: `radon` cyclomatic complexity, LOC/module, test coverage — before vs after
- [ ] `docs/iso25010_evaluation.md` — every characteristic mapped to a measured metric

### Phase 6d — the two UAT findings *(reported by the user 2026-08-29)*

> **Both are done.** FS-16 and US-10, including the overlay split. §7 Q6
> was decided by the user mid-sprint.

> Both came from running the system, not from reading it. Neither is visible to
> the test suite as written, and that is recorded with each one.

**FS-16 — the end-of-session guard runs after the state it checks is destroyed.**

- [x] **Delete the pre-flight `/stop_camera` fetch in `static/js/attendance.js`
      and submit the End form directly.** The route calls `stop_camera()` itself
      a few lines after the guard, so this is behaviour-preserving apart from
      the defect: the browser stops the session, the server forgets the
      subject, and the guard it was meant to feed sees `None`. One deletion,
      and R3 starts working for the first time.
- [x] **Make the session row the authority, not the dropdown.** Done, and it
      is the load-bearing half: deleting the fetch only stops *this* client
      creating the state. `attendance_repo.open_sessions_today()` is consulted
      whenever the in-memory session is gone, and a submission naming a
      different subject than the open row is refused. No hidden field was
      needed — the row is already written before the camera opens, so the
      server can find it without the browser's help.
- [x] **Close the right session, and stamp the register with it.** Fell out of
      the above: the fallback used to write `session_id = NULL` and close
      nothing, so the dashboard counted a finished class as active all day.
- [x] **A test in the browser's order.** `tests/test_end_attendance_subject.py`
      posts the form with the session still set, which is the one order
      production never uses. Add a case that stops the session first and still
      expects the 409 — it failed against the old route, and a second one pins
      the matching-subject path that proves the fix is not merely a refusal.

**US-10 — an enrolled student is in no class.**

- [x] **§7 Q6 first.** Decided by the user 2026-08-29: **C, built as B
      first.** Both halves are implemented.
- [x] **Separate the three causes behind "Not Enrolled" on the overlay.**
      `save_attendance()` returns `None` for *not a student*, *not in this
      class*, and *the database raised* — and the overlay renders one orange
      label for all three. "Not in this class" is the one the operator can act
      on, and a database outage currently looks like a roster problem.
- [x] **Make a student in zero classes visible before the camera does.**
      Whatever §7 Q6 decides, a student can still end up in no class later
      (unenrolled, a new term), so the state needs to be legible on Manage
      Students rather than discovered at the kiosk.

**US-11 — `Jr.` could not be enrolled.**

- [x] **Remove the trailing-period rule from `validate_student_name()`.** Its
      reason - the name being a path component - was removed by the Phase 5
      folder migration. Replaced with "must contain at least one letter or
      digit", which is a statement about names rather than about Windows.
- [x] **Leave `validate_student_id()` alone.** An ID *is* the folder name, so
      the same character is still refused there. A test says so, because the
      two rules now look inconsistent and the next reader will want to align
      them.
- [x] **Prove it through all three entry points**, not the validator:
      `/enrol`, `/enrol/start` (which re-validates the name out of the JSON,
      and would otherwise accept a student on the form and refuse them a step
      later) and `/update_student`, so a student enrolled as "Jr" without the
      dot can be corrected.

---

### Phase 6e — the demo-readiness audit and its code mitigations ✅ *(2026-08-29)*

An end-to-end audit against a live database and real hardware, rescoped by the
user to the demo machine (complete, running, its own database instance,
**retrained on every version update**). That policy is what surfaced D1–D7,
none of which existed as findings before it was stated.

Implemented — code only; **data findings went to the team that owns the
database** (see [`handover-phase-6e.md`](handover-phase-6e.md) §5):

- **R1** ✅ The liveness challenge now advances on frames LBPH could not read.
  Diagnosed in 6c, left open there, measured at **4 min 07 s** to record one
  student. One line, inside an existing guard; three tests confirmed failing
  first. Recognition-neutral: held-out **80/80, avg 33.46, identical**.
- **D1** ✅ *(new)* A partial retrain exited **0**. `train_model.py`'s CLI now
  exits `EXIT_INCOMPLETE` (3) and logs its result; `--allow-incomplete`
  accepts a knowingly short roster. FS-12's shape at a second entry point.
- **D2 / B3** ✅ `scripts/preflight.py` *(new)* — read-only readiness checks
  with a meaningful exit status. Catches the one thing no exit code can: a
  **stale** model, left in place by the atomic write after a failed retrain.
- **D4** ✅ *(new)* `LBPH_PARAMS` was pinned by **nothing**, while a test
  guarded the threshold calibrated against it. `tests/test_lbph_params.py`
  closes it; verified by reproducing the `neighbors=12` mistake.
- **D3, D7** ✅ Documented: the erasure step that retraining automates, and the
  first measured retrain cost (**17 s**, 4 identities / 400 images).
- **G7** ◧ `build/` deleted. `capture_dataset.py` kept — a test drives it, so
  the documentation is what is wrong.

**1327 passed, 51 skipped** (20 added) · 40 integration · `ruff` clean.

⚠️ **R1's live effect is unmeasured** — the dev machine has no working camera.
Measure on the demo machine before quoting an improvement.

---

## 6. Effort summary

| Phase | Focus | Est. | ISO characteristics addressed |
|---|---|---|---|
| 0 | Restore service | 1 h | Reliability, Functional Suitability |
| 1 | Foundation | 1 d | Maintainability, Portability |
| 2 | Security | 2 d | **Security**, Usability |
| 3 | Recognition engine | 3 d | **Performance Efficiency**, Reliability, Maintainability |
| 4 | Data model **+ browser enrolment** | 2 d | **Functional Suitability**, Reliability, Compatibility |
| 5 | Web & UX | 2 d → **less** | **Usability**, **Compatibility**, Maintainability |
| 6 | Evidence | 2 d | All eight (measurement) |

**Total ≈ 12 working days.** Phases 0–2 (≈3 days) take the system from *broken and insecure* to *demonstrable and defensible*.

**Phase 4 ran over its estimate**, and the reason is worth recording rather
than smoothing over: browser enrolment was pulled forward into it from Phase 5,
two live bugs arrived mid-sprint from the user (FS-14, FS-15), and one
migration had to be written three times because the first two drafts relied on
the server failing in ways it does not. Four of its items did not land.

**Phase 5 is correspondingly smaller than estimated** — the capture rewrite,
the subject dropdown and the export filters are already done. What remains
there is blueprints (MA-1), HTTPS, flash messages, the live recognised-students
feed, the accessibility pass and the responsive layout.

---

## 7. Open decisions

*Originally "decisions needed before Phase 3". Q1 blocked Phase 3 and is now
answered, so Phase 3 can start; Q2 and Q3 block Phases 5 and 6.*

1. ~~**Recognition backend.**~~ ✅ **Decided 2026-08-08 by the user: keep LBPH, and do not add a second backend.** The manuscript is written and the thesis is pending prefinal defense; changing the recognition model would mean rewriting it. The standing recommendation had been to add an embedding backend alongside LBPH as a documented comparison — **that is now out of scope too**, since it would add a chapter rather than change one, and the constraint is manuscript time, not implementation time.

   **What this settles:** LBPH stays at `neighbors=8`, which is already in place from Phase 0 and already measured (1.835 GB → 0.220 GB at the old dataset size; the live model is **55 MB** for 3 identities). PE-0 is fixed. **PE-1 and PE-2 are now permanently *mitigated*, not eliminated** — they become documented limitations of the system rather than defects to fix.

   **What it obliges, and this is the part that matters for the defense.** At `neighbors=8` the model costs ~18.3 MB per enrolled student, and `cv::FileStorage` cannot read a model back somewhere between 0.57 GB and 1.84 GB (measured, §3a). That puts the hard ceiling at roughly **31–100 students, with the lower bound being the safe planning figure** — below a single large class. This is not hypothetical: exceeding it reproduces exactly the outage Phase 0 recovered from, where OpenCV writes a model it then refuses to read. An examiner asking "does this scale to a real classroom?" must get a measured number and an honest "no, and here is why" — not a hand-wave. Producing that figure is Phase 6 evidence work and is cheap, since the measurement already exists.
2. ~~**Deployment topology.**~~ ✅ **Decided 2026-08-08 by the user: move enrolment into the browser.** `getUserMedia` + upload; the `subprocess` + server-side OpenCV window design goes. This resolves **CO-3**, and **CO-1, CO-2 and PO-6 fall out with it** — `cv2.CAP_DSHOW` and `ctypes.windll` are the Windows-only pieces, and both live in the capture path that is being deleted.

   **⚠️ This has a hard prerequisite, and it is not negotiable: TLS.** Browsers expose `getUserMedia` only in a *secure context* — HTTPS, or `localhost`. On a plain-HTTP origin served to another machine the camera call does not prompt and does not fail gracefully; it rejects. **So "enrolment in the browser" and "no TLS" cannot both be true.** Serving the app over HTTPS is therefore a Phase 5 deliverable rather than a nice-to-have, and it flips `SESSION_COOKIE_SECURE` to `true` at the same time (SE-11, currently `false` precisely because there is no TLS).

   **TLS approach — decided 2026-08-08 by the user: self-signed certificate in development, a proper certificate on deployment.** Three things about that which are cheaper to know now than to discover mid-sprint:
   - **A certificate with no Subject Alternative Name is rejected outright**, and no click-through can override it — modern browsers stopped honouring the common name years ago. On a LAN the app will be reached by IP, so the SAN must contain that address as an `IP:` entry. A generated-in-thirty-seconds `openssl req` certificate typically has no SAN at all.
   - **A self-signed certificate does give a secure context once the exception is accepted**, so `getUserMedia` works. The cost is an interstitial warning on first visit per browser profile.
   - **Prefer a locally-trusted dev CA (`mkcert`) over a bare self-signed certificate.** It generates correct SANs and installs a CA into the OS and browser trust stores, so there is no warning at all. The reason is not comfort: a browser security warning on a projector during the defense invites "so is it secure?", and answering that costs more time than the setup would have.
   - **Certificates and keys are gitignored** (`*.pem`, `*.key`, `*.crt`, `certs/`, added 2026-08-08). A private key is a credential; committing one is the same class of mistake as the hardcoded `SECRET_KEY` Phase 1 removed, and git history is permanent.

   **Recommended split, stated so it can be argued with before anyone builds it:** the *browser* supplies a camera and a screen; **all vision logic stays on the server.** The page captures frames and uploads them, the server runs the existing face-geometry, blur, brightness and pose checks and returns the verdict the UI displays. The alternative — reimplementing the quality gates in JavaScript with MediaPipe Web — means two implementations of the same thresholds, which is **MA-4 all over again**, and MA-4 is a Phase 3 task to *delete* a duplicate, not create one.

   **Note what does *not* move.** Attendance recognition keeps the server-side camera: the classroom camera is physically at the kiosk, and `/video_feed` already streams it to a remote browser perfectly well. Only *enrolment* becomes browser-side. If that reading is wrong — if the attendance camera also needs to be the operator's webcam — say so before Phase 5, because it is a much larger change.

   **New attack surface, and Phase 2's rules apply to it.** An endpoint that accepts uploaded images needs: `@role_required('admin')`, CSRF, a strict content-type and magic-byte check, a per-file and per-request size cap, a bounded image count, and a decode step that cannot be talked into an enormous allocation. It must write through `security/paths.py` like everything else. It is also the natural moment to fix **FS-9** — with the images arriving before the row is written, "insert only on success" becomes the easy path rather than the hard one.

3. ~~**Dataset recapture.**~~ ✅ **Decided 2026-08-08 by the user: recapture is feasible.** Phase 6 becomes a real evaluation — ≥2 sessions per student on different days and lighting, plus an impostor set, and therefore an actual FAR/FRR/EER with a justified operating point instead of the same-session 60/60 that cannot be quoted without four caveats (§3).

   **⚠️ Sequence it after Phase 5, not before.** Recapturing with today's `capture_dataset.py` means doing it twice, since the enrolment tool is being replaced (Q2). Done afterwards, the recapture *is* the acceptance test for the new browser flow — one exercise, two results.

   **✅ The impostor set is decided: the Georgia Tech Face Database** (user, 2026-08-09). 50 subjects × 15 images, on disk at `gt_db/`, gitignored. **Impostors and probes only — never mixed into training.** It is a different capture condition from this dataset, so mixing it in would change what the model is rather than enlarge it; used as an impostor set, that difference is harmless, because the question being asked is "does an unenrolled face get accepted?" and the answer must hold across conditions.

   **What this unblocks, and what it does not.** FAR is measurable *today*,
   against the current model, without waiting for the recapture — the impostor
   side of an open-set evaluation does not depend on fixing the same-session
   leakage. The recapture is still needed for the FRR side and for the DET
   curve that justifies a threshold. Deliberately **not** done in Phase 3
   (user, 2026-08-09): the phase stayed on its six engine items.

   ⚠️ **The database carries its own usage terms**, which is a separate
   question from the RA 10173 consent governing the three enrolled students,
   and needs checking before any figure derived from it is published.

   **⚠️ Impostors are data subjects.** Someone who is never enrolled still has their face captured and processed, which is sensitive personal information under RA 10173 exactly as an enrolled student's is. They need the same consent, and the consent has to name the actual purpose — "to test whether the system wrongly recognises you" — not enrolment. Nothing in the schema records consent yet (`docs/data_privacy.md` §2), so this is paper, and it needs collecting *before* the capture session, not after. An examiner reviewing an open-set evaluation is entitled to ask where the impostor faces came from.

   **⚠️ `RECOGNITION_THRESHOLD` is expected to move, and only here.** 58.0 is the measured-working value, never a calibrated one, and `tests/test_settings.py` asserts it precisely so nobody changes it on a hunch ([`lessons.md` L2](lessons.md)). A threshold derived from a real DET curve is the one legitimate reason to change it — update that test in the same commit, with the curve as the justification.

**Decided 2026-08-08 (Phase 2), no longer open:**

4. ~~**CI.**~~ ✅ **Added.** The Phase 1 handover recorded that no git remote existed; one does — `origin` → `github.com/ab-JOY/AI-Attendance-System`, with `main` already pushed. `.github/workflows/ci.yml` runs `ruff` and the full suite. It **cannot** run the evaluators, since `dataset/` and `trainer/` are gitignored, so accuracy stays a local check.
5. ~~**SE-3 folder scheme.**~~ ✅ **Decided: sanitise + containment, not a surrogate key.** The traversal is closed without a dataset migration or a retrain, and the recognition pipeline was not touched — confirmed by the held-out run still reporting 60/60. **The consequence stands and is not a bug that was missed:** the dataset folder is still `{id}_{name}`, so renaming a student in the database without renaming their folder still breaks delete, edit and recapture (handover §1.6). Revisit with the Phase 4 data-model work, where the migration is cheaper because the schema is already moving. **✅ Closed 2026-08-16 (Phase 5).** The folder is `dataset/{student_id}`, `labels.txt` is `{label},{student_id}`, and the display name is resolved from the `students` table at model load — so the trained model is no longer the authority on how to spell a person's name. Migrated with `scripts/rename_dataset_folders.py` (dry-run by default; all three exit branches exercised) against a copy first, then retrained: **held-out 60/60, avg 34.95, unchanged**. An un-migrated installation is refused with the fixing command named, rather than silently training a model that is missing people.

**Raised and decided 2026-08-29 (user, from a live run):**

6. **Does enrolment choose the student's subjects?** US-10: `/enrol/finish`
   writes the `students` row and nothing else, so the class-list relation the
   camera checks (FS-3) is only ever created on a *different* screen that the
   enrolment flow never mentions. The obvious path therefore ends in "Not
   Enrolled" for a student who was just enrolled. **The check is not the
   problem and must not be relaxed** — without it, any recognised face is
   recorded against whatever subject happens to be running, which is the
   finding Phase 4 existed to close. Three ways to satisfy it instead:

   - **A — subject selection during enrolment** (the user's suggestion). A
     multi-select on the enrolment form, written to `enrolments` in the same
     transaction as the student row. The obvious path works with no second
     screen. ⚠️ It does not *replace* the Class List screen: a student's
     subjects change every term, and recapture must not re-ask (the images are
     being replaced, not the roster).
   - **B — leave the flow, make the gap loud.** On success, say "…is enrolled
     for recognition but is not in any class list yet" with a link to the
     screen that fixes it, and mark students in zero classes on Manage
     Students. Cheapest, and it also covers students who fall out of every
     class later — which A does not.
   - **C — both.** A as the default path, B as the safety net for the students
     A cannot reach: everyone enrolled before it existed, and everyone
     unenrolled afterwards.

   ✅ **Decided 2026-08-29 by the user: C, built as B first.** Implemented in
   the same sprint — see the US-10 row in §2.4 for what landed.

   **Recommendation as it stood: C, built as B first.** B is small, needs no schema
   thought, and closes the "nothing said so" half of the finding, which is the
   half that cost the operator a live session. A is then a convenience on top
   rather than the only thing standing between an enrolment and a working one.
   ⚠️ **The thesis writeup is affected either way** — the enrolment procedure
   is described in the manuscript, so this is the user's call.

---

## 8. Review

*To be completed as phases land.*

| Phase | Completed | Notes |
|---|---|---|
| 0 | ☑ 2026-08-08 | Restored service. Root cause was deeper than the interrupted write: `neighbors=12` made the model both unpersistable (PE-0) and unmatchable (all distances above threshold). Model 1.835 GB → 55 MB, held-out 0/60 → 60/60, predictions 26× faster. Also fixed a `test_accuracy.py` bug that made it score 0 images, and rewrote `docs/walkthrough.md` with reproducible numbers. **Two data blockers remain** (missing student rows, empty subjects table) — operator action, not code. |
| 1 | ☑ 2026-08-08 | Foundation. Deleted 143 dead files (incl. `hello_flutter/`, `haarcascade/`); `pyproject.toml` with exact pins and a hard `<3.12` bound for mediapipe; `config/settings.py` (pydantic-settings) absorbing all credentials, paths and the recognition threshold; 178 `print()` → `logging` with hot-path diagnostics at DEBUG; 35 unit tests and a clean `ruff` gate. Held-out accuracy unchanged at **60/60, avg 34.95** — the refactor is behaviour-neutral. Caught an import-shadowing bug that would have broken every DB call at request time while passing every other check (L5). New finding **FS-12**. CI deferred at the user's request. |
| 2 | ☑ 2026-08-08 | Security. Every one of the 35 routes is authenticated and role-checked by a **default-deny** hook, so a route added without a marker is refused rather than served — and a test walks `app.url_map` to prove it. Passwords are bcrypt hashes **verified in Python**, which is what actually closed SE-15: `ADMIN`, `AdMiN` and `admin   ` were re-measured against the live database and are now rejected. Dataset paths go through one validated helper that proves containment in `dataset/`. CSRF everywhere, destructive routes POST-only, generic error pages. Tests **43 → 205**, and **48 of the 74 new route tests fail against `main`** — the fixes are demonstrated, not asserted. Held-out accuracy **unchanged at 60/60, avg 34.95**: the phase is recognition-neutral. Four new findings (FS-6, FS-13, SE-16, SE-17) found and fixed. **Not done:** the surrogate-key folder scheme (user's decision — out of scope), so §1.6's rename coupling survives. One incident, [L6](lessons.md). |
| 3 | ☑ 2026-08-09 | **Complete — 8 of 8 items, plus one new finding.** Second half: RE-2, PE-4, RE-10, PE-6, PE-7, PE-5/US-2, SE-12, `docs/benchmarks.md`, and **CAM-1** (§2.9). Recognition state left module-level and unlocked by MA-12 now lives in `vision/session.py` behind an RLock, with a **single-viewer invariant** doing the work the lock cannot — the lock is not held across a frame, so what stops two streams double-counting identity votes is that the second one gets a 409. Cold start `import app` **12.16 s → 3.18 s**, which paid for itself immediately: `conftest.py`'s import ban is gone and the fast suite went **202 → 421 tests**. **Two findings turned out to be different from how they were filed.** PE-7 was recorded as a count mismatch; the count was a grep artifact and the real hazard was 21 exception paths that leak a connection — harmless today, a hang behind a pool, so exception safety had to land *before* the pool. SE-12 was recorded as a replay risk; the randomised sequence addresses that only partly (documented honestly), but the same work uncovered a **false-rejection bug** where a student standing more than about a metre back could never pass liveness at all, because the thresholds were in frame-normalised units while the gate was in face widths. **One published number was wrong and is corrected in place:** the PE-6 commit claimed 30–81 % of loop work was redundant, based on an assumed consumer cost; the real loop costs 48.7–147.3 ms against a 33.3 ms camera cap, so that half of PE-6 saves nothing here ([L9](lessons.md), `docs/benchmarks.md` §3). The producer fix is real — a failing camera went from **94.7 % of a core to 0 %**. Tests **286 → 444**; held-out unchanged at **60/60, avg 34.95**, so the whole phase is recognition-neutral. **Not verifiable by an agent and handed to the user: a live camera run**, which CAM-1 unblocked. See [`handover-phase-3b.md`](handover-phase-3b.md). |
| ~~3~~ | ~~◧~~ | *(superseded by the row above)* **Half done, 2026-08-08 — 2 of 8 items.** MA-4 and MA-12. `vision/` now holds the face-geometry gate, the quality gate and the per-track state machine, none of which needs a camera to test. **MA-4 landed as one implementation with two declared profiles, not one threshold set** (user's decision): enrolment collects 50 of its 100 images per student off-axis, and recognition's frontal-only rule would have made those stages uncollectable. Proven behaviour-preserving by differential test against the pre-refactor code — 0 mismatches on the recognition gate over 40,000 randomised meshes, and the enrolment gate's 105 traced to integer truncation *by experiment*, not inference ([L7](lessons.md)). MA-12: `generate_frames()` **405 lines / depth 10 → 115 / depth 3**, `recognize_face.py` 1817 → 1261. The recognition loop was driven end-to-end headless for the first time. Tests **205 → 286**; held-out unchanged at 60/60, avg 34.95. Three corrections to the Phase 2 handover, one of which ([L8](lessons.md)) had the sprint planned around a warning that was false. **Remaining: RE-2, PE-4, PE-6, PE-7, PE-5/US-2, SE-12, `docs/benchmarks.md`** — see [`handover-phase-3a.md`](handover-phase-3a.md) §6. |
| 4 | ◧ 2026-08-11 | **8 of 12 items, plus browser enrolment pulled forward from Phase 5 and two live bugs the user reported mid-sprint.** 13 commits, tests **450 → 628**, held-out unchanged at **60/60, avg 34.95**, schema at migration **006**. **Closed:** PO-5, FS-3, FS-4, FS-5, FS-7, FS-8, FS-9, RE-3, RE-4, RE-7 — verified by driving the real routes against the deployed database and asserting the row counts back afterwards ([L6](lessons.md)). An unenrolled student is refused, Absent is persisted scoped to the class list, and the system produced its **first ever "Late"** — it could not have before, because `subjects.time_in` was a VARCHAR compared against a TIME. **Reduced, not closed:** CO-3 — enrolment runs in the browser but only over `http://localhost`, since `getUserMedia` needs a secure context and TLS was out of scope; CO-1, CO-2, MA-2 and PO-6 all survive because `capture_dataset.py` still exists, deliberately, as the only proven enrolment path until a person drives the new one. **Two live bugs, both reported by the user:** **FS-14**, where a recognised student could never be marked present because the confirmation bar (52.0) sat below the recognition threshold (58.0) — silent, and invisible to an evaluator scoring same-session crops at 34.95; closed after measuring that 750 impostor images score 62.2 at the closest, so the band protected nothing ([L10](lessons.md)). And **FS-15**, where a failed recapture destroyed the student's existing dataset. **A serious defect in my own migration, found by testing it against the data I was afraid of rather than an empty table:** `MODIFY ... NOT NULL` does not fail on this server — no `STRICT_TRANS_TABLES`, so NULL became 0 — and the `ALTER TABLE` rebuild did not re-validate the foreign key either, so a "successful" run left corrupt references *after* `DROP COLUMN` had committed ([L11](lessons.md)). **Also found:** the server is **MariaDB 10.4.32**, not the MySQL every document claims, ~~and it stops on its own twice per session with no shutdown logged~~ — ✅ **corrected 2026-08-16: XAMPP's MySQL does not start after a Windows restart and the user starts it by hand; there was never an anomaly** ([L15](lessons.md)). `python app.py` now migrates a stale schema on start-up, because the tester runs against their own database. **Not done: FS-10's override screen (the audit table exists, the screen does not), integration tests against a real database, the `dataset/{student_id}` migration, and deleting `capture_dataset.py`.** See [`handover-phase-4.md`](handover-phase-4.md) §7. |
| 4b | ☑ 2026-08-15 | **The two Phase 4 items that did not land.** Tests **628 → 647 fast (29 skipped) + 29 integration**, held-out unchanged at **60/60, avg 34.95**, schema still at 006 (this sprint added no migration — 006 was already there and unused). **FS-10:** `/attendance/<id>/correct`, `@authenticated` so the instructor who watched the miss can fix it. The status update and the audit insert share one transaction, so a correction that is not recorded cannot happen; the failure path is the test that matters and it injects an audit-insert failure to prove the register is unchanged. **Integration tests:** `tests/integration/`, gated on `INTEGRATION_DB_NAME`, building and dropping a scratch schema, covering FS-3, FS-4, FS-5, FS-7, FS-8, RE-3, RE-4, PO-5 and FS-10. **The safety gate is the part that mattered to get right** — a suite that truncates tables is one typo from emptying the deployment, so the target must be named, must not equal the configured database (a *failure*, never a skip), and must match `test_*`; all three branches were run and observed firing. **Every claim was proven non-vacuous**, six by re-introducing the defect and two against a database built from migration 001 alone (0 foreign keys, no unique index). **Two of my own FS-10 tests were vacuous and the mutation run caught them:** they searched the route's text for "FOR UPDATE" and "time_in", which its own docstring contains, so deleting the real SQL left them passing — L4 turned on the person applying it. CI gains a service container; **MySQL 8.0 at the user's choice, while the deployment is MariaDB 10.4.32**, with `sql_mode` pinned to the deployment's non-strict setting and a test asserting it held. Configured database byte-identical before and after. See [`handover-phase-4b.md`](handover-phase-4b.md). |
| 5 | ☑ 2026-08-16 | **8 of 8, plus the two items `handover-phase-4b.md` §7 left open.** Seven commits, tests **647 → 1,101 fast (40 skipped)** and **29 → 37 integration**, held-out unchanged at **60/60, avg 34.95**, schema still at 006 (no migration this sprint), live row counts byte-identical. ⚠️ **Two corrections, both from the 5b review below:** the row counts were *carried*, not measured — students is **4**, not 5 ([L8](lessons.md), R13) — and this sprint shipped **fourteen defects that the 1,101 tests and a clean `ruff` both passed over**, two of them P1. Read the 5b row before quoting anything from this one. **Closed:** MA-1, MA-2, CO-2, US-1, US-2, US-3, US-5, US-7, US-8, PE-8, FS-11, §7.5. **Narrowed:** CO-1 and PO-6 (the server still needs Windows for the classroom camera; the clients no longer do), US-6 (reviewed by inspection, not audited; the OpenCV overlay is out of reach of ARIA by construction), CO-3 (TLS is configured and verified, but *remote enrolment* is unproven until a second machine does it). **`app.py` 2,890 → 161 lines.** The `dataset/{student_id}` migration's payoff is the seventy lines that came *out* of `/update_student`: an `os.rename()` under a database cursor with a hand-written compensating rename, all of it a consequence of a display name being load-bearing for storage. **Three defects were found by driving the system rather than by the suite.** A `url_for` split across two lines survived the template rewrite and 852 passing tests, because `BuildError` is raised at *render* time — found by loading a page and reading the 500 (L5), and now covered by a test that resolves every template endpoint. The export's temporary-file cleanup hook does not fire under the test client, so every export leaked a full attendance register into the temp directory — found by the integration test written for it, and rewritten as an in-memory buffer. And two long-standing source-level bans (`test_db_access.py`, `test_dataset_paths.py`) were about to scan a file with nothing left in it while twenty new modules went unchecked; both are derived from the tree now, and widening them surfaced two legitimate exceptions nobody had considered. **One self-inflicted incident:** undoing a mutation test with `git checkout --` discarded a file's uncommitted Phase 5 work; caught by a ban that sweeps every template rather than the one under test ([L13](lessons.md)). ~~**MariaDB stopped on its own again**, third occurrence~~ — ✅ **corrected 2026-08-16, this was never a defect** ([L15](lessons.md)). See [`handover-phase-5.md`](handover-phase-5.md). |
| 5b | ☑ 2026-08-16 | **The Phase 5 defect register, cleared before the UAT.** An end-to-end review of the refactor ([`review-phase-5.md`](review-phase-5.md)) found **fourteen defects — 2 P1, 2 P2, 5 P3, 5 P4 — and the suite was green through every one of them.** Six commits, tests **1,101 → 1,143 fast (42 skipped)** and **37 → 39 integration**, held-out unchanged at **60/60, avg 34.95**, schema **006 → 007**. **The two P1s both invalidated the live runs this UAT exists to perform.** `/attendance/live` read `recognition_session.recognized_ids` without calling it, so US-3 answered 500 on every poll of every session and *had never worked*; the guard above it returns early unless a session is **running**, and no test had ever constructed that state — `grep -rl "attendance_live" tests/` returned nothing. And FS-8 had been reopened by an argument: `save_attendance()` resolved `status or derive_status(...)` while its only production caller passed a literal `"Present"`, so every row the system had written since Phase 4 said Present and `/reports` rendered a Late counter that was structurally 0. The integration test that *asserts the derivation works* called the function with **no status** — a signature production did not use, green while the system ran the other branch ([L3](lessons.md)). The parameter is deleted rather than unused, so the defect is now unrepresentable. **A finding whose prescribed fix was itself wrong, and this is the one worth reading.** R4 said absences carry a fabricated `time_in = NOW()` — correct — and prescribed "write NULL". `attendance.time_in` was `TIME NOT NULL` and this server has no `STRICT_TRANS_TABLES`, so the NULL would have been **silently stored as `00:00:00`**: a fabricated midnight in place of a fabricated end-time, on an INSERT reporting success. Migration **007** widens the column first. New [L14](lessons.md) — a review reads files and cannot see the schema, so its *finding* and its *fix* have different evidentiary standing. **Two decisions were the user's** (2026-08-16): ending a session with a mismatched subject is refused with a **409** naming both rather than silently preferring the running one (R3), and the recorded status is carried through to the **camera overlay** rather than fixing the register alone — a student marked Late in the register and "Present" on screen is FS-7 one layer up (R2). **Also closed:** a `@json_api` route that faulted answered HTML, which is why R1 presented as a bare status code (R7); the export's "server-side cursor" was `fetchall()` in three documents' worth of prose (R6, [L9](lessons.md)); `admin WHERE id=1` survived SE-16's role fix (R8); the test suite wrote 25,000 lines into the deployment's operational log (R9); plus R5, R10–R14. **Every new assertion was mutation-tested**, and the two that matter most are guards rather than tests: the loop smoke test's `save_attendance` stub took `(student_id, subject, status)` — *shaped to the call site*, so it documented the defect instead of catching it — and the R4 test asserts `time_in IS NULL` **in SQL**, because the Python value would read a coerced `00:00:00` as a time and pass. `docs/uat_manual.md` gains **E4b, E10b, E11b, E11c and A8**, and its priority list now says which cases are being exercised for the first time. |
| 6a | ☑ 2026-08-21 | **Round 1 — the three defects the first UAT found. All three were reported as one thing and were another.** Tests **1,155 → 1,205 fast (50 skipped)**, `ruff` clean, routes **46 → 48**, schema unchanged at 007. ⚠️ **This checkout is a fresh copy, is not a git repository, and its baseline is 1,155/50 rather than `handover-phase-5b.md`'s 1,143/42** — the 8 extra skips are differential tests that read a pre-refactor blob with `git cat-file` ([L8](lessons.md): the numbers are from different trees, neither is wrong). ⚠️ **The deployment database was rebuilt from scratch at 10:02 on 2026-08-21** — `students`, `subjects`, `enrolments` and `attendance` are all **0 rows** and the admin password is back to the default, so none of the three fixes is confirmed on hardware yet. **A — "the camera does not open on enrolment".** `templates/enrol.html` carried `data-record="{{ record \| tojson }}"`. `tojson` escapes `<`, `>`, `&` and `'` but **not `"`**, because it is built for a script element or a *single*-quoted attribute; the first `"` of the JSON closed the attribute, `getAttribute()` returned the single character `{`, `JSON.parse` threw at `enrol.js:34`, and the IIFE died before reaching `openCamera(null)` on its last line — leaving every server-rendered placeholder frozen on screen with **nothing logged anywhere, because the failure was in the browser**. It only ever bit *new* enrolments: recapture passes `record = {}`, which contains no quote, so half the feature worked and that is how it reached a UAT. The comment directly above it asserted the opposite ("an attribute value needs no guarding at all") — [L9](lessons.md) again, now [L17](lessons.md). **C — "identity verified correctly but the liveness check keeps returning Unknown".** Two independent defects, neither of them an accuracy problem. Frontality (`max_nose_offset_of_width = 0.28`) was inside the gate `_stream_frames()` calls **before `tracker.assign()`**, where a rejected frame hits a bare `continue` — so a check whose stated job was *"is this face recognisable?"* was answering *"does this face exist?"*, and a frame that does not exist never refreshes `last_seen`, so `expire_old_tracks()` deleted the track 1.5 s later. **The system asked the student to turn and then deleted anyone who did**, who came back to "Verifying 0/20", re-confirmed, drew a fresh random challenge and repeated ([L18](lessons.md)). And `process_confirmed_track()` gave "I read a *different student*" and "I could not read this face **at all**" the same counter, so **two frames (0.13 s) redrew the challenge and five (0.33–0.7 s) destroyed the identity** — on precisely the frames a requested head movement produces ([L19](lessons.md)). Frontality now gates *confirmation* and nothing else, so an attendance mark still requires a frontal face while the track survives the turn; the counters are split, with the contradiction case left **exactly as strict as it was** and guarded by its own tests. **Three plausible causes were measured and ruled out first**, which is the part worth carrying: LBPH matches turned faces fine (held-out stratified *by enrolment stage* — LEFT 9/9, RIGHT 9/9 at threshold 58, median distance 34 against frontal's 34.2), the demanded turn is well inside the training set (0.146w/0.135w against a 0.10w requirement), and the duplicate `dataset/{id}_{Name}` folders are inert. **The bug was not in any of the parts — it was in where one of them was called from.** **B — "both cameras exist but it defaults to the laptop cam".** It did, correctly: `open_best_camera()` takes the first live index and `selected_camera.txt` could only be written by the scan itself. `GET /cameras` + `POST /cameras/select` and a picker on the attendance page; no plumbing into `session.start()` was needed, since it calls `open_camera()` with no arguments and the saved preference is already consulted second. The scan is **refused while a session is running** (probing would fight it for the device) and the chosen index is **re-scanned rather than parsed** — CAM-1 is the finding that a camera can open, read and return black, and the saved index is tried *first* on the next run. **Every new assertion was mutation-tested from a copy** ([L13](lessons.md) — there is no git here to undo with), including end to end: restoring the conflated branch makes the real-face loop smoke test stop recording attendance. **Neither A nor C is visible to the test suite by construction** — one dies in the browser, the other logged nothing until this sprint added three WARNINGs — so both were found by a person watching a screen, which is FS-14 and US-3's shape a third time. See [`handover-phase-6a.md`](handover-phase-6a.md). |
| 6a (round 2) | ☑ 2026-08-21 | **What fixing round 1 revealed — reported as "capture is too slow, camera view is overflowing the screen and capture direction is ambiguous", and two of those three were one cause.** Tests **1,205 → 1,210**, `ruff` clean. ✅ **Round 1's finding A is confirmed in the field** — the user reached "21 of 100 images" on a *new* enrolment, the half that could never open a camera before; round 2 is what that first successful run then reported, which is the argument for running a fix in front of a person rather than trusting a green suite. **D — the enrolment capture page had no stylesheet at all.** Ten classes, zero rules. `enrol.js` was already driving every indicator on it and **none of them rendered**: `.capture-overlay` is a `<canvas>` *sibling* of the video, so with no `position:absolute` it stacked underneath as its own block and **the face box `drawBox()` computes had never once appeared on the picture**; the progress bar was a `<div>` with no height; all three `data-state` values on the stage list looked identical; and `#preview` had no bounds, so a `<video>` fed a 1920x1080 stream laid out at its intrinsic size and pushed the page off the viewport — which is the reported overflow. The preview was also never mirrored although `drawBox()` says it mirrors coordinates *"to match the preview, which is flipped"*, so **turning left made the picture appear to turn right** — which is most of the reported ambiguity. **Nothing raised, and 1,205 tests and a clean `ruff` were green through all of it**, because the suite checks structure, endpoints, escaping and access and a missing stylesheet is none of those ([L20](lessons.md)). The tell was already in the template: inline `style=` attributes, which were not a shortcut but the only place left to make one element look deliberate. **E — capture was too slow, and 90% of it was one line.** `_save()` called `_reset_hold()`, so the subject re-earned six frames of hold **for every one of the 15 images in a stage** while standing still in a pose they had never left: **102 of a measured 114 s floor**, on a server spending **15 ms** judging a frame. The 200 ms upload tick was justified by a comment quoting "roughly 50-150 ms" — a figure carried from the *recognition* loop, which runs LBPH on every tracked face, onto a path that does MediaPipe plus one alignment and no matching at all ([L9](lessons.md), now [L22](lessons.md)). Hold moved to stage entry, tick 200→120, `CAPTURE_DELAY` 0.80→0.40: **114 s → 42 s, 63% faster**. ⚠️ **`MAX_IMAGES` stays at 100 by the user's decision** — cutting it to 60 would have reached ~28 s but moves model size, the LBPH scaling ceiling in §3a and accuracy, so nothing the evaluation quotes is touched. Nothing saved is relaxed: every gate still runs per frame and `_refuse()` still resets the hold on any failure, guarded by its own test because a subject drifting between images at full speed would be a worse dataset and invisible until accuracy moved. **The most useful thing this round produced was a vacuous test caught by mutation.** D's guard scanned `style.css` with a regex — and the stylesheet documents itself, naming `.capture-overlay` and `.capture-stage` in prose, so deleting a real rule left the test **green**. That is [L12](lessons.md) happening **inside a test file whose own docstring opens with "Everything here parses the HTML. Nothing greps it."** — a rule written down, quoted at the top of the module, and still broken by the code written to enforce a different one ([L21](lessons.md)). Comments and declaration blocks are stripped before scanning now, and the mutation was re-run to prove it bites. **Also found and deliberately not fixed:** nine unstyled classes across six other templates, cosmetic and unreported — named in the handover §7 rather than silently swept in or silently dropped, which is why D's guard is scoped to the two camera pages. See [`handover-phase-6a.md`](handover-phase-6a.md) §3b. |
| 6a (round 3) | ☑ 2026-08-21 | **"it can't detect looking forward face" — the same bug this project has now shipped three times, and the second time it was written down and deferred with an argument nobody checked.** Tests **1,210**, `ruff` clean. ✅ **Round 2's findings D and E are confirmed in the field**: the layout is bounded and the yellow face box appeared on the video, the first time that rectangle has ever been on screen. **G — `get_head_pose()` compared yaw and pitch against fractions of the FRAME.** `pitch = nose.y - eye_centre.y` grows with the face, and STRAIGHT required it between `PITCH_UP` 0.095 and `PITCH_DOWN` 0.110 — **a window 0.015 wide**, which is not a statement about a head but about standing at one distance. Measured over the 300 real enrolment images composited at a range of sizes, a face **looking directly at the camera** was called **UP** at a 400 px face box, STRAIGHT only between roughly 500 and 560 px, and **DOWN** from 600 px upward, with occasional RIGHT past 800 px — the corner the user's screenshot landed in. STRAIGHT is the **first** stage and every later stage waits behind it, so enrolment was unusable outside a ~15% band of distance. ⚠️ **`vision/pose.py` knew.** Its docstring carried a ⚠️ naming SE-12, said these thresholds had the same shape of bug, and then argued containment: *"the enrolment stages also constrain distance"*. That covers **15 of the 100 images** — only CLOSE/MEDIUM/FAR pin the ratio; the five POSE stages pin nothing and `good_distance()` admits a **45×** range of face area. A test even pinned the defect as a feature (`assert 0.015 == PITCH_DOWN - PITCH_UP`, under *"a narrow band … spelled out because it is surprising"*). Every threshold is a fraction of face width or height now and `get_head_pose()` **requires the face box**, so the frame-relative version is unrepresentable rather than discouraged; thresholds derived from the enrolment set, not chosen. Validated on all 225 POSE images: **STRAIGHT 75/75 at every face size from 400 to 1050 px**, LEFT 45/45, RIGHT 45/45, UP 30/30. ⚠️ **DOWN is 14/30 and that is a deliberate trade**: STRAIGHT runs to pitch 0.257 and DOWN starts at 0.234, so they genuinely overlap — the instruction says "look *slightly* down" and the subjects obliged — and no single threshold separates them. The line sits just above STRAIGHT's maximum so all 75 STRAIGHT images pass, because that is the reported defect and the gate every other stage waits behind; DOWN then wants a more definite movement, is told so on screen, and still completes. **H — the browser was uploading a mirrored world.** Found while fixing D. `grabFrame()` mirrored every frame under the comment *"Undo the preview's mirror before uploading … or LEFT and RIGHT are swapped for the whole capture"* — right about the stakes, wrong about the mechanism, because **`drawImage(video)` samples the decoded frame and no CSS transform touches it**. There was no mirror to undo; the flip *introduced* one, so a student told "TURN SLIGHTLY LEFT" could only satisfy it by turning **right** — and the saved crop was a mirror image of the face, which would have trained the model on mirrored faces and matched un-mirrored ones against them. Verified across all three paths: `capture_dataset.py` and `recognize_face.py` mirror nothing; browser enrolment was the only one that did, so it was also producing a dataset inconsistent with the three students already enrolled. ⚠️ **`drawBox()` was the one place the mirroring was self-consistent, and for the wrong reason** — its comment was false twice over (the preview had no CSS; the box arrived mirrored) and **the two errors cancelled exactly there**, so fixing either alone would have broken the face box. That is why it looked fine and nobody looked further ([L24](lessons.md)). **Also found:** a **stale copy of the entire project** installed in `.venv/Lib/site-packages` — every file this sprint edited differs, every untouched file matches byte for byte. The app and the suite are safe (both put the project root on `sys.path` first), but any scratch script run from elsewhere silently gets the old code, which cost one confusing `TypeError` against a function that plainly had the right signature. [L5](lessons.md) with a whole project behind it. New lessons [L23](lessons.md) (a threshold in the wrong units, three sprints running — and a deferral's containment argument is a claim that gets measured) and [L24](lessons.md). ⚠️ **H has no automated guard**: there is no JavaScript test harness in this project, and two of this sprint's eight findings lived in `enrol.js`. See [`handover-phase-6a.md`](handover-phase-6a.md) §3c. |
| 6 | ☐ | |
| 6b | ☑ 2026-08-21 | **Defect I — retraining could not be started from the UI at all.** Not a UI defect: [static/js/training.js](../static/js/training.js), `/train_model` and the `BackgroundJob` were all correct. `train_model()` refuses the whole run on any `{student_id}_{name}` folder and names `scripts/rename_dataset_folders.py --apply` as the fix; that script refused too, because the target `{student_id}` folder existed — **a deadlock with no third step**. The three offending folders were verified **byte-for-byte identical** to their counterparts (SHA-256 over all 300 images, then a second independent per-file comparison by the script). Fix: `folders_are_identical()` resolves a *provably safe* collision by moving the leftover into `dataset/_migrated_duplicates/` (a move, never a delete — the 300 images are all still on disk), `train_model()` skips that directory by name, and a differing collision is still refused. Retraining verified end-to-end through the background job the button drives: **succeeded, 4 identities, trainer.yml 72,103,331 bytes** (≈18.0 MB/student, consistent with Phase 6a). 16 new tests; suite **1240 passed, 50 skipped**; ruff clean. New lesson **L26**. ⚠️ Corrects `handover-phase-6a.md` §1.4: those folders were "inert" when that was written and had since become fatal. |
| 6c | ☑ 2026-08-21 | **Camera lifecycle — the wedged device, the relogin lockout, and the silent stall.** Three defects from UAT, all in §3 of [`assessment-phase-6c.md`](assessment-phase-6c.md). (1) **Nothing released the camera at process exit** — no `atexit`, no signal handler, no shutdown path, and `logout()` is `session.clear()`; a session running at exit left `cv2.VideoCapture` open and the OS holding the device, which Windows reports as `0xA00F429F<WindowShowFailed> (0x8007001F)` — a healthy device that will not open, indistinguishable from a hardware fault. `RecognitionSession` now registers a release hook on first camera open. **Verified in real subprocesses: fires on normal exit and on `KeyboardInterrupt`; confirmed it does *not* fire on a hard kill, which is documented rather than claimed.** (2) **The single stream slot leaked** — `generate_frames()` releases it in a `finally`, which for an MJPEG response runs only on close/collection, so `/video_feed` was refused with "already open in another window" and no relogin could clear it. Added a per-frame heartbeat and a `VIEWER_STALE_SECONDS = 10` takeover; the single-viewer invariant is intact and a reclaimed generator cannot evict its replacement. (3) **`CameraReader._read_failures` had no reader anywhere** — a dead camera stalled silently forever. Now warns once on the transition, logs recovery, and exposes `is_delivering`. 12 new tests; suite **1252 passed, 50 skipped**; ruff clean. New lesson **L27**. ⚠️ Deliberately **not** done: stopping the session on logout — see the handover, it is a behaviour decision, not a bug. |

---

## 9. Phase 6f — the verification-repeats defects (FS-17, FS-18, SE-18)

**Raised 2026-08-30 by the user:** *"why does the verification repeat even
after a person has already been verified?"* Traced to **three independent
loops**, only one of which was the R1 defect Phase 6e closed. Reproduced
without a camera or a database; the log evidence is `logs/app.log`
2026-08-21 16:01–16:06, one person, five minutes, nothing recorded.

### FS-17 — an unrecoverable rejection is retried on every frame, forever

`process_confirmed_track()` sets `attendance_saved` only when
`save_attendance()` returns truthily. `NOT_IN_CLASS` and `UNKNOWN_STUDENT` are
falsy and **permanent**, so nothing latches, `identity_reconfirmed` stays True,
and the next frame repeats the whole thing. Measured: **193 `save_attendance()`
calls in 200 frames.** Each is two DB round trips plus a WARNING line, and
because `predict_identity()` skips LBPH only on `attendance_saved`, each is
*also* a full LBPH predict — 98 ms/face at today's roster (`docs/benchmarks.md`),
280–400 ms at 30 students. One student missing from `enrolments` therefore
holds the frame rate down **for everyone else in shot**. Reopens PE-3 for the
exact face it was meant to exempt. 124 such lines in `logs/app.log`.

- [x] Latch a permanent refusal on the track; stop retrying; skip LBPH for it
- [x] Retry only `ERROR` (the DB was unreachable), on a frame backoff
- [x] Log the refusal once per confirmed track, not once per frame

### FS-18 — `recognized` is never seeded from the register

`RecognitionSession.start()` does `self._recognized = set()` and reads no
attendance rows, so `adopt_completed_identity()` — which exists precisely to
stop a student re-passing liveness — cannot fire across a session restart. Stop
and restart attendance, or restart the app, and a student already Present today
redoes the 20-frame confirmation window *and* a fresh liveness challenge, for a
write that is a no-op duplicate.

- [x] Seed the session from the register at `start()`, via a `SessionHooks`
      callable (`vision/` must stay database-free)
- [x] Carry the recorded **status** with it, not just the id — otherwise a Late
      student who re-appears reads "Present" on the overlay, which is FS-7
- [x] A failed read must not stop the session starting

### SE-18 — the post-liveness re-check resets on unreadable frames

`note_identity_mismatch()` runs on every frame with no usable match, zeroing
`post_match_count`, so attendance needs **8 consecutive** readable frames.
Measured expected wait at the 4.8 fps in the log: 2.1 s at a 95% readable-frame
rate, 11.4 s at 70%, **106 s at 50%**. A 25-frame run (~5 s) destroys a *passed*
challenge outright and sends the track back to zero.

⚠️ **This is the second half of the Phase 6c diagnosis that Phase 6e
deliberately deferred** (`handover-phase-6e.md` §1, §7). **Decided 2026-08-30
by the user: take it**, on the grounds that the log was recorded on a
high-resolution camera and the demo may not use one.

The change is R1's split applied one stage later: an unreadable frame must
**neither advance nor reset** the re-check. A contradiction — LBPH reading a
*different* enrolled student — still resets it.

- [x] Unreadable frames stop resetting `post_match_count`
- [x] A contradiction still resets it, and still drops the identity at 5 frames
- [x] Pin that unreadable frames still cannot confirm anybody (the 6e trap)

**Landed 2026-08-30.** 31 tests added (1327 → 1358), `ruff` clean, held-out
accuracy **80/80 avg 33.46 — identical**, so recognition is unchanged. Measured
before → after: `save_attendance()` calls in 200 frames **193 → 1**; the
re-check surviving one unreadable frame **0 → preserved**. See
[`handover-phase-6f.md`](handover-phase-6f.md). Gates re-run after the venv was
rebuilt by `scripts/setup.sh --recreate`; results identical.

### Not actioned — the user's call (§7)

**The 70–95 px dead band.** `RECOGNITION_PROFILE.min_face_width` accepts a
70 px face box; `align_face()` warps every crop to 200×200, so a small box is
*upscaled* before `RECOGNITION_QUALITY.min_blur_variance = 50` takes its
Laplacian, and interpolation cannot restore what the sensor never resolved.
Measured over 150 real enrolment crops:

| face box in frame | median blur variance | frames passing the gate |
|---:|---:|---:|
| 200 px | 1231.1 | 100% |
| 160 px | 171.8 | 100% |
| 130 px | 108.3 | 99% |
| 110 px | 78.1 | 93% |
| 90 px | 54.7 | 74% |
| 70 px | 34.6 | **0%** |
| 50 px | 16.0 | **0%** |

So between 70 px and roughly 95 px the geometry gate admits, tracks and locks
an identity onto a face whose every crop the quality gate then refuses. These
are *enrolment stills* — already sharp, no motion blur — so a live frame mid-turn
is worse. This is a **calibration decision** (L2 / PE-0: the last parameter
changed here without re-measuring took recognition to 0/60), so it is recorded
and not taken.

### B3 — an empty class list now blocks the session, and the End control locks

**Asked for 2026-08-30 by the user, same sprint.** B3 was handed to the other
team as data work (`handover-phase-6e.md` §5) and remains theirs — but the
*silence* around it was ours. FS-3 scopes recognition to `enrolments`, so a
session on a subject nobody is enrolled in refuses every face with "Not in this
class" and ends by writing a register of nobody. Nothing said so until a
student was standing in front of the camera being refused.

- [x] `enrolments.count_for_subject()` — the mirror of `count_for_student()`
- [x] `subjects.for_selection_with_class_list_size()` — the picker, plus a count
- [x] The attendance dropdown marks such a subject "— no class list"
- [x] **`/start-attendance` refuses when the count is zero**, before a session
      row exists and before the camera is touched
- [x] The End dropdown is **locked to the running session's subject**
- [x] The refusal is a `<dialog>`, not only the top-of-page notice — on a small
      screen the notice is off-viewport and a refused Start read as a broken
      button
- [x] The dialog carries an **Open Class List** button to the refused subject,
      withheld from non-admins because that screen is `@role_required('admin')`

⚠️ **Built first as a warning that let the session start; changed to a refusal
on the user's instruction.** The first reasoning was that an empty class list is
a roster nobody has filled in yet and blocking would obstruct whoever is setting
the class up. The user overruled it, and the reasoning holds: the session cannot
produce one useful row, and that is knowable in a single query before an
operator's time, a class's time and a camera slot are spent on it.

⚠️ **The gate does not refuse when the check itself fails.** "The database did
not respond" is not an answer, and refusing on it would turn a blip into a class
that cannot take attendance — the outcome the gate exists to be cheaper than.
`open_session()` needs the same database one line later, so a real outage still
stops the session with its own message.

**The End dropdown (R3/FS-16).** While a session runs there is exactly one
subject that control may legitimately carry, so it is disabled, set to the
running subject, and its value travels in a hidden input (a disabled `<select>`
submits nothing). ⚠️ **The lock is a convenience; the 409 is the guard, and it
stays** — a disabled control is absent from a curl, a replayed POST or a browser
with scripting off, and what it stands in front of is a register written for a
subject that was never taught.

⚠️ **`COUNT(e.subject_id)`, never `COUNT(*)`** — demonstrated live against the
dev database rather than asserted:

```
CS401: COUNT(*) = 1, COUNT(e.subject_id) = 0
```

A LEFT JOIN with no match still yields one row of NULLs, so `COUNT(*)` reports
a student on a class list that has none — silencing the warning on precisely
the subjects that need it. Third occurrence of this trap in the codebase
(`repositories/students.py`, `scripts/preflight.py`) and the first pinned by a
test.

**Landed 2026-08-30.** 14 tests added (1358 → 1372), `ruff` clean,
`node --check` clean. Both queries run against real MariaDB and agree.

⚠️ **Driven live against the dev database, attendance now cannot be started at
all**, because every subject on it has an empty class list:

```
CS401   (id 1): success = False   test123 (id 3): success = False
open_session reached : []
attendance_sessions  : 2 before, 2 after
```

That is the gate working, and it is also **a demo blocker until `enrolments` is
populated** — which is B3, still open and still the other team's (§5 of
`handover-phase-6e.md`). It has changed shape: it used to be a session that
silently recorded nothing; it is now a session that will not start.
