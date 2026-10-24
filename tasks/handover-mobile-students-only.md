# Handover: mobile app made student-only → next (phone verification)

> **Superseded in part, 2026-10-24:** the multi-frame pipeline and square preview are back, plus a classroom
> code. Current state: [`handover-phone-classroom-code.md`](handover-phone-classroom-code.md).

> ## ⚠️ ALL OF THIS WAS REVERTED BY THE USER, 2026-10-22 14:45
>
> The user asked to "undo all the update except the committed" and return to the
> app that only takes a photo with the camera. Every code change below was undone:
> the tree is the last commit plus the user's own uncommitted files (`.vscode`,
> `config/settings.py`, `tests/test_camera_selection.py`, the `.docx`, and their
> email-field removal in `StudentRegistrationScreen.js` / `web/api.py`).
> Fast suite after the revert: 1,734 passed. `serve.py` restarted 14:46.
>
> **Backup of everything reverted:** `C:\projectss\backup-mobile-work-2026-10-22\`
> (`all-uncommitted-tracked-changes.patch` - includes the user's own hunks too -
> plus copies of the new files and a tar of the edited ones). Kept: this file and
> the lessons L33 addendum, L41, L42 in `lessons.md`.
>
> ### Re-applied at the user's request, 2026-10-22 16:00 (uncommitted)
>
> - **Students-only, no registration** - as in §1 points 2-4 below: no
>   `/api/students/register`, `/colleges_programs`, `/enrol/*`, `/students`,
>   `/subjects`, `/dashboard`; `/api/login` students only; `student_required` on
>   every route; app screens restored from the backup's students-only versions.
> - **New: once per class per day.** `/api/attendance/frame` refuses a student
>   already Present/Late for the running subject today **before any face is
>   judged** - session list first, then the register
>   (`attendance_repo.recorded_status_today()`, same key and Absent rule as
>   `recorded_today()`), failing closed (503) if the register is unreadable.
>   `/api/attendance/live` returns `your_status`; the app shows "already recorded"
>   instead of the Verify button.
> - The `NotRecorded.ERROR` fix is back: without it the once-a-day rule would
>   lock a student out for the day with no row.
> - **Not re-applied:** the multi-frame pipeline, square preview, 58.0/liveness,
>   per-thread CLAHE. Phone attendance is still **one photo at 70.0, no
>   liveness, the session's shared detector**.
> - Tests: `test_api_students_only.py` (24), `test_api_once_a_day.py` (11; removing
>   the register check fails 4). Fast suite 1,764 passed. `serve.py` restarted 16:01.
>
> ### Classroom verifying made more tolerant, 2026-10-22 (uncommitted)
>
> User: "a few movement cancels the verifying". Classroom/browser camera
> (`TRACK_CONFIG`, recognize_face.py): one weak frame no longer zeroes the
> 8-in-a-row run (`TrackState.note_weak_prediction`; only a different student
> breaks it); window wiped after **12** weak frames (was 3); track timeout
> **3.0 s** (was 1.5). Unchanged: 58.0, 20-frame window, 85%, 8 in a row,
> frontal-only, liveness, mismatch limits. 2 new tests in
> `test_vision_tracking.py` (mutation-checked). Fast 1,766 passed; slow 39
> passed. **Not tried on a live camera yet**; serve.py needs a restart.
>
> ### Website login: role dropdown removed, 2026-10-22 (uncommitted)
>
> Panel feedback: choosing "User Type" before every sign-in was slow. The web
> login (`templates/login.html`) now has only ID + password. `web/auth.py::login`
> fetches both `admin_by_username` and `instructor_by_id`, re-checks the
> identifier exactly (SE-15), and the account whose bcrypt hash the password
> matches decides the role (admin tried first). An instructor ID equal to an
> admin username still signs in as the instructor unless the admin's password
> is typed. The `role` form field is ignored; the placeholder-swap JS in
> `static/js/app.js` was removed. Tests: `test_login_without_role.py` (10; 4
> fail against the old login). Fast suite 1,776 passed. Mobile app login was
> already ID + password (students only). serve.py needs a restart.
>
> ### Instructor IDs with letters + password recovery, 2026-10-22 (uncommitted)
>
> - Add Instructor field was `type="number"` (browser refused letters; column is
>   VARCHAR(100)). Now text; server checks `[A-Za-z0-9._-]{1,100}`
>   (`INSTRUCTOR_ID_PATTERN`, web/instructors.py) - a `/` or space would break
>   `/edit_instructor/<id>`. Existing live IDs are all digits, so all pass.
> - Forgotten **instructor** password: already handled - admin, Manage
>   Instructors -> Edit -> new password (forces change at next login).
> - Lost **admin** password: new `scripts/reset_password.py` (server access
>   only): `--list`, or `admin <username>` / `instructor <id>` -> random
>   temporary password printed once, bcrypt, `must_change_password = 1`, exact
>   identifier. Never run against the live DB in testing.
> - Tests: `test_instructor_id_format.py` (9; 5 fail on the old code),
>   `test_reset_password_script.py` (9). Fast suite 1,794 passed.
>
> ### Instructors manage their own class lists, 2026-10-23 (uncommitted)
>
> Expert panel request. The admin's Class List routes (`/subject_enrolments`,
> `/enrol_student`, `/enrol_section`, `/unenrol_student`) now also take
> instructors, guarded by `_refuse_other_instructors()` in web/subjects.py
> (`is_taught_by`, 403 before any repository call). Face Capture links each
> class's Class List; the empty-roster dialog gives instructors the button.
> DM-12 placement rules unchanged. `test_instructor_class_list.py` (14; guard
> removed -> 7 fail). Fast suite 1,806 passed. **Not tried in the real app;
> serve.py needs a restart.** Details: todo.md §17.
>
> Of the findings below, these are **still open**: the one-photo check at 70.0
> with no liveness, and the session's shared detector used from a request
> thread. (Registration, staff logins and the `NotRecorded.ERROR` bug are fixed
> again; the middle-name bug went with the registration screen.) Sections 2+
> describe the reverted work, not the current code.

**Sprint:** review of the mobile app, then: remove self-registration and face
capture from it, and make it student-only.
**Date:** 2026-10-21 / 22
**Read with:** [`lessons.md`](lessons.md) L41 · [`handover-fs20.md`](handover-fs20.md)
(capture sessions) · [`handover-phase-8.md`](handover-phase-8.md) (deployment, tunnel)

---

## 0. State

| | |
|---|---|
| Branch | `attendance-browser-camera`. **Nothing from this sprint is committed** - the user did not ask |
| Tests | Fast suite **1,757 passed, 98 skipped** (`-m "not slow"`). New: `tests/test_api_students_only.py` (24), `tests/test_api_attendance_frame.py` (4) |
| On a phone | **Not tried.** No React Native harness; the app was only checked to bundle (esbuild, no missing imports) |
| User's own uncommitted files, untouched | `.vscode/settings.json`, `config/settings.py` (`flask_host` 0.0.0.0 - but `.env` sets `FLASK_HOST=127.0.0.1`, which wins), `tests/test_camera_selection.py`, `docs/System_Limitations_and_Recommendations.docx`. Their uncommitted email-field removal lived in `StudentRegistrationScreen.js` and `/api/students/register`, both now deleted |
| How the user runs it | `deploy\cloudflare\start-demo.ps1` (quick tunnel, link changes each run) + `npx expo start` in `mobile/`, Expo Go, server URL pasted in the app's Settings. **Not** `python app.py` |

## 1. What changed and why

1. **`/api/attendance/frame` no longer reports a database failure as success.**
   `NotRecorded.ERROR` fell through to "Verified! Marked as NotRecorded.ERROR" and
   `mark_recognized()`, locking the student out with no row. Now a 503 and no mark
   ([web/api.py](../web/api.py), the `if not status:` branch).
2. **No registration or face capture in the app or `/api`.** `/api/students/register`
   was `@public`: anyone with the tunnel link could create a student, put a face in
   `dataset/` and trigger a retrain. `/api/enrol/*` let a student replace their own
   images. Removed with `/api/colleges_programs`. Only admin, or an instructor for
   their own classes, captures - on the website. See L41.
3. **The app is student-only.** `/api/login` issues student tokens only (403 with a
   "use the website" message for staff); `student_required` refuses staff tokens on
   every route, including ones issued before the change (24 h expiry).
   `/api/students`, `/api/subjects`, `/api/dashboard` removed; `_instructor_scope`
   removed and its 5 tests in `test_instructor_scope.py` replaced by a pointer.
4. **App:** role buttons, register link, `StudentRegistrationScreen`,
   `FaceCaptureScreen`, the staff dashboard and staff History/Monitoring branches
   removed. A stored staff login is cleared on start. **Login no longer hangs on a
   wrong password** (axios throws on 401; it was uncaught).
5. Docs: `docs/data_privacy.md` §3 gains the Student (mobile) row.

Mutation-checked: the new tests fail against the committed `api.py` (18 of 24) and
with the role check removed (8).

## 2. Next: phone verification "like the system does" - planned, NOT started

The user wants phone attendance as reliable as the classroom camera. Explained and
agreed in outline; the user will start it on the next session.

### ✅ Items 1-13 BUILT 2026-10-22 (uncommitted)

- `services/phone_verification.py` - `PhoneVerifications`: per-phone FaceMesh +
  `FaceTracker` (timeout **5 s**, not 1.5 - see below), label map filtered to the
  signed-in student, per-phone lock, `MAX_PHONES = 4` (429), 30 s idle expiry,
  every phone dropped when `(subject_id, session_id)` changes; `_close()` takes the
  phone's lock so a sweep cannot close a FaceMesh mid-`process()`.
- `/api/attendance/frame` runs it; decodes at 1280x720; `/attendance/live` releases
  phones when no session runs. **No `_recognition_lock`**: `face_preprocessing.py`
  CLAHE is now **per-thread** (identical settings), so phone and classroom run in
  parallel. The plan below said "hold the lock" - superseded.
- App: Monitoring streams frames (1280 wide, no shutter sound), shows the server's
  instruction, lighting tips, Stop button, 90 s timeout, stops on blur/session end;
  Late is coloured as Late.
- Tests: `test_phone_verification.py` (16, fakes), `test_api_attendance_frame.py`
  (rewritten, 9), `test_phone_verification_real.py` (4, slow, **real faces**: own
  face recorded through window + liveness + re-check; signed in as another enrolled
  student → nobody recorded; 2 s between frames still confirms; **the classroom's
  1.5 s timeout would not** - the differential behind 5 s). Removing the label
  filter fails both identity tests - and the real one then *records the wrong
  student*. Fast suite 1,784 passed; smoke + multi-face 27 passed.
- **Measured (synthetic composites, L25 applies):** 48 frames to record; server
  ~232 ms/frame median. ≈16 s at 3 fps, 24 s at 2, 48 s at 1. **Not measured on a
  real phone** - that is item 14/15, next.

### ✅ Square preview, 2026-10-22 (user: "arm's length is too hard")

The 16:9 strip forced a phone held upright to fit a face into a short band. The
preview is now a **square** with a 72% face guide; the app sends a 1080 square
(`frame.js`, `FRAME_ASPECT = 1`); the route **letterboxes** it into 1280x720
(`infra/uploads.py::letterbox`) instead of `canonicalise()`'s crop. Square = 1.78x
the strip's height, so the face can be that much closer. Older 16:9 uploads are
unchanged. Real-face tests: a close-up filling the guide is recorded when
letterboxed; the same frames cropped to 16:9 record nobody. L33 has an addendum -
preview shape, app crop and server fit must change together.

### ✅ Faster, steadier phone rules, 2026-10-22 (user: "too slow", "Unknown" at 6)

**Decided by the user:** phones get their own steadiness rules
(`PHONE_TRACK_OVERRIDES` in `services/phone_verification.py`). Causes found:
3 weak frames in a row wiped the count; any weak frame reset the in-a-row count;
a face jumping position/size between photos started a new track at zero.
- Overrides: timeout 5 s; any face continues the track unless ~6x size change
  (`min_iou` 0, `max_distance` 10000, `min_size_similarity` 0.15); count wiped only
  after 12 weak frames; 3 provisional, **12-frame window**, 5 in a row.
- **Unchanged (security core), pinned by a test:** 58.0, 85% agreement, mismatch
  limits, liveness + its 8-frame re-check, the label map.
- "Unknown" on a phone now reads "Hold still - verifying 6/12";
  `describe_confirmation_block()` takes the track's own window (was hardcoded /20).
- App: takes the next photo while the previous uploads (one request in flight,
  in order); uploads 960 square, capture quality 0.7.
- Measured, synthetic faces following the prompts: **23 frames (phone rules) vs 33
  (classroom rules)**. Real tests: a shaky hand (side-to-side jumps + blur bursts)
  is recorded with phone rules and **not** with the classroom's; signed in as
  another student still records nobody. Fast 1,791 passed; smoke 27 passed.
- **Known limit worth telling the panel:** with the label map filtered, a different
  enrolled face on a *confirmed* phone track reads as "no match" rather than "a
  different student", so it does not reset the challenge (the classroom resets
  after 2 such frames). The 8-frame re-check still needs the student's own face.
  Not changed.

### ✅ Near-live capture, 2026-10-22 (user: "make it live, it is much faster")

App only, no server change. `expo-camera` has **no video-frame API**; true live
frames need `react-native-vision-camera` + frame processors, which cannot run in
Expo Go (needs a development build / the APK). Done instead: `pictureSize` = the
smallest offered size with a short side ≥ 960 (`smallestUsablePictureSize()`;
e.g. 1280x960 instead of 4000x3000), `animateShutter={false}`, and the loop waits
for `onCameraReady`. The multi-frame verification was **not** reverted. Not yet
timed on the user's phone. VisionCamera is the next step if still too slow, at APK
time.

**Before this, the phone was one photo, threshold 70.0, no frontality, no liveness, shared
detector, 1920x1080.** The classroom path is 5 provisional frames, a 20-frame window
at ≥85% agreement + 8 consecutive, 58.0, frontal only, 2-step liveness, 8 post-match
frames, 1280x720.

**Plan presented to the user:**
- Per-phone context reusing `recognise_frame()` (recognize_face.py) - own FaceMesh
  and `FaceTracker(TRACK_CONFIG)`, the session's recognizer and subject.
- **Pass a `label_map` holding only the signed-in student's labels.** LBPH still
  compares against everyone, so another enrolled face's nearest label is not in the
  map → weak → never confirms. That is what stops a friend recording Jeff.
- Hold `_recognition_lock` around it: `preprocess_for_lbph()` uses one module CLAHE
  object (data race). Cost: the classroom stream slows while a phone verifies.
- Decode at `RECOGNITION_FRAME_WIDTH x HEIGHT` like `web/sessions.py`.
- Expire idle phone contexts (~30 s) and all of them when the session's
  `session_id` changes; close their detectors; cap concurrent phones.
- Don't write `context.recognized` directly from the phone path; call
  `session.mark_recognized()` after a save.
- App: stream frames while verifying, show the server's per-frame status, stop on
  done/timeout; upload 1280 wide.

**Risks to measure, not assume:** phone frame rate (≥1 frame per 1.5 s or tracks
expire - `TRACK_CONFIG.timeout_seconds`); total time (estimated 15-30 s); the 8 s
liveness step timeout at low fps.

**Full list agreed with the user (2026-10-22), in order:** (1) verification
pipeline = items above + per-phone CLAHE copy so phone and classroom predicts run
in parallel instead of under `_recognition_lock`, phone cap, expiry; app streams
~2-3 fps with live server status, lighting tips, timeout. (2) Measure on the
user's phone: 1 phone vs 3, 2-3 lighting conditions. (3) Accuracy: instructors run
*Add Session* from a phone browser per student (puts phone-camera images in
training) - then re-measure. (4) Rotating classroom code. (5) Small fixes from §3.5
plus the login subtitle "with Liveness Detection". (6) Permanent domain later.

**Second step offered: a rotating classroom code** shown on the instructor's screen
and sent with the frames - the only thing here that proves *presence*. Without it a
student can verify genuinely from home. Video replay of the student also defeats
the liveness check; no defence exists in this system.

## 3. Open decisions (the user's)

1. ~~Building §2 decides two carried items~~ **Decided by the user 2026-10-22**
   ("make the verifying like the system does"): phones use 58.0 and liveness.
   Closes `handover-phase-8.md` §6.4.
2. Whether to add the rotating classroom code.
3. **Manuscript:** the mobile app no longer registers or captures, and has no staff
   login. Anything describing either needs updating.
4. Sections A-D are enforced only by the website's dropdown; `parse_placement()`
   accepts any name.
5. Still open from the review, not fixed: `/api/attendance/live` shows every
   recognised student's name and ID to any student (RA 10173); the live list colours
   Late green; an expired token is not handled in the app (no 401 → login);
   `app.json` is still `com.anonymous.mobile`; the default server URL is plain HTTP.

## 3b. APK build - prepared, paused by the user (2026-10-22)

The user will polish the app first, then build. Already done: `mobile/app.json`
name `PRMSU Attendance`, slug `prmsu-attendance`, package `ph.edu.prmsu.attendance`;
`mobile/eas.json` (`preview` = APK). Not done: `eas-cli` is not installed, no
`eas login`, no build. **`expo-doctor` failed two checks - fix before building:**
missing peer `expo-font` (`npx expo install expo-font`), and 5 packages off the
SDK 57 versions (`npx expo install --check`). If the app will use plain-http
Wi-Fi, an APK blocks cleartext by default.

## 4. Warnings

- **Server changes need `serve.py` restarted (L42).** The user tested the
  square and tolerance changes against a server started before them. To keep
  the trycloudflare link, restart only the process on port 8000; Caddy and
  cloudflared can stay up. Restarted 2026-10-22 14:30 with all changes loaded.

- **Never `git checkout .` or `git stash`** - the user's own uncommitted files are in
  the tree.
- Students registered through the old app keep their accounts and images.
- The deleted screens held fixes 1 and 3 of this sprint (middle-name, section
  picker); they went with the screens, deliberately.
