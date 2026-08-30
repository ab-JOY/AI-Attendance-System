# Handover — Phase 6f → next

**Sprint:** Phase 6f — the three "verification repeats" loops, and B3's silence
**Date:** 2026-08-30
**Status:** ✅ **All three implemented, tested and measured.** FS-17, FS-18 and
SE-18, plus a UI warning for B3 added at the user's request (§4a). One finding
was deliberately **not** actioned — the 70–95 px dead band is a calibration
decision and belongs to the user (§5).
**Read with:** [`todo.md`](todo.md) §9 — the plan and the measurements ·
[`handover-phase-6e.md`](handover-phase-6e.md) §1 and §7 — SE-18 is the item
6e deferred

---

## 0. What changed, in one table

| Finding | Change | Files |
|---|---|---|
| **FS-17** | A permanent refusal latches on the track instead of being retried every frame | `recognize_face.py`, `vision/tracking.py` |
| **FS-17** | A database *error* is retried on a 25-frame backoff, not per frame | `vision/tracking.py` |
| **FS-18** | The session is seeded from today's register at `start()` | `vision/session.py`, `repositories/attendance.py`, `recognize_face.py` |
| **FS-18** | `recognized` carries the recorded **status**, so a Late student is not drawn Present | `vision/session.py`, `vision/tracking.py` |
| **SE-18** | An unreadable frame no longer resets the post-liveness identity re-check | `recognize_face.py` |
| **B3** | A session on a subject with an empty class list is **refused**, and the dropdown marks it | `web/sessions.py`, `repositories/enrolments.py`, `repositories/subjects.py`, `templates/attendance.html` |
| **R3/FS-16** | The End dropdown locks to the running session's subject | `web/sessions.py`, `templates/attendance.html`, `static/js/attendance.js` |
| **US-1/US-2** | A start refusal is a `<dialog>` carrying an "Open Class List" button, not only a notice at the top of the page | `templates/attendance.html`, `static/js/attendance.js`, `static/css/style.css`, `web/sessions.py` |

| Gate | Result |
|---|---|
| `pytest` | **1378 passed, 43 skipped** (1327 before; **51 added**) |
| `ruff check .` | clean |
| `node --check static/js/attendance.js` | clean |
| `eval_heldout_accuracy.py` | **80/80, avg 33.46 — identical to Phase 6e** |

All three re-run on the rebuilt environment (§6) and unchanged.

---

## 1. Where this came from

The user asked why verification repeats for a person who has already been
verified, and named it as recurring. It is **three independent loops**, and
only one of them is the R1 defect 6e closed. The evidence was already on disk:
`logs/app.log`, 2026-08-21 16:01–16:06, one person, five minutes, nothing
recorded —

```
16:01:25  Rejected attendance: test-111 …          ×27, one per frame, 210 ms apart
16:01:38  Dropped the locked identity test-111 after 25 consecutive frames …
16:02:00  Dropped …   16:02:28  Dropped …   (×6)
16:04:56  Liveness challenge … timed out … (restart 1)   … through restart 4
16:05:45  Rejected attendance: test-111 …          ×23, one per frame again
```

**Reproduced without a camera or a database in both directions**, which is what
made the three separable.

---

## 2. FS-17 — a rejected write was retried on every frame, forever

`process_confirmed_track()` set `attendance_saved` only when
`save_attendance()` returned truthily. `NotRecorded` is falsy by design
(US-10), and **two of its three members are permanent**: the student is not in
this class, or the model holds a face `students` does not. Nothing latched
either. `identity_reconfirmed` stayed True, so the next frame asked the same
question and got the same answer.

| | before | after |
|---|---|---|
| `save_attendance()` calls in 200 frames | **193** | **1** |
| LBPH predicts on that track | one per frame | none after the refusal |
| WARNING lines | one per frame | one per confirmed track |

**The cost was never only the log.** `predict_identity()` keyed its skip on
`attendance_saved`, so the one track that could never succeed was the one that
kept paying a full LBPH predict — 98 ms at today's roster, 280–400 ms at 30
students. **One student missing from `enrolments` held the frame rate down for
everyone else in shot.** That is PE-3 reopened at exactly the face it was
written to exempt, and it is why B3 (§5 of the 6e handover) is worse than a
roster inconvenience.

### Traps

