# Handover — Phase 6c → next

**Sprint:** Phase 6c — the camera lifecycle (the wedging fix)
**Date:** 2026-08-21
**Status:** ✅ Three defects fixed and verified. **Scoped deliberately:** this
sprint did the camera lifecycle only. The liveness and pipeline findings are
assessed but **not** implemented.
**Read with:** [`assessment-phase-6c.md`](assessment-phase-6c.md) — the full
evidence, and the two items still open,
[`handover-phase-6b.md`](handover-phase-6b.md), [`lessons.md`](lessons.md) —
**L27 is new**.

---

## 1. What was fixed

All three are §3 of the assessment. None needed a new dependency and none moved
a calibrated threshold.

### 1.1 The camera was never released at process exit — the wedging

There was no `atexit` hook, no signal handler and no shutdown path anywhere in
`app.py`, and `logout()` is `session.clear()`. A session still running when the
process ended left `cv2.VideoCapture` open, so the OS kept the device claimed
after the process was gone. Windows then reported the camera as **healthy while
refusing to open it** — `0xA00F429F<WindowShowFailed> (0x8007001F)` — which is
indistinguishable from a hardware fault and sent the investigation to drivers
and reboots.

`RecognitionSession._ensure_released_at_exit()` registers a release hook the
first time a camera is opened, and only then, and only once.

**Verified in real subprocesses, all three cases:**

| Process ends by | `capture.release()` called? |
|---|---|
| Falling off the end (normal exit) | ✅ yes |
| `KeyboardInterrupt` (what Ctrl+C raises) | ✅ yes |
| `Stop-Process -Force` (hard kill) | ❌ **no** |

⚠️ **The third row is a real limit, not an oversight.** No userspace code can
run after a hard kill. The OS is supposed to reclaim the device; a camera that
stays wedged after `taskkill /F` is a driver problem. The docstring says this
because it was measured, not because it sounded plausible.

### 1.2 The stream slot leaked — the relogin lockout

`generate_frames()` releases the viewer slot in a `finally`, which reads as
airtight and is not: a generator's `finally` runs when it is **closed or
collected**, and for an MJPEG response whose browser has navigated away that is
not prompt. Until it happened, `acquire_viewer()` raised `SessionBusy` and the
operator was told the stream was "already open in another window" when no such
window existed. **No amount of logging out and back in could clear it** — the
slot belongs to the process, not the login. That is the reported symptom.

Now: `note_viewer_frame(token)` heartbeats once per frame from
`generate_frames()`, and `acquire_viewer()` takes over a slot idle for more
than `VIEWER_STALE_SECONDS = 10`.

**The single-viewer invariant is not weakened.** Taking over is safe precisely
because the holder is not streaming — the invariant exists to stop two
generators advancing the tracker at once, and one that has stopped iterating
advances nothing. Two tests pin the edges: a live-but-slow stream is never
evicted, and a reclaimed generator calling `release_viewer()` later **cannot**
evict its replacement.

### 1.3 A dead camera said nothing

`CameraReader._read_failures` was counted from the day the class was written
and **nothing in the codebase ever read it** — the property had no caller. A
camera that died mid-session produced a silent permanent stall. Now:
`_consecutive_read_failures`, one `ERROR` on the transition (never per frame),
an `INFO` when frames return, and `is_delivering` so a caller can tell "no face
in shot" from "no camera".

---

## 2. ⚠️ Deliberately not done — and why

**`logout()` still does not stop the session.** The assessment named it, and I
did not implement it, because it is a **behaviour decision rather than a bug**:

- Stopping recognition on logout would let one operator logging out on their
  own laptop **end a class register in progress at the kiosk**. Attendance
  already written survives, but the session stops and the remaining students
  are not recorded.
- The lockout it was meant to fix is addressed by §1.1 and §1.2 without that
  risk.
- What remains after a relogin is `start()` refusing a *different* subject
  while one is running — *"a session for … is still running. End it first."*
  That is arguably correct: it stops frames already counted toward one subject
  being silently reattributed to another, and the UI has an End Attendance
  button.

**If you want logout to end the session, say so** — it is four lines. It should
be a decision recorded in `todo.md` §7, not something I slipped in while fixing
a device leak.

---

## 3. Still open, unchanged by this sprint

From [`assessment-phase-6c.md`](assessment-phase-6c.md), **neither
implemented**:

- **§2 — the liveness challenge only advances on frames LBPH could read.**
  Proven by execution: same pose, same yaw, `index 1` with a match and
  `index 0` without, while the 8 s timeout runs regardless. This is ~74% of the
  reported 4-minute delay and is a handful of lines. **This is the highest-value
  remaining item.**
- **§4 — LBPH is linear in stored images.** Measured at ~0.1 ms per image:
  ~280–400 ms per face per frame and a ~625 MB model at 30 students, against
  52 ms and 72 MB today. This reopens `todo.md` §7 Q1 with evidence that did not
  exist when Q1 was decided, and Q1 is the user's.

---

## 4. Verified state

| Thing | Value |
|---|---|
| Test suite | **1252 passed, 50 skipped** (1240 before; 12 new). Skip count unchanged — still the git-blob baseline |
| `ruff check .` | clean |
| Files changed | `vision/session.py`, `infra/camera.py`, `recognize_face.py`, `tests/conftest.py`, `tests/test_camera_lifecycle.py` (new) |
| Breaking changes for existing callers | **None.** `RecognitionSession.__init__` gained two optional keyword arguments (`clock`, `register_exit_hook`), both defaulted; every existing construction still works unchanged |

---

## 5. Traps for whoever is next

- **`tests/conftest.py` now has an autouse fixture that patches
  `atexit.register`.** It exists because dozens of test sessions with fake
  hardware were installing real process-exit hooks, which fired during
  interpreter shutdown after pytest had closed its capture streams — a wall of
  `--- Logging error --- ValueError: I/O operation on closed file`. If you add a
  test that genuinely needs a real exit hook, opt out explicitly; do not remove
  the fixture.
- **`RecognitionSession` resolves `atexit.register` at call time, not as a
  parameter default.** A default is bound once at class-definition time and
  would ignore the patch above. Do not "tidy" it back into a default.
- **`VIEWER_STALE_SECONDS = 10` is a lease, not a timeout on the stream.** A
  stream slower than one frame per ten seconds would be evictable — at the
  ~5 fps measured there is a 50× margin, but if the pipeline ever gets much
  slower (and §4 says it will at 30 students), revisit this number rather than
  discovering it.
- **The camera on this machine is currently faulty, independently of any of
  this.** After a reboot, with the app never started, the Windows Camera app
  showed torn frames with grey filler, and OpenCV read 100% blank rows at 1 fps
  before the device stopped opening at all. That is hardware/driver, not code —
  do not read a failed camera test here as a regression from this sprint until
  the device is known good. An external USB webcam is the fastest way to get an
  unblocked test path.
