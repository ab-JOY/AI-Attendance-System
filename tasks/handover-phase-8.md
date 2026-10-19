# Handover: Phase 8 → next

**Sprint:** Phase 8. Deploy behind Cloudflare Tunnel + Caddy, and take
attendance from a browser webcam (§7 Q7)
**Date:** 2026-10-17
**Read with:** [`todo.md`](todo.md) PO-4, CO-3 and §7 Q7 ·
[`lessons.md`](lessons.md) L37, L38 · [`../docs/deployment.md`](../docs/deployment.md)

---

## 0. State, in one table

| | |
|---|---|
| Branches | `deploy-cloudflare-caddy` (commit `2b55546`, deployment) → `attendance-browser-camera` (browser camera, **uncommitted**). Neither is pushed or merged |
| Tests | **1,633 passed, 91 skipped** (full suite incl. slow, 2026-10-17, after round 2) |
| `ruff` | 17 errors, **all in `web/api.py`**, which carried the user's own uncommitted edits before this sprint. None are from this sprint |
| Schema | unchanged (no migration) |
| Deployed? | **No.** Nothing has been run on Cloudflare yet; the user is doing it from `docs/deployment.md` |
| User's own uncommitted files | `.gitignore`, `.vscode/settings.json`, `config/settings.py` (`flask_host` → `0.0.0.0`), three `mobile/` files, `tests/test_camera_selection.py`, `web/api.py`. **Left exactly as found.** The deployment commit staged only this sprint's hunk of `config/settings.py` |

---

## 1. What changed and why

### Deployment (committed, `2b55546`)

- **Google Cloud was ruled out by measurement, not preference.** A VM has no
  camera, and `RecognitionSession.start()` refuses without one, so attendance
  could not start there. Kept as `docs/deployment-gcp.md` / `deploy/gcp/`.
- **Chosen by the user: Cloudflare Tunnel + Caddy on the classroom PC.**
  `serve.py` (waitress, **one process**, 8 threads) on 127.0.0.1:8000 → Caddy on
  127.0.0.1:8080 → cloudflared. `PROXY_HOPS=2`.
- **PROXY_HOPS / ProxyFix.** Behind a proxy every visitor is 127.0.0.1, so
  SE-13's lockout would have locked an account out for everyone.
- **waitress strips `X-Forwarded-For` by default** (L37). The test client
  could not see it; real Caddy in front of real waitress did.
  `serve.server_options()` sets `clear_untrusted_proxy_headers=False` only
  when `PROXY_HOPS > 0`. `tests/test_deployment.py` runs real waitress.
- `serve.py` **refuses** `PROXY_HOPS > 0` on a non-loopback `FLASK_HOST`, since a
  LAN client could otherwise forge its address and never be locked out.

### Browser-camera attendance (uncommitted)

- `infra.camera.BrowserCamera` imitates `cv2.VideoCapture` (`read`,
  `isOpened`, `release`) and is fed by `push()`. **It feeds the existing
  `CameraReader`**, so everything after the capture is unchanged.
- `RecognitionSession.start(subject, camera=None)`, `submit_frame()`,
  `uses_browser_camera`. A supplied camera skips `configure_camera`, which
  would block reading three frames from a browser that has sent none.
- `POST /attendance/frame`, `@authenticated`. Accepted only from the login that
  started the session (`BROWSER_CAMERA_KEY` in the Flask session holds its
  `session_id`), so there's no database query per frame.
- **Frames are sized to 1280x720** (`vision.pose.RECOGNITION_FRAME_*`), not
  enrolment's 1920x1080. Recognition's face-size thresholds are pixels tuned at
  1280x720. `decode_frame()` / `canonicalise()` take a `size`.
- Page: a *Camera source* choice, a webcam picker, a frames/s readout. Uploads
  are self-paced (the next frame goes only after a reply), capped at ~8/s, and
  **never mirrored** (see `enrol.js` on what mirroring did to liveness).

---

