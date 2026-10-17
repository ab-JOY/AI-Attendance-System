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