- ⚠️ **`ERROR` is deliberately not permanent.** An unreachable database has not
  refused anything; it has failed to answer. Latching it would mark a student
  absent for an outage that lasted one frame. `PERMANENT_REFUSALS` is a
  module-level frozenset and `test_error_is_not_a_permanent_refusal` pins its
  membership, because getting it wrong is silent in both directions — `ERROR`
  in the set marks people absent for a blip, `NOT_IN_CLASS` out of it restores
  the per-frame loop with the whole suite green.
- ⚠️ **A refusal is not a success.** `attendance_saved` means a row exists; a
  refusal means one deliberately does not. `attendance_settled` is the property
  that means "stop asking" and is what the LBPH skip now reads;
  `attendance_saved` still means only what it always did. A refused student is
  never added to `recognized` — if they were, the next session would skip their
  verification entirely.
- ⚠️ **`may_attempt_attendance()` mutates.** It counts the backoff down on the
  frames it refuses; called from anywhere but the one write site, the backoff
  expires early or never.
- The retry backoff is frames, not seconds, deliberately: everything else in
  `TrackConfig` is frames, and the frame loop has no clock in it.

---

## 3. FS-18 — the session started believing nobody had been recorded

`RecognitionSession.start()` did `self._recognized = set()` unconditionally.
`adopt_completed_identity()` reads that set and exists **precisely** to stop a
student re-passing liveness for a mark they already have — and the only thing
that ever populated it was a write this process had made. So it worked within
one continuous camera run and could not fire across a restart. Stop and start
attendance, restart the app, run the second period: a student already Present
today redid the 20-frame confirmation window *and* a fresh randomised
challenge, for a write the UNIQUE index then discarded as a duplicate.

`repositories.attendance.recorded_today()` is new; it arrives through a new
`SessionHooks.already_recorded` callable because `vision/` has no database in
it and keeping it that way is what makes its tests run in milliseconds.

### Traps

- ⚠️ **`recognized` is a mapping now, not a set** — `{student_id: status}`.
  Membership tests and `set(recognized)` read identically, so only two call
  sites changed, but `.add()` no longer exists. `recognized_ids()` still
  returns a set and `/attendance/live` is untouched.
- ⚠️ **The status had to travel with the id.** Seeding ids alone would have
  made a **Late** student read "Present" on the overlay, because
  `recorded_status` falls back to Present when it knows nothing — which was
  honest while the mark could only have come from a track this process never
  saw, and would have become FS-7 the moment the register was the source. The
  within-session fallback is unchanged.
- ⚠️ **A failed read must never stop a session starting.** Losing the seed
  costs one student one unnecessary challenge; refusing to start costs the
  class their attendance. Both `_seed_recognized()` and
  `already_recorded_today()` swallow and log — belt and braces on purpose, so
  the message that names what was lost is written where the subject code is in
  scope.
- ⚠️ **The query must agree with the UNIQUE index** (RE-3) about what "already
  recorded" means: `(student_id, subject_id, attendance_date)`. If they drift,
  a student is asked twice or skipped wrongly. Commented in place and pinned.
- The seed is `dict(...)`-copied, so the frame loop cannot write through into a
  repository result.

---

## 4. SE-18 — the post-liveness re-check reset on unreadable frames

**This is the item Phase 6e deferred** (`handover-phase-6e.md` §1 trap 2, §7),
**taken on 2026-08-30 by the user's decision**, whose reason is worth keeping:
the 4 min 07 s log was recorded on a **high-resolution camera**, and the demo
may not use one.

`note_identity_mismatch()` ran on every frame with no usable match and zeroed
`post_match_count`, so attendance needed **8 consecutive** readable frames.
Expected wait at the 4.8 fps in the log:

| readable frame rate | 95% | 80% | 70% | 60% | 50% |
|---|---|---|---|---|---|
| expected wait | 2.1 s | 5.2 s | 11.4 s | 30.5 s | **106 s** |

The change is R1's split applied one stage later: an unreadable frame now
**neither advances nor resets** the re-check. The one call site left is the
contradiction branch.

### Traps

- ⚠️ **Why this does not weaken the re-check, stated exactly.**
  `post_match_count` is incremented in **one place** — `note_identity_match()`,
  on the readable path — which no frame in the unreadable branch can reach. So
  all 8 credited frames are still real LBPH matches against the locked
  identity. Removing the reset stops those frames destroying progress; it gives
  them no power to make any. A **contradiction** — LBPH reading a *different*
  enrolled student, which is the frame shape a face swap actually produces —
  still resets the counter to zero and still drops the identity at 5 frames.
  `test_the_recheck_only_ever_advances_on_a_real_match` drives 200 unreadable
  frames through a passed challenge and asserts nothing was recorded.