### Browser-camera attendance, round 2 (after the user's trial through Cloudflare)

- **Reported:** "super laggy, it can't recognise", and "sometimes the webcam
  won't open". **Measured:** ~0.75 frames/s through the tunnel (L39).
- **The page no longer opens /video_feed in browser mode.** It shows its own
  webcam and draws the results from each upload's reply (`faces`:
  `recognize_face.overlay_as_json`). Recognition runs on a server thread
  started at Start (`start_upload_recognition`), holding the viewer slot, with
  uploads as its heartbeat (`RecognitionSession.submit_frame`).
- **Uploads are 960 wide at JPEG 0.6** (44 KB, not 139 KB), with 2 in flight.
  The smoke test pins those settings and still confirms and passes liveness.
- **Webcam switching:** stale getUserMedia results are stopped and ignored (a
  request counter), and NotReadableError retries once after 700 ms.
- **Result through a real tunnel:** 4.8–5.8 frames/s, with a box drawn in 126 of 132
  replies. The classroom-camera mode is unchanged and still uses /video_feed.
- `tests/test_tls_settings.py` now ignores the machine's `.env` (`_env_file=None`).
  The deployed `.env` sets SESSION_COOKIE_SECURE=true, which broke it.

### Round 3: "after End and Start again: the camera is only 640 pixels wide"

- **Cause:** `/end-attendance` rendered `attendance.html` itself, without the
  camera settings. The page then asked the webcam for an ideal width of **0**, and the
  browser opened its smallest mode (640x480 on the user's laptop, 96x96 in Edge's
  fake camera). The page's minimum-width check compared against 0 and passed.
- **Fix:** both routes render through `web/sessions.py::_render_attendance_page()`.
  An `ast` guard test allows exactly one `render_template("attendance.html")`;
  it was mutation-checked by restoring the old call.
- **Also:** the page remembers the last webcam that worked (localStorage), and
  if a camera it opened automatically is too small, it tries the device's other
  webcams. Start refuses a webcam under 960 wide before any session exists.
- **Verified in Edge:** Start→End three times, each reopening at 1280x720
  (~8 frames/s); a forced 640x480 webcam is refused at Start, and no session is created.

## 2. Findings this sprint

| | |
|---|---|
| **L37** | waitress deletes `X-Forwarded-*`. Any header- or address-dependent behaviour must be tested through the real server |
| **L39** | Through the tunnel the first browser-camera design ran at ~0.75 frames/s. Measure the real network path first |
| **L38** | After a reload, uploads resumed at 7.7/s while **nothing was recognised**: the new stream got a 409 for the slot the old page still held, and was never retried. Fixed by retrying a refused stream while the session runs. Affects classroom-camera reloads too |
| Hidden `<video>` | Uploads are drawn from the preview element, so it is kept at one invisible pixel (`.is-feeding`) rather than `hidden`. Verified that it keeps playing in Edge |
| Mobile endpoint | `/api/attendance/frame` has **no liveness** and uses distance **70.0** where the calibrated threshold is 58.0 (`lessons.md` L2). Pre-existing, **not fixed**, noted in `deployment-gcp.md` §1 |

---

## 3. Verified, and how

- Real Caddy + real `serve.py` + local MariaDB: the lockout stays per-visitor, the
  request reaches the app as `https`, and the guard refuses `0.0.0.0`.
- Real Edge, fake camera, **scratch database** `test_browsercam_scratch`
  (created and dropped; the real one was never written): webcam opens at
  1280x720, ~7.5 frames/s, the hidden video keeps playing, the annotated stream renders,
  resume after reload works (viewer 3 opens after the 409), End writes the register.
- Real enrolled student's frames, JPEG q85, through `BrowserCamera` + real
  `CameraReader` + the real loop: confirmed, liveness passed, recorded once.
- Mutation-checked: the camera re-delivering a frame, a classroom session accepting
  uploads, any login feeding the session, uploads sized for enrolment. All four
  were caught.

