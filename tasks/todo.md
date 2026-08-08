# AI Attendance System — Codebase Audit & Refactoring Plan

**Audit date:** 2026-08-07
**Scope:** Full codebase (excluding `.venv/`)
**Evaluation frame:** ISO/IEC 25010:2023 product quality model
**Status:** Audit complete — plan awaiting approval before implementation

---

## 0. Headline: the system is currently non-functional and cannot self-heal

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
| FS-3 | P1 | **No enrolment model.** `/end-attendance` marks *every student in the database* absent for *every subject*. There is no student↔subject relation anywhere in the schema. **Demonstrated 2026-08-08:** with subject `CS401` (BSCS/section B), the unrelated `test-id` student was reported Absent for it. | [app.py:1200-1240](../app.py#L1200-L1240), [schema.sql:35-45](../database/schema.sql#L35-L45) |
| FS-4 | P1 | **"Absent" is never persisted.** `/end-attendance` computes absences in memory only; `/reports` counts `status == 'Absent'` from the `attendance` table, which will always be **0**. Two screens report contradictory numbers. **Demonstrated 2026-08-08** on the same simulated session: `/end-attendance` → `Present=2 Absent=2`; `/reports` for that same subject → `Present=2 Absent=0`. | [app.py:1216-1219](../app.py#L1216-L1219) vs [app.py:1313](../app.py#L1313) |
| FS-5 | P1 | **Dashboard is fake** — Total Students, Total Subjects, Today's Attendance, Active Sessions are hardcoded `0` in the template. | [dashboard.html:25-45](../templates/dashboard.html) |
| FS-6 | P1 | ~~`/edit_instructor/<id>` renders `edit_instructor.html`, which **does not exist**~~ ✅ **Fixed 2026-08-08 (Phase 2)**, in passing — a route with authentication added that still 500s for the legitimate user is not meaningfully secured. Two further faults on the same screen were fixed with it: the template's `url_for` passed `id` where the route takes `instructor_id` (a `BuildError`), and it rendered `instructor.username`, a column the schema has never had. | [app.py](../app.py), [edit_instructors.html](../templates/edit_instructors.html) |
| FS-7 | P2 | Subject code is a **free-text field typed twice** (start + end of session), not a dropdown bound to the `subjects` table. A typo silently creates an orphan attendance session that no report will find. | [attendance.html:50-77](../templates/attendance.html#L50-L77) |
| FS-8 | P2 | No late/absent policy despite `subjects.time_in`/`time_out` existing and `admin_panel.py` offering a "Late" status. Everything recorded is `"Present"`. | [recognize_face.py:1296](../recognize_face.py#L1296) |
| FS-9 | P2 | `capture_face` **inserts the student row before capture succeeds**. Cancel the capture (ESC) and you get a student with no dataset — which then permanently breaks training (FS-2). This is how the current outage was created. | [app.py:180-231](../app.py#L180-L231) |
| FS-10 | P2 | No attendance edit/override UI. A false negative cannot be corrected by the instructor. | — |
| FS-11 | P3 | `/export_excel` ignores all report filters and always exports the entire table to a fixed filename. | [app.py:1332-1360](../app.py#L1332-L1360) |
| **FS-13** | P2 | ~~**The Delete button on the Students page did nothing.**~~ ✅ **Fixed 2026-08-08 (Phase 2).** `templates/students.html:134` linked to `delete_student` with an `<a href>` (GET) while the route is POST-only, so it answered **405**. Two divergent delete UIs existed and only `manage_students.html`'s worked. Now a POST form with a CSRF token, matching the other page. | [students.html](../templates/students.html) |
| **FS-12** | **P1** | ~~**A fatal failure during enrolment is reported to the operator as success.**~~ ✅ **Fixed 2026-08-08.** Every bare `sys.exit()` in `capture_dataset.py` exited with status **0** — missing `face_preprocessing`, missing arguments, an empty student ID, an unopenable camera, and a camera read error part-way through all exited "cleanly". `app.py` treats 0 as success and runs an automatic retrain, so the operator saw a completed enrolment for a student with no usable dataset, feeding FS-2/FS-9. **Root cause was an implicit contract:** the exit codes were literals on both sides and nothing tied them together. Now `config/exit_codes.py` defines `EXIT_SUCCESS`/`EXIT_FAILURE`/`EXIT_CANCELLED` and both modules import it; the code is decided in one place after cleanup, and **only a complete capture (`count == MAX_IMAGES`) reports success**. Covered by `tests/test_capture_exit_codes.py`, including a ban on bare `sys.exit()`. | [capture_dataset.py](../capture_dataset.py), [config/exit_codes.py](../config/exit_codes.py), [app.py:242](../app.py#L242) |

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
| PE-8 | P2 | `pd.read_sql` given a raw `mysql.connector` object (unsupported by pandas; DBAPI2 fallback, emits warnings) and materialises the entire attendance table into memory. | [app.py:1349](../app.py#L1349) |
| PE-9 | P3 | Training augmentation ×4 in memory with no batching — `faces` list holds every augmented 200×200 image at once. | [train_model.py:267](../train_model.py#L267) |

### 2.3 Compatibility

| ID | Sev | Finding | Evidence |
|---|---|---|---|
| CO-1 | P1 | **`cv2.CAP_DSHOW` is Windows-only.** Camera acquisition fails outright on Linux/macOS. ⚠️ **Resolved by the §7 Q2 decision (2026-08-08): enrolment moves into the browser.** The capture path that contains this is being replaced in Phase 5, not refactored. | [camera_utils.py:30,41](../camera_utils.py#L30) |
| CO-2 | P1 | `ctypes.windll` for window placement — Windows-only (guarded, but the whole capture UX assumes Windows). ⚠️ **Resolved by the §7 Q2 decision (2026-08-08): enrolment moves into the browser.** The capture path that contains this is being replaced in Phase 5, not refactored. | [capture_dataset.py:82](../capture_dataset.py#L82) |
| CO-3 | P1 | **The architecture is single-machine only.** ⚠️ **Resolved by the §7 Q2 decision (2026-08-08): enrolment moves into the browser.** The capture path that contains this is being replaced in Phase 5, not refactored. `/capture_face` spawns an OpenCV GUI window *on the server*. If the browser is not on the server desktop, the operator sees nothing and the request hangs until someone at the server presses ESC. | [app.py:209](../app.py#L209) |
| CO-4 | P2 | `mediapipe==0.10.14` pins the project to Python ≤3.11 (venv is 3.11). No documented interpreter constraint. | [requirements.txt:5](../requirements.txt#L5) |
| CO-5 | P2 | MySQL-specific SQL (`CURDATE()`) while the test harness uses SQLite with a *different* schema — tests never exercise production queries. | [app.py:1188](../app.py#L1188), [test_accuracy.py:26-48](../test_accuracy.py#L26-L48) |
| CO-6 | P3 | `hello_flutter/` is the unmodified Flutter counter demo — no integration point, pure dead weight. | [main.dart:14](../hello_flutter/lib/main.dart#L14) |

### 2.4 Usability

| ID | Sev | Finding | Evidence |
|---|---|---|---|
| US-1 | P1 | Errors return **raw text on a blank 500 page** with no navigation — every failure is a dead end. ~15 occurrences. | [app.py:227,365,601](../app.py#L227) |
| US-2 | P1 | No flash-message system, no success confirmations, no loading indicators for the two multi-minute operations (capture, training). | app-wide |
| US-3 | P1 | Recognised-students list **does not update live** — the operator must end the session to learn whether anyone was recorded. | [attendance.html:129-165](../templates/attendance.html#L129-L165) |
| US-4 | P2 | Subject code typed twice, free-text, must match exactly (see FS-7). | [attendance.html](../templates/attendance.html) |
| US-5 | P2 | `onsubmit="return confirm('… {{ student.name }} …')"` — Jinja escapes `'` to `&#39;`, which decodes back to `'` inside the attribute and **breaks the JS string**. A student named e.g. `O'Brien` makes the handler fail to compile and **the delete submits with no confirmation**. | [manage_students.html:130-136](../templates/manage_students.html) |
| US-6 | P2 | Status conveyed by colour alone (green/red boxes, coloured OpenCV overlays); no ARIA, no text alternative, emoji `👤` as avatar. Fails WCAG 1.4.1. | templates, [recognize_face.py:1335](../recognize_face.py#L1335) |
| US-7 | P3 | No responsive layout; fixed sidebar. Unusable on tablet/phone. | [style.css](../static/css/style.css) |
| US-8 | P3 | `static/js/script.js` is **empty**; all JS is inlined in templates. | — |
| US-9 | P3 | No password reset, no account lockout feedback, no session-timeout notice. | — |

### 2.5 Reliability

| ID | Sev | Finding | Evidence |
|---|---|---|---|
| RE-1 | P0 | **Non-atomic model write** destroys the working model on any failed retrain, with no backup and no rollback. Root cause of the current outage. | [train_model.py:320-340](../train_model.py#L320-L340) |
| RE-2 | P1 | **Recognition state is module-level mutable globals** (`tracks`, `track_verification`, `recognized`, `cap`, `next_track_id`) mutated from the Flask worker thread and the camera thread with **no locking**. Two `/video_feed` requests share and corrupt one state machine; whichever generator exits first calls `cap.release()` and kills the other. | [recognize_face.py:341-350](../recognize_face.py#L341-L350), [recognize_face.py:1816](../recognize_face.py#L1816) |
| RE-3 | P1 | **No unique constraint** on `attendance(student_id, subject_code, attendance_date)`. Duplicate suppression is a read-then-write check in Python — a classic TOCTOU race under concurrent recognition. | [schema.sql:48-58](../database/schema.sql#L48-L58), [recognize_face.py:825-868](../recognize_face.py#L825-L868) |
| RE-4 | P1 | **No foreign keys** anywhere. Deleting a student leaves orphan attendance rows unless the app remembers to cascade manually (it does, in one of two paths). | [schema.sql](../database/schema.sql) |
| RE-5 | P1 | **No logging framework.** ~200 `print()` calls. No log file, no levels, no timestamps, no audit trail — a biometric attendance system with no forensic record. | app-wide |
| RE-6 | P1 | **Zero automated tests.** The four `test_*.py` files contain no assertions and are print-only demo scripts; `pytest` collects nothing. No CI. | `test_*.py` |
| RE-7 | P2 | Student create = DB insert + subprocess + train, with **no transaction and no compensating rollback** (see FS-9). | [app.py:180-231](../app.py#L180-L231) |
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
| MA-1 | P1 | **Three god files**: `app.py` 1466, `recognize_face.py` 1817, `capture_dataset.py` 1983 lines. No blueprints, no service layer, no repository layer. Routes mix HTTP, SQL, filesystem, and subprocess management. | — |
| MA-2 | P1 | `capture_dataset.py` has **all logic at module scope** — no functions wrapping the flow, no `if __name__ == "__main__"`. Importing it runs the camera. **Untestable by construction.** | [capture_dataset.py:1072-1984](../capture_dataset.py#L1072) |
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
| PO-5 | P2 | No database migration mechanism; `schema.sql` and `setup_db.py` **duplicate the schema** and must be kept in sync manually. | [schema.sql](../database/schema.sql), [setup_db.py](../setup_db.py) |
| PO-6 | P2 | Windows-only camera backend and GUI assumptions (see CO-1, CO-2). | — |
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
- [ ] Encapsulate all camera/recognition globals in a `RecognitionSession` object with an `RLock`; **one session at a time**, enforced (RE-2)
      ⚠️ MA-12 moved `tracks`, `track_verification` and `next_track_id` into a
      `FaceTracker` instance, so three of the globals are already gone — but
      **the instance is still module-level and still unlocked**, so RE-2 is
      not partly done, only staged. `cap`, `camera_reader`,
      `attendance_running`, `current_subject`, `recognized`, `recognizer` and
      `label_map` are all still module globals.
      ⚠️ **RE-10 is still live and was re-confirmed by the smoke run:**
      `generate_frames()` calls `cap.release()` when it exits, so one browser
      tab closing ends the session for everyone. Fix it in this commit.
- [ ] Load the model **once** at session start, not at import and not per call; cache by file mtime (PE-4)
      ⚠️ **The 11.6 s is two costs, not one.** Measured 2026-08-08:
      `cv2` 0.90 s, **mediapipe 4.29 s**, FaceMesh construction 0.04 s,
      **LBPH read 4.83 s**. Deferring only the model load leaves ~5.5 s,
      which is still too expensive for `tests/conftest.py` to drop its
      exception. **Defer the mediapipe import too** and `import
      recognize_face` falls to ~1 s.
      Baseline for `docs/benchmarks.md`, median of 3 cold subprocess runs:
      `import recognize_face` **9.93 s / 168 MB** peak working set,
      `import app` **12.16 s / 204 MB**. (PE-4's "multi-GB RSS" was measured
      against the 1.83 GB model and no longer holds at 55 MB.)
- [ ] Throttle `CameraReader` with a condition variable / frame-ready event (PE-6)
- [ ] Move training to a **background job** with a status endpoint; UI polls and shows progress (PE-5, US-2)
- [ ] Add MySQL connection pooling; drop per-event connects (PE-7)
- [ ] Strengthen liveness: add blink detection and/or a randomised multi-step challenge; document residual replay risk honestly (SE-12)
- [ ] **Verify:** model size, cold-start time, and steady-state FPS measured before/after and recorded in `docs/benchmarks.md`

### Phase 4 — Data model & functional gaps *(≈2 days)*
- [ ] Migrations as the single schema source; delete the `schema.sql`/`setup_db.py` duplication (PO-5)
- [ ] Add **`enrolments(student_id, subject_id)`** join table — the missing core relation (FS-3)
- [ ] Add FKs across `attendance`, `enrolments`, `subjects.instructor_id` (RE-4)
- [ ] Add `UNIQUE(student_id, subject_code, attendance_date)` and rely on it instead of the read-then-write check (RE-3)
- [ ] Add `attendance_sessions` (subject, date, start, end, instructor) so a session is a first-class record
- [ ] **Persist Absent rows** on session end, scoped to students *enrolled in that subject* (FS-3, FS-4)
- [ ] Derive Late from `subjects.time_in`; convert `time_in`/`time_out` to `TIME` (FS-8)
- [ ] Make enrolment atomic: capture first, insert only on success, with cleanup on cancel (FS-9, RE-7)
- [ ] Wire the dashboard to real aggregate queries (FS-5)
- [ ] Fix the `edit_instructor.html` template name (FS-6)
- [ ] Add an attendance override/correction screen with an audit trail (FS-10)
- [ ] **Verify:** integration tests for the full session lifecycle against a real MySQL test database

### Phase 5 — Web layer & UX *(≈2 days → re-estimate; see the first two items)*

> **Reshaped by §7 Q2 (decided 2026-08-08): enrolment moves into the browser.**
> Two items below changed meaning, and the ≈2-day estimate predates both.

- [ ] **Serve the application over HTTPS.** *New, and a prerequisite for the
      item below rather than a polish task.* `getUserMedia` is only available
      in a secure context, so browser enrolment cannot work over plain HTTP to
      another machine. **Self-signed in development, a real certificate on
      deployment** (user, 2026-08-08). Flip `SESSION_COOKIE_SECURE` to `true`
      in the same change (SE-11) and update `.env.example`.
      ⚠️ The certificate **must carry a Subject Alternative Name** covering the
      hostname *or IP* used to reach it — a common-name-only certificate is
      rejected outright and no exception can override that. Prefer `mkcert`
      over `openssl req`: correct SANs, and no warning screen to explain
      during a defense. Keys are gitignored; never commit one.
- [ ] ~~Refactor `capture_dataset.py` into functions with a `main()` guard
      (MA-2)~~ → **superseded: replace it.** Browser-side capture
      (`getUserMedia`) uploading frames to an admin-only, CSRF-protected,
      size-and-type-validated endpoint that writes through
      `security/paths.py`. The ~2,000-line module and its `subprocess` +
      OpenCV-window design go with it, taking **CO-1, CO-2, CO-3, PO-6 and
      MA-2** along. **Keep the quality gates on the server** — reimplementing
      them in JavaScript recreates MA-4, which Phase 3 exists to delete.
      Fold **FS-9** in here: with images arriving before the row is written,
      "insert only on success" is the easy path.
- [ ] Split `app.py` into blueprints + service layer + repositories (MA-1)
- [ ] Replace free-text subject entry with a `<select>` bound to `subjects`, chosen once per session (FS-7, US-4)
- [ ] Flash messages, loading states, error pages with navigation (US-1, US-2)
- [ ] Live recognised-students feed via polling or SSE (US-3)
- [ ] Fix the `confirm()` quoting bug — move to a `data-` attribute + delegated listener (US-5)
- [ ] Accessibility pass: ARIA labels, text alternatives to colour, focus states, keyboard nav (US-6)
- [ ] Responsive layout; move inline JS into `static/js/` (US-7, US-8)
- [ ] Report filters honoured by `/export_excel`; streamed download (FS-11)

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
- [ ] Build an **impostor set** (non-enrolled faces) for open-set evaluation
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

---

## 6. Effort summary

| Phase | Focus | Est. | ISO characteristics addressed |
|---|---|---|---|
| 0 | Restore service | 1 h | Reliability, Functional Suitability |
| 1 | Foundation | 1 d | Maintainability, Portability |
| 2 | Security | 2 d | **Security**, Usability |
| 3 | Recognition engine | 3 d | **Performance Efficiency**, Reliability, Maintainability |
| 4 | Data model | 2 d | **Functional Suitability**, Reliability |
| 5 | Web & UX | 2 d | **Usability**, **Compatibility**, Maintainability |
| 6 | Evidence | 2 d | All eight (measurement) |

**Total ≈ 12 working days.** Phases 0–2 (≈3 days) take the system from *broken and insecure* to *demonstrable and defensible*.

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

   **⚠️ Impostors are data subjects.** Someone who is never enrolled still has their face captured and processed, which is sensitive personal information under RA 10173 exactly as an enrolled student's is. They need the same consent, and the consent has to name the actual purpose — "to test whether the system wrongly recognises you" — not enrolment. Nothing in the schema records consent yet (`docs/data_privacy.md` §2), so this is paper, and it needs collecting *before* the capture session, not after. An examiner reviewing an open-set evaluation is entitled to ask where the impostor faces came from.

   **⚠️ `RECOGNITION_THRESHOLD` is expected to move, and only here.** 58.0 is the measured-working value, never a calibrated one, and `tests/test_settings.py` asserts it precisely so nobody changes it on a hunch ([`lessons.md` L2](lessons.md)). A threshold derived from a real DET curve is the one legitimate reason to change it — update that test in the same commit, with the curve as the justification.

**Decided 2026-08-08 (Phase 2), no longer open:**

4. ~~**CI.**~~ ✅ **Added.** The Phase 1 handover recorded that no git remote existed; one does — `origin` → `github.com/ab-JOY/AI-Attendance-System`, with `main` already pushed. `.github/workflows/ci.yml` runs `ruff` and the full suite. It **cannot** run the evaluators, since `dataset/` and `trainer/` are gitignored, so accuracy stays a local check.
5. ~~**SE-3 folder scheme.**~~ ✅ **Decided: sanitise + containment, not a surrogate key.** The traversal is closed without a dataset migration or a retrain, and the recognition pipeline was not touched — confirmed by the held-out run still reporting 60/60. **The consequence stands and is not a bug that was missed:** the dataset folder is still `{id}_{name}`, so renaming a student in the database without renaming their folder still breaks delete, edit and recapture (handover §1.6). Revisit with the Phase 4 data-model work, where the migration is cheaper because the schema is already moving.

---

## 8. Review

*To be completed as phases land.*

| Phase | Completed | Notes |
|---|---|---|
| 0 | ☑ 2026-08-08 | Restored service. Root cause was deeper than the interrupted write: `neighbors=12` made the model both unpersistable (PE-0) and unmatchable (all distances above threshold). Model 1.835 GB → 55 MB, held-out 0/60 → 60/60, predictions 26× faster. Also fixed a `test_accuracy.py` bug that made it score 0 images, and rewrote `docs/walkthrough.md` with reproducible numbers. **Two data blockers remain** (missing student rows, empty subjects table) — operator action, not code. |
| 1 | ☑ 2026-08-08 | Foundation. Deleted 143 dead files (incl. `hello_flutter/`, `haarcascade/`); `pyproject.toml` with exact pins and a hard `<3.12` bound for mediapipe; `config/settings.py` (pydantic-settings) absorbing all credentials, paths and the recognition threshold; 178 `print()` → `logging` with hot-path diagnostics at DEBUG; 35 unit tests and a clean `ruff` gate. Held-out accuracy unchanged at **60/60, avg 34.95** — the refactor is behaviour-neutral. Caught an import-shadowing bug that would have broken every DB call at request time while passing every other check (L5). New finding **FS-12**. CI deferred at the user's request. |
| 2 | ☑ 2026-08-08 | Security. Every one of the 35 routes is authenticated and role-checked by a **default-deny** hook, so a route added without a marker is refused rather than served — and a test walks `app.url_map` to prove it. Passwords are bcrypt hashes **verified in Python**, which is what actually closed SE-15: `ADMIN`, `AdMiN` and `admin   ` were re-measured against the live database and are now rejected. Dataset paths go through one validated helper that proves containment in `dataset/`. CSRF everywhere, destructive routes POST-only, generic error pages. Tests **43 → 205**, and **48 of the 74 new route tests fail against `main`** — the fixes are demonstrated, not asserted. Held-out accuracy **unchanged at 60/60, avg 34.95**: the phase is recognition-neutral. Four new findings (FS-6, FS-13, SE-16, SE-17) found and fixed. **Not done:** the surrogate-key folder scheme (user's decision — out of scope), so §1.6's rename coupling survives. One incident, [L6](lessons.md). |
| 3 | ◧ | **Half done, 2026-08-08 — 2 of 8 items.** MA-4 and MA-12. `vision/` now holds the face-geometry gate, the quality gate and the per-track state machine, none of which needs a camera to test. **MA-4 landed as one implementation with two declared profiles, not one threshold set** (user's decision): enrolment collects 50 of its 100 images per student off-axis, and recognition's frontal-only rule would have made those stages uncollectable. Proven behaviour-preserving by differential test against the pre-refactor code — 0 mismatches on the recognition gate over 40,000 randomised meshes, and the enrolment gate's 105 traced to integer truncation *by experiment*, not inference ([L7](lessons.md)). MA-12: `generate_frames()` **405 lines / depth 10 → 115 / depth 3**, `recognize_face.py` 1817 → 1261. The recognition loop was driven end-to-end headless for the first time. Tests **205 → 286**; held-out unchanged at 60/60, avg 34.95. Three corrections to the Phase 2 handover, one of which ([L8](lessons.md)) had the sprint planned around a warning that was false. **Remaining: RE-2, PE-4, PE-6, PE-7, PE-5/US-2, SE-12, `docs/benchmarks.md`** — see [`handover-phase-3a.md`](handover-phase-3a.md) §6. |
| 4 | ☐ | |
| 5 | ☐ | |
| 6 | ☐ | |