- ⚠️ **A comment in `recognize_face.py` used to say the separation rested on
  this reset, and it no longer does.** That comment has been corrected in place
  rather than deleted, because it was the load-bearing explanation of the R1
  fix and a future reader finding it stale would reasonably restore the reset.
  The separation never rested on the reset; it rests on where the counter is
  incremented.
- **`max_unreadable_frames_before_clear = 25` is unchanged.** A track that has
  genuinely lost its face still gives the identity up. What changed is only
  that a *gap* no longer costs progress — 25 must still be **consecutive**.
- Two of the four new tests here pass against the pre-change code as well.
  That is deliberate: they are the guards, and a guard that fails before the
  fix is testing the fix rather than guarding it. The two that measure the
  reset were confirmed to fail against the pre-change code.

---

## 4a. B3 — the empty class list is now a gate, and the End control locks

**Two guards on the same screen**, both about a session that cannot produce the
register the operator thinks they are getting. B3 itself is data work and
remains the other team's (`handover-phase-6e.md` §5).

### B3 — a session on a subject with nobody enrolled is refused

FS-3 scopes recognition to `enrolments`, so such a session refuses **every**
face with "Not in this class" and ends by writing a register of nobody. The
dropdown marks the offering **"— no class list"**, and `/start-attendance`
refuses it.

⚠️ **Built first as a warning that let the session start, then changed to a
refusal on the user's instruction.** The first reasoning — that an empty class
list is a roster nobody has filled in yet, and blocking would obstruct whoever
is setting the class up — was overruled, and the user's reasoning holds: the
session cannot produce one useful row, and that is knowable in a single query
before an operator's time, a class's time and a camera slot are spent on it.
**If a future sprint is tempted to soften this back to a warning, that is the
argument it has to beat, and it has already lost once.**

**Traps**

- ⚠️ **The gate runs before `open_session()`.** The other two refusal paths in
  that route have to close an `attendance_sessions` row they already opened;
  this one never opens it, never reaches the camera, and leaves nothing behind.
  `test_nothing_is_opened_or_started_when_it_is_refused` pins all three.
- ⚠️ **A failed check does NOT refuse.** "The database did not respond" is not
  an answer, and a gate that closes when it cannot see is worse than the thing
  it guards: a blip would become a class unable to take attendance at all.
  `open_session()` needs the same database one line later, so a real outage
  still stops the session with its own message.
- ⚠️ **`COUNT(e.subject_id)`, never `COUNT(*)`** in
  `subjects.for_selection_with_class_list_size()`. Demonstrated live rather
  than asserted — `CS401: COUNT(*) = 1, COUNT(e.subject_id) = 0` — because a
  LEFT JOIN with no match still yields one row of NULLs. **Third occurrence of
  this trap here** (`repositories/students.py`, `scripts/preflight.py`) and the
  first pinned by a test. Now that it drives a gate, getting it wrong lets
  exactly the wrong sessions start.
- **`for_selection()` was left alone and a second function added.**
  `web/students.py` also calls it and has no use for the count. They share
  `SELECTION_COLUMNS` through a derived `QUALIFIED_SELECTION_COLUMNS`.

### R3/FS-16 — the End dropdown locks to the running session

Reported by the user: choosing a different subject in the End control was
answered with a **409** after the fact. While a session runs there is exactly
one subject that control may legitimately carry, so it is now `disabled`, set
to the running subject, with its value in a hidden input beside it.

**Traps**

- ⚠️ **The lock is a convenience; the 409 is the guard, and it stays.** A
  disabled control is absent from a curl, a replayed POST and a browser with
  scripting off. What it stands in front of is R3 — a register written for a
  subject that was never taught, referentially valid and factually wrong, with
  nothing logged. `test_the_server_still_refuses_a_mismatch_regardless_of_the_lock`
  drives the route past any rendered control and asserts the 409 survives.
  **Do not read this lock as having made that check redundant.**
- ⚠️ **A disabled `<select>` submits nothing**, so the value travels in a
  hidden `subject_id`. `readonly` does nothing at all on a `<select>`.
- ⚠️ **Locked twice, in two places that must agree.** The template renders the
  lock when the page loads with a session already running; `lockEndSubject()`
  in `attendance.js` applies it after a start, because the page does not
  reload. Both produce the same DOM and the same element ids on purpose.
