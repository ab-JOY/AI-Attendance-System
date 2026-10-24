# Handover: phone attendance on the classroom's rules + the classroom code

**Date:** 2026-10-24 · **Branch:** `attendance-browser-camera` · **Nothing here is committed** (the user did not
ask). Supersedes the "not re-applied" notes in [`handover-mobile-students-only.md`](handover-mobile-students-only.md);
read that one for the history and the reverted work. Plan and checkboxes: [`todo.md`](todo.md) §18.

## 0. State

| | |
|---|---|
| Tests | Fast **1,895 passed, 98 skipped**. Slow **47 passed, 1 skipped** (incl. 8 real-face phone tests) |
| App | Bundles (esbuild, no missing imports). **Not run on a phone** |
| Server | **`serve.py` must be restarted** for any of this to be live (L42). New dependency: `segno==1.6.1` (installed in `.venv`, declared in `pyproject.toml`) |
| User's own uncommitted files, untouched | `.vscode/settings.json`, `config/settings.py`, `tests/test_camera_selection.py`, the `.docx` |

## 1. What changed and why

User's request: recognise the student against their captured dataset "even in a different position", a liveness
check that is "not buggy, smooth", and "the app won't be used at home".

1. **Phone frames go through the classroom pipeline** (restored from the backup of the reverted 2026-10-22 work,
   `C:\projectss\backup-mobile-work-2026-10-22\`). `services/phone_verification.py`: own FaceMesh + tracker per
   phone, label map = the signed-in student's labels only (a friend's face never confirms), the LBPH model trained on
   `dataset/` is the reference. Identity rules unchanged: 58.0, 85% agreement, frontal, 2-step liveness, 8-frame
   re-check. Phone steadiness overrides (5 s timeout, any face continues the track, 12-frame window, 5 in a row) are
   the "different position" part. Per-thread CLAHE (`face_preprocessing.py`), square preview letterboxed
   (`infra/uploads.py::letterbox`), `describe_confirmation_block(window=)`.
2. **Smoother liveness on the phone (presentation only - no threshold changed).** `PhoneResult` and the frame
   route now return `stage` (finding/matching/liveness/recheck/done), `step` (LEFT/RIGHT/CENTER) and `progress`
   (0..1), from `progress_of(state)`. The app draws an arrow over the preview and one progress bar, instead of
   making the student read "Liveness 1/2: Turn LEFT" mid-turn. `mirror={false}` is set explicitly on the front
   camera so LEFT is the student's own left, as on the classroom camera.
3. **Classroom code** (`services/classroom_code.py`). HMAC(secret derived from SECRET_KEY, session key, 20 s step)
   → 6 digits; current and previous step accepted. Shown as a QR + digits on `/attendance` while a session runs
   (`/attendance/class-code`, only to the instructor who owns the running subject or an admin - `_may_use_subject`).
   App: Scan (back camera, `barcodeScannerSettings`) or type → `POST /api/attendance/checkin` → 180 s pass
   (in memory, per session). `/api/attendance/frame` refuses with 403 `need_code` **before any face is judged**.
   10 wrong codes per student per session → locked (429). `/api/attendance/live` returns `pass_seconds`.
4. Order of refusals in the frame route: no session (409) → already recorded today (done/already) → no pass (403)
   → face pipeline.

Tests: `test_api_checkin.py` (30, new), `test_api_attendance_frame.py` (10, restored + adapted),
`test_phone_verification.py` (23: restored + `progress_of` on a real TrackState/LivenessChallenge + failed-save),
`test_phone_verification_real.py` (8, slow, restored), `test_api_once_a_day.py` (moved onto a fake pipeline),
`test_uploads.py` (+3 letterbox). Mutation-checked: removing the pass check or the owner check fails 3 tests.

## 2. Corrections to earlier notes

- `lessons.md` L33's addendum (square preview, letterbox) was false after the revert and is **true again** now.
- The old route's `NotRecorded.ERROR` → 503 branch is gone with the one-photo path. The pipeline already retries a
  failed save (`defer_attendance_retry`) and the phone service marks only `attendance_saved` tracks - pinned by
  `test_a_failed_save_marks_nobody_and_keeps_the_phone`.

## 3. Warnings for the next agent

- **Mirror direction is untested on a real phone.** `mirror={false}` is expo-camera's documented default; if a
  phone still saves mirrored photos, "turn LEFT" never passes and the challenge times out and redraws. First thing
  to check on a device.
- **Timing on a phone is unmeasured.** Synthetic: 23 frames to record with phone rules. At 2-3 frames/s ≈ 10-15 s
  plus the turns. Pass is 180 s, app timeout 90 s.
- Passes and wrong-try counts are in memory: a server restart makes students scan again (by design).
- The QR is redrawn only when the code changes; the countdown updates on the 2 s live poll.
- Changing any status string: L43 (`status_color()` keys on prefixes; the app now prefers `step`/`stage`).

## 4. Open decisions (the user's)

1. **Relay of the code.** A classmate can send a photo of the code to someone at home, valid up to ~40 s. The
   remote person must still pass face + liveness as themselves, live. Stronger options not built: allow-list the
   campus network's public IP (`CF-Connecting-IP` through the tunnel) - strong if the campus has a fixed IP; GPS
   geofence - spoofable and 10-50 m off indoors.
2. Code period (20 s) and pass length (180 s) are defaults, not measured choices.
3. **Manuscript:** the phone flow is now check-in (QR) → face stream with liveness; the old "one photo" description
   is wrong. Diagrams/use cases need the classroom code.
4. Still open from earlier: `/api/attendance/live` shows every recognised student's name and ID to any student
   (RA 10173); expired token not handled in the app; APK build steps in the older handover §3b.

## 5. Same evening: phone speed (user: "finding the face is too slow, verification too")

Measured, 2026-10-24:
- Classroom camera unaffected: verified in **7.1 s** at 23:10 (last night 5-8 s). The user's "slower" report
  coincided with 0.4 GB free RAM of 7.7 GB and the laptop on battery.
- A tunnel round trip costs **0.7-1.3 s whatever the size** (21-50 KB). Server: **239 ms/frame** median on
  real faces, almost all LBPH predict. One frame per request ⇒ ~1 frame/s ⇒ 25-35 s per verification.
- A phone returned a **768 square**; the 960-wide minimum refused it. Phone route now uses
  `decode_letterboxed()`: refuse only a frame that would be enlarged (square < 720).

Changed: the app photographs continuously and sends everything taken since the last request (≤ 4,
`MAX_FRAMES_PER_REQUEST`), oldest first, one request at a time; the route judges them in order and stops at
done. 720 square at JPEG 0.6 (30 KB; 960/0.8 was 60 KB) - recorded after the same 48 real-face frames at
960/0.8, 720/0.8, 720/0.6, 720/0.5. Through the tunnel: 1 frame ~0.9 s, 4 frames ~1.8 s per request.
Server logs one line per phone verification (`Phone verification for ...: N frames in T s (F frames/s
received), server M ms per frame`), also for phones that stop or are cut off by the session ending.

**Not yet measured on the phone.** Read that log line after the user's next try: frames/s received < 1.5 means
the phone's capture or 4G uplink is the limit, not the server. Identity and liveness rules unchanged.

## 6. 2026-10-25 00:15 - count fell from 10/12 back to 2

Log: 32 frames in 9 requests over 44.5 s (one request per ~5 s over 4G), phone camera 735 ms/picture, server
390 ms/frame (classroom camera running beside it). Gaps between batches crossed the 5 s track timeout, so the
track restarted. `PHONE_TRACK_TIMEOUT_SECONDS` 5 -> 15. Real-face differential in
`test_phone_verification_real.py`: batches 6 s apart record at 15 s and never at 5 s. Watch next: the liveness
step timeout (8 s, global `LIVENESS_CONFIG`) at this request rate - a delay over 8 s redraws the challenge.

## 7. 2026-10-25 00:24 - head turns slow: the challenge was redrawn 4 times

Log: "timed out on step LEFT/CENTER after 8 s" x3 in one attempt (47 frames, 13 requests, 69.5 s). Cause: the
batch sent after a new instruction held pictures taken *before* the student saw it, so the turn reached the
server one request (~5 s) later, ~10 s after the step began. App-only fix: on any change of (stage, step) the
queue is emptied and a picture begun before the answer arrived is dropped. Server rules unchanged. Not yet
measured on the phone. If it still times out, the remaining lever is a phone-only liveness step timeout
(security trade-off - the user's decision).

## 8. 2026-10-25 - phone needs 1 frame to start (was 3)

User chose option 1 only: `real_face_confirm_frames` 3 -> 1 in PHONE_TRACK_OVERRIDES (~4 s at 0.5 frames/s).
Not an identity check. Re-check NOT changed (user asked to remove it; explained that turn frames carry no
identity check, so without it a photo + someone else's head turns would record the student; offered 8 -> 3,
not chosen yet). Matching window 12 unchanged. Test subjects sundaytest/2/3 (ids 52-54, Sunday only) created
in the DB for testing; the user will delete them.