## 4. Not verified

A physical external webcam. The Cloudflare leg, and MJPEG through it. Upload
rate on the school connection. Autostart task, Caddy as a Windows service.
**Whether JPEG compression shifts LBPH distances across all students:** one
student passing is not an FAR/FRR figure.

## 5. Warnings for the next agent

- **Do not run `python app.py` alongside `serve.py`.** Two processes means two
  sessions fighting for one camera.
- The single-viewer slot drives recognition. Anything that opens, reloads or
  replaces `/video_feed` must be driven in a real browser (L38).
- Loading `trainer/` (smoke test, full suite, starting a session) can make a
  concurrent retrain fail on Windows (L36). Tell the user before running those.
- `start()` returns True for an already-running subject **on whichever camera
  it started with**. `/start-attendance` now refuses a camera mismatch; keep that.

## 6. Open decisions (the user's)

1. **Commit and merge** `attendance-browser-camera` after the user's trial.
2. **Manuscript:** attendance may now use either camera; say which source
   produced each evaluation figure (§7 Q7).
3. **RA 10173:** Cloudflare decrypts traffic in transit, including enrolment frames and
   the browser camera's frames. It belongs in `docs/data_privacy.md`.
4. Whether to fix the mobile endpoint's missing liveness and its 70.0 threshold.

---

## 7. Addendum 2026-10-19: face capture through the Cloudflare tunnel

**Reported:** capture opened through the tunnel never saved an image (0 of 100,
no box). **Measured:** this PC uploads through the tunnel at **15-70 KB/s**, and a
400 KB body took 5.7 s and 25.7 s, against 0.016 s locally. `enrol.js` sent
1920x1080 at JPEG 0.92 with one frame in flight, so the stage hold never completed.
This is L39 again, for enrolment.

- **`enrol.js` steps down only when replies are slow:** 1920/0.92 → 1280/0.8 →
  `MIN_NATIVE_FRAME_WIDTH` (960)/0.7, after 3 consecutive replies over 700 ms. On localhost
  it stays at 1920/0.92, so localhost captures are unchanged. Quality is
  never below 0.7, because JPEG blocking inflates the Laplacian the blur gate reads.
  The page says when it has stepped down.
- **Every gate still runs on the saved crop at every tier.** A smaller upload cannot save
  a crop below the blur, brightness or contrast floor. It can only be refused more often.
- **Provenance:** `/enrol/frame` decodes with the new `infra.uploads.decode_native()` and
  records the real upload width and bytes. The log line on finish or cancel
  reads e.g. `from 160 frames (960px x150, 1920px x10), 48 KB average`.
  **A capture built from 960 frames has less detail than one from 1920.** If any
  evaluation figure uses such students, say so.
- **Bug found and fixed: the "idle" timeout was a total-duration cap.**
  `EnrolmentSession.age_seconds` counted from Start, so a capture still receiving frames
  was swept at 300 s. It now counts from the last frame. Test added and mutation-checked.
- **Not verified:** the step-down in a real browser (no browser automation
  installed), the real frame sizes from the user's webcam, and how many images the blur gate
  refuses at 960. The finish log line answers all three after one real capture.
- **Tests:** fast suite 1,602 passed, 90 skipped. Slow enrolment smoke tests pass.

### Round 2 (same day): only the face is uploaded

- **Measured first real tunnel capture** (`23-1-1-0918`, Jeff's **real** dataset, recaptured by
  the user, not a test student; the old images are gone): 8 min 10 s, 268 frames
  (1920 x3, 1280 x3, 960 x262), 60 KB average, 1.8 s/frame, about 33 KB/s up. The
  step-down worked. The idle-timeout fix was necessary (8 min > 300 s).
  **Jeff's dataset now comes from 960-wide uploads.** Recapture on localhost if any
  accuracy figure depends on him.