- The JS locks to `body.subject_id` — the subject the **server** says it
  started — not to whatever the Start form was showing.
- **The lock keys on `recognition_session.subject`**, so with nothing running
  the control is free. That case is what the dropdown exists for: ending a
  session the process has forgotten after a restart or a crash.

### The refusal had to be a dialog, not a notice

**Reported by the user:** the refusal was written into the notice strip at the
top of the page, and on a small screen the Start button is below the fold — so
pressing Start scrolled nothing, changed nothing visible, and **read as a
broken button rather than a deliberate refusal.** A check the operator cannot
see is worse than no check: it makes the system look faulty instead of
particular.

It is a `<dialog>` now, shown with `showModal()`, carrying an **Open Class
List** button that goes to the offering that was just refused.

**Traps**

- ⚠️ **This is not the `alert()` that US-1/US-2 removed, and the difference has
  to be preserved.** That was removed because it is unreadable to a screen
  reader until dismissed and *gone the moment it is*, so the message had to be
  remembered. A `<dialog>` is labelled (`aria-labelledby`), announced, Escape
  closes it, focus is trapped and returned — **and `refuse()` still writes the
  notice**, so dismissing the dialog leaves the message on the page.
  `test_the_notice_strip_survives_alongside_the_dialog` pins the second half.
  **Do not replace this with `alert()`. Do not drop the notice.**
- ⚠️ **The button's URL is built by `url_for` on the server and returned in the
  JSON**, not assembled in JavaScript. US-8: a URL pasted into a script is a
  route reference that does not move with the route, and this one is
  parameterised.
- ⚠️ **No button for a non-admin, and a different remedy sentence.**
  `subjects.subject_enrolments` is `@role_required('admin')` — verified live,
  `GET /subject_enrolments/1` answers **200 for admin and 403 for
  instructor** — so offering it to an instructor sends them to a wall and turns
  "somebody must fill in this roster" into "the system is broken" for the one
  person who can fix neither. They are told to ask an administrator instead.
- **Every start refusal goes through the dialog**, not just this one. "A
  session is already running" and "the camera could not be started" fail the
  same way for the same reason.
- **`showModal` is feature-detected.** Without it the notice is the whole
  behaviour, which is what the page did before — degraded, not broken.
- The action is cleared, not left over, between refusals: the dialog is reused
  and a stale button would send the next refusal somewhere unrelated.
- ⚠️ **`#session-dialog` must keep `margin:auto`.** A modal `<dialog>` is
  centred by the browser's own `margin:auto`, and the `*{margin:0}` reset at
  the top of `style.css` overrides it — so the first build rendered pinned to
  the **top-left corner**, which on a phone is roughly where the operator was
  already not looking. The rule restates the spec's modal geometry
  (`position:fixed; inset:0; margin:auto`) so it does not depend on a UA
  stylesheet the next reader has to know about. Caught by looking at it in a
  browser; no test in this suite would have.

**Live, against the dev database:**

```
as admin      → action = {label: "Open Class List", url: "/subject_enrolments/1"}
as instructor → action = null, "Ask an administrator to add students…"
```

### Measured live against the dev database

⚠️ **An earlier draft of this section said attendance could no longer be
started at all, because every subject had an empty class list. That was true
when measured and is no longer** — the user populated CS401 the same evening.
Re-measured after they said so:

```
CS401   (id 1, 3 students) -> STARTS   (session opened and closed again)
test123 (id 3, 0 students) -> REFUSED
attendance_sessions: still open = 0
```

So the gate does what it is for: the demo subject runs, and only the subject
that could record nobody is refused. **The lesson is about the reading, not the
gate** — a class-list count is operator state that changes between one command
and the next, so a figure taken from it is true of a moment, not of the system.
Quote it with the time attached or re-run `python scripts/preflight.py`, which
answers it live.

B3 has changed shape rather than gone away: an unenrolled roster used to be a
session that silently recorded nothing, and is now a session that will not
start. That is still worth checking before a demo, but it is no longer a
blocker on this database.

---

## 5. ⚠️ NOT actioned — the user's call

**The 70–95 px dead band, and it is the thing most likely to bite at the
demo.** `RECOGNITION_PROFILE.min_face_width` admits a 70 px face box.
`align_face()` warps every crop to 200×200 before
`RECOGNITION_QUALITY.min_blur_variance = 50` takes its Laplacian, so a small
box is **upscaled** first, and interpolation cannot restore detail the sensor
never resolved. Measured over 150 real enrolment crops:

| face box | 200 px | 160 px | 130 px | 110 px | 90 px | 70 px | 50 px |
|---|---|---|---|---|---|---|---|
| median blur variance | 1231 | 172 | 108 | 78 | 55 | 35 | 16 |
| frames passing the gate | 100% | 100% | 99% | 93% | 74% | **0%** | **0%** |

Between 70 px and roughly 95 px the geometry gate admits, tracks and can lock
an identity onto a face whose every crop the quality gate then refuses. These
are **enrolment stills** — already sharp, no motion blur — so a live frame
mid-turn is worse. On a lower-resolution camera the same student at the same
distance lands in that band.

**It is a calibration decision and it is the user's** (todo.md §7; L2 / PE-0 is
what the last unmeasured parameter change here cost). The options are to raise
`min_face_width` so the geometry gate stops admitting faces the quality gate
will refuse, to lower `min_blur_variance`, or to leave both and instruct people
to stand closer. **Whichever is chosen, re-run `eval_heldout_accuracy.py`
before and after** — that is the whole lesson of L2.

The operator-facing half is also still open: a confirmed track stuck in the
dead band draws "Hold still - looking for your face", which is the wrong
advice. 6e left the same overlay question open and asked for a camera.

---

## 6. Verified state, and what was NOT touched

**Untouched:** every threshold, `LBPH_PARAMS`, the model, `RECOGNITION_QUALITY`,
`RECOGNITION_PROFILE`, the enrolment path. The liveness *challenge* itself is
untouched — SE-18 is the stage after it. `for_selection()` is unchanged for its
other caller (`web/students.py`), and the `/end-attendance` guard is unchanged.

⚠️ **§4a is the one change that alters an existing outcome.**
`/start-attendance` now has a refusal it did not have, and it is reachable in
normal use — see the demo-machine note in §4a. Everything else in this sprint
either removes a repeat or adds a field.

**`eval_heldout_accuracy.py`: 80/80, avg 33.46, identical to 6e.** Recognition
is unchanged, which is the only claim these three changes must not disturb.

**The venv was destroyed and rebuilt while this sprint was finishing**, by
`scripts/setup.sh --recreate` — which is new in this tree and not from this
work, along with a README section and the removal of a stale test (below).
Every gate in §0 was run twice: once before the rebuild and once after it, on
the restored environment, with identical results. Nothing in this sprint
depends on the venv's contents.

**`tests/test_vision_pose.py::test_head_pose_is_unchanged` was failing when
this sprint began**, and was not from this work — it failed on a clean checkout
too. It was a differential test against a pre-extraction blob whose signature
deliberately changed on 2026-08-21, so it could only ever fail. **Another agent
has since dropped it**, which is the right call: `get_head_pose` was *meant* to
change, and `test_the_pose_verdict_does_not_depend_on_how_close_the_face_is`
asserts the property the old implementation got wrong. The suite is green.

⚠️ **This tree is under git now**, unlike at 6e — `phase-4-through-6e`, clean at
session start. `dataset/` and `trainer/` are both still ignored; verified with
two separate `git check-ignore` calls, not one with `-q`.

⚠️ **Nothing here was run against a working camera.** The dev machine's cameras
still produce no picture (audit B4). FS-17 and FS-18 are logic, and their
measurements are exact. **SE-18's real effect is a timing claim and remains
unmeasured on hardware** — the table in §4 is a model, not an observation. This
is 6e's §7 item, still open, one stage further along.

---

## 7. Open

- **Measure SE-18 and R1 together on the demo machine**, with the camera you
  will actually use. This is still the first thing to do.
- **The 70–95 px dead band** — §5. The user's decision, and the one with a
  deadline attached to it.
- **The overlay during unreadable frames** — §5, needs a camera.
- **Check `enrolments` before a demo**, with `python scripts/preflight.py`
  rather than from memory. §4a turned B3 from a session that silently recorded
  nothing into one that will not start, so an unenrolled subject is now loud
  instead of silent. As of 2026-08-30 CS401 has 3 students and starts;
  `test123` has none and is refused, which is the gate working. ⚠️ **This
  count is operator state and changes between commands** — an earlier draft of
  §4a quoted it as "every subject is empty", which was true when measured and
  was stale within the hour.
- Everything still open from 6c: **LBPH is linear in stored images** — §7 Q1 is
  the user's.