- **`enrol.js` now sends only the face region**, the last returned box plus a 35%
  margin. The rest of the frame is flat grey (96), at full frame size, so geometry is
  unchanged. Every 15th upload, and any upload after a reply with no box, is the
  whole frame (re-acquisition, and the many-faces gate). Measured on a real webcam
  screenshot at 960: 46 → 24 KB (q0.7), 98 → 45 KB (q0.92). That's a ratio from a
  screenshot, not live camera bytes.
- **Guard: `EnrolmentSession._crop_was_sent()`.** The browser names the region in
  `X-Face-Region` (upload pixels). `infra.uploads.canonical_region()` maps it,
  rounding inward by 1 px. A mask of it goes through **the same `align` hook** as the
  face, and the crop is refused (`ALIGN_FAILED`, hold reset) unless every crop pixel is 255
  or `align` raises. An unreadable header is a 400, not ignored. Both refusal paths are
  mutation-checked: the unit tests cover the strict pixel check, and the real-face slow
  test (real MediaPipe and `align_face`) covers the raise path.
- **Fast suite:** 1,619 passed, 90 skipped. Slow enrolment tests pass.
- **Still not verified:** a real capture with masking, in a browser. Its
  finish log line gives frames, widths and KB.

---

## 8. Addendum 2026-10-19: instructors capture faces for their own classes

The user asked for it, removed it while tunnel capture was broken, and asked for it
again once tunnel capture worked. This settles who may change the biometric model
(the `/train_model` comment): **instructors may capture, but manual retraining stays
admin-only.** An instructor's finished capture retrains automatically, as an admin's does.

- The five `/enrol*` routes are `role_required('admin', 'instructor')`.
  `_instructor_refusal()`, checked at `/enrol` **and** `/enrol/start`, allows an existing
  student only if they're on one of the instructor's class lists
  (`enrolments_repo.is_in_class_of`), and a new student only if they're placed in at least
  one class the instructor teaches. An existing ID sent as "new" is still a recapture
  (finish renames and replaces images), so it gets the same ownership check.
- `_owns_capture()`: only the login that started a capture (`CAPTURE_OWNER_KEY` token)
  can send frames, finish or cancel it. Any admin can still act on any capture.
- Page: `GET /class_capture` (sidebar *Face Capture*), `templates/class_capture.html`.
  `enrol.html` returns to `done_url` by role, and `training_status.html` hides Retrain from
  non-admins.
- Merged by hand with the tunnel work in `web/enrolment.py`: the owner check runs
  before the body is read in `/enrol/frame`. `tests/test_instructor_capture.py` (21).
  Both guards were mutation-checked after the merge (5 and 3 failures). Fast suite:
  1,644 passed, 90 skipped.
- **Open (the user's):** the manuscript's access-control description.
  `docs/data_privacy.md` §3 is updated.

---

## 9. Addendum 2026-10-19: one subject, several sections (DM-10)

See `todo.md` §13. Uncommitted, on `attendance-browser-camera`.

- **Schema unchanged.** A `subjects` row is still one offering for one section.
  The Add form creates several in one submit: `sections-<n>-program_id`,
  `-year_level`, `-section`, `-day`, `-time_in`, `-time_out`. A form with no
  `sections-<n>-` fields is read as one section from the plain names. The Edit
  form and the existing tests use that.
- **Trap: `db_cursor(commit=True)` commits on an early `return`.** Any refusal
  inside a multi-row write must raise (`web/subjects.py::_Refused`).
- `subjects_repo.all_subjects()` is ordered by code, name, program, year and section,
  not newest first, so the template can group with `loop.changed`.
- `day_picker.html` takes an optional `day_field` (default `day`).
- New `static/js/subjects.js`. Without it, the form still works for one section.
- **DM-11 (same day):** Instructor moved into each section row. One code has one name:
  Add refuses a second spelling, and Edit renames all sections of the code
  (`subjects_repo.name_for_code` / `rename_code`). `rename_code` compares as BINARY;
  a plain `<>` misses case-only renames under this collation (mutation-checked).
