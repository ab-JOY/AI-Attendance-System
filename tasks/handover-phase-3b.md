# Handover — Phase 3, second half → Phase 4

**Sprint:** Phase 3, Recognition engine — **second half**
**Date:** 2026-08-09
**Status:** ✅ **Phase 3 complete. 8 of 8 items, plus one new finding (CAM-1).**
**Branch:** `phase-3-recognition`, 8 commits, branched from `phase-2-security`
**Read with:** [`todo.md`](todo.md), [`lessons.md`](lessons.md),
[`handover-phase-3a.md`](handover-phase-3a.md) (the first half — still worth
reading for MA-4 and MA-12)

This supersedes `handover-phase-3a.md` for anything the two disagree on, and
§4 lists **three corrections**, one of them to my own commit message.

---

## 1. Read this first — what will bite you

1. **`dataset/` and `trainer/` are gitignored and must stay that way.** Face
   images of three identifiable students and the biometric templates derived
   from them. Both checks were run today and both work:
   ```bash
   # 1. Ignore rules still in place? (expects exactly 2 matches)
   [ "$(git check-ignore dataset trainer | wc -l)" -eq 2 ] && echo SAFE || echo STOP

   # 2. After staging, before committing - anything sensitive slip in?
   git diff --cached --name-only | grep -qE '^(dataset|trainer)/' \
     && echo "STOP - sensitive data staged" || echo "SAFE"
   ```
   Never use `git check-ignore -q` with more than one path.

2. **`gt_db/` is now a decided part of the plan and is also gitignored.** The
   Georgia Tech Face Database, 50 subjects × 15 images, is the Phase 6
   impostor set (user, 2026-08-09). Same rule: never commit it. It carries its
   own usage terms, separate from RA 10173 consent — check them before
   publishing any figure derived from it.

3. **There are still two uncommitted files that are the user's, not yours.**
   `git status` shows `M .gitignore` and `?? tasks/notes.txt` — the same two
   the last handover flagged. They add `gt_db/` to the ignore rules and record
   where it came from. **I left them alone again.** They are *protective*
   (the ignore rule is what keeps 750 face images out of git) so they are
   worth committing, but that is the user's call and I did not take it. **Ask.**

4. **`import recognize_face` is now 1.57 s and `import app` 3.18 s**, so
   `tests/conftest.py` no longer bans importing them. Prefer testing `vision/`
   and `infra/` where you can — both are hardware-free and their tests run in
   milliseconds — but the ban is gone with the reason for it.

5. **A route with no access-control marker is refused, not served.** Unchanged
   from Phase 2, and it caught nothing this sprint only because I remembered.
   `/train_status` needs `@authenticated` **and** `@json_api`.

6. **Never put a real student identifier in a test.** [L6](lessons.md). The
   smoke harness has to use a real enrolled ID — the model knows three faces —
   so it stubs `save_attendance` *and* arms a tripwire on the database. When
   PE-7 moved the write onto a pool, the existing tripwire silently stopped
   covering it; both seams are armed now. **If you move a database call, check
   what was watching the old one.**

7. **Renaming a student still breaks their dataset folder.** Scheme is still
   `dataset/{student_id}_{name}`. Deliberate, `todo.md` §7.5, revisit in
   Phase 4 where the migration is cheap.

8. **The camera situation is specific and you will waste an hour on it.**
   See §6.

---

## 2. Verified current state

Every number measured today on this branch at `805a3d0`. Nothing carried
forward except where the table says so.

| Check | Result |
|---|---|
| `ruff check .` | **clean** |
| `pytest tests/` | **444 passed in 75.7 s** (286 at the start of this half) |
| `pytest -m "not slow"` | **421 passed, 23 deselected, in 28.7 s** (was 202 / 16.4 s) |
| `eval_heldout_accuracy.py` | **60/60, avg distance 34.95** — identical to Phases 0–3a |
| `import recognize_face` | **1.57 s / 55 MB** (was 9.15 s / 168 MB) |
| `import app` | **3.18 s / 95 MB** (was 12.16 s / 204 MB) |
| Recognition loop | **6.8 fps** with a face, **20.5 fps** with none — *first measurement* |
| Route table | **36 routes** + `static`: 3 public, 10 authenticated, 23 admin-only, **0 unclassified** |
| Model | `trainer/trainer.yml`, **52.4 MB**, 3 identities |
| `git check-ignore dataset trainer` | 2 matches — SAFE |
| `git status` | 2 files, **both the user's** — see §1.3 |
| Python | 3.11.5 |

**Still cannot be verified by an agent:** live camera capture, live enrolment,
browser UI rendering, **and the new liveness challenge** (§6).

### Re-establish the baseline before you start

```bash
git checkout phase-3-recognition
.venv/Scripts/python.exe -m ruff check .              # expect: All checks passed
.venv/Scripts/python.exe -m pytest tests/ -q          # expect: 444 passed
.venv/Scripts/python.exe eval_heldout_accuracy.py     # expect 60/60, avg 34.95
git status --porcelain                                # expect ONLY the user's 2 files
```

If held-out does not reproduce 60/60, stop and diagnose. Phase 3 did not touch
the recognition *pipeline* — only the code around it — so any movement means
something else did.

---

## 3. What changed

Eight commits. The three that will affect your work most are marked ⭐.

### `c6f975c` — the headless harness, committed before the rewrites

`generate_frames()` is the largest block in the codebase and had no test.
Phase 3a drove it with a throwaway script; this commits it as
`tests/test_recognition_loop_smoke.py`, skipped when `dataset/` is absent
(always, in CI).

Composite size was measured, not guessed: at 240 px MediaPipe found 1 face in
100, at 520 px it found 100 and the full path passed 100 with distances
17.7–28.9. **The capture stages are visible in the file numbering** — images
1–25 frontal, 26–40 left, 41–55 right — which is what makes a liveness answer
scriptable.

**It was checked against the negative case** ([L4](lessons.md)): the same
session with the turn removed still tracks and still confirms, but liveness
does not pass and nothing is saved. So the liveness assertions measure the
turn.

**This paid for itself immediately.** Rewriting the loop in the next commit
broke exactly one assertion — the one reaching for a global that had moved —
and everything else passed unchanged.

### ⭐ `eb656fc` — `RecognitionSession` (RE-2, PE-4, RE-10)

Eight pieces of state into `vision/session.py` behind an `RLock`. Heavy
collaborators injected as `SessionHooks`, so `vision/` keeps its
no-camera/no-MediaPipe/no-database property.

⚠️ **The lock is not held across a frame, and cannot be** — a `/video_feed`
response lives as long as the operator leaves the page open, so `stop()` would
never return. **What makes the unlocked per-frame path safe is the
single-viewer invariant**: the tracker accumulates identity votes toward an
attendance decision, so a second stream would double-count. `acquire_viewer()`
refuses it and `/video_feed` answers **409**.

⚠️ That refusal happens in the *route*, not the generator. A generator body
does not run until Flask iterates it, by which point the browser has been
promised `multipart/x-mixed-replace` and "no" is no longer expressible. If you
add another streaming route, the same applies.

⚠️ **Two behaviour changes to watch in the live run:**
- Starting a **different subject** while a session runs is now **refused**
  (was: silently overwrote `current_subject`, mis-attributing frames already
  counted toward the first subject). The operator must end the session first.
- A second `/video_feed` is **409**, not a second stream.

PE-4: model and MediaPipe both deferred. Model cached on `(mtime, size)` —
size too, because Phase 0's outage was a partial write and a truncated
`trainer.yml` can share an mtime with the write that produced it ([L1](lessons.md)).

### `98d265f` — CAM-1: refuse a camera that shows nothing

New finding, §2.9 of `todo.md`. **Two false starts, both caught by running it
against the real device rather than reasoning about it**, and the second is
the instructive one:

1. Judging on the *best* frame of the probe opened the dead camera — a noise
   burst had Laplacian variance 1258, twice the *working* camera's 618.
2. Judging on variation and edge content alone also opened it — a second probe
   of the same device gave median std dev 5.2 and detail 138.7, clearing both
   thresholds. **Only the brightness floor rejects it**, at median mean 0.3.

Deliberately conservative: a dim room is a bad picture and must still open, and
a test asserts that. Refusing to open a camera would be worse than the bug.

### ⭐ `df3c776` — PE-6: the camera reader

Moved to `infra/camera.py` with a `Condition`. **Read §4.3 before quoting any
number from this commit's message.**

### ⭐ `2b09367` — PE-7: pooling, behind exception safety

⚠️ **The order here is the whole finding, and it is not what PE-7 says.** See
§4.2. `db_cursor()` first, pool second. `tests/test_db_access.py` has four AST
bans, verified non-vacuous against the pre-PE-7 file.

Also moved bcrypt **off** a held connection in `/login` and
`update_instructor`. `/login` is the one route an unauthenticated attacker can
call repeatedly; holding one of five pooled connections per hash attempt would
have handed them a lever the rate limiter does not cover.

### `2882e86` — PE-5/US-2: training in the background

`/train_model` → 202, second start → 409, `/train_status` polled by
`templates/training_status.html`.

**Stages, not a percentage.** Training is ~20 s at three students; the UI shows
which stage and how long, both true, rather than a percentage that would have
to be invented ([L7](lessons.md)).

### ⭐ `805a3d0` — SE-12: liveness

The randomised sequence is the item as filed. **The bug found while doing it
matters more** — see §5.

### `<this commit>` — benchmarks, plan, handover

---

## 4. Corrections

**Check these against your plan before you build anything.**

### 4.1 — to `handover-phase-3a.md` §6, on PE-7

It warns: "29 `get_db_connection()` calls in `app.py` and 28 `conn.close()` —
find the mismatch first." **There is no mismatch.** The 29th "call" is the
`def` line, matched by a text grep; `update_instructor`'s two closes are on
mutually exclusive branches. AST count: 27 calls, 27 closes, balanced. Anyone
who spent a day hunting a missing close would have found nothing.

### 4.2 — the real PE-7 hazard, which is larger

**21 of the 27 call sites were not inside a `try`/`finally`.** Against raw
connections a leak is invisible — MySQL reaps it. *Behind a fixed-size pool it
is permanent*, and after `pool_size` exceptions every subsequent request blocks
forever. **Adding the pool first would have turned an invisible inefficiency
into a hang** — this project's signature move. Exception safety had to land
first, and did.

### 4.3 — ⚠️ to my own PE-6 commit message (`df3c776`)

That message quotes a table of "frames re-processed" at assumed consumer costs
of 45 ms and 10 ms, and calls the 45 ms row "the realistic one for this
deployment."

**It is not.** Measured two commits later: the recognition loop costs
**48.7 ms with no face and 147.3 ms with one**, against a camera capped at
**33.3 ms**. The loop is *always slower than the camera* on this machine, so it
never outran it under the old code either, and **the duplicate-frame half of
PE-6 saves nothing measurable here.** Do not quote "30–81 % of work wasted".

The producer half is real and unaffected: a failing camera went from **94.7 %
of one core to 0.0 %**. The frame-ready signal still removed the consumer's
20 ms poll-sleep and made `stop()` wake a blocked reader immediately.

Corrected in `docs/benchmarks.md` §3 and written up as [L9](lessons.md): *a
benchmark's assumptions are measurements too.*

---

## 5. The SE-12 finding, because it is not what the item says

SE-12 is filed as a **replay** risk. The randomised sequence addresses that
partly, and `docs/data_privacy.md` §7.6 says exactly how partly.

**The bug found while implementing it is a false-rejection bug, and it was
live.** The old thresholds compared *frame-normalised* yaw against a constant
0.030; the geometry gate works in *face widths*, refusing a nose past 0.28w.
Those do not scale together. Measured, driving the real mesh over the enrolment
set's deliberate left-turn images:

| face box | turn actually made | old threshold needed | reachable? |
|---:|---:|---:|---|
| 208 px | 0.136w | **0.185w** | **no** |
| 260 px | 0.148w | 0.148w | borderline |
| 342 px | 0.144w | 0.112w | yes |
| 508 px | 0.154w | 0.076w | yes |

**The turn a person makes is a constant ~0.14 of their face width at every
distance** — a fact about heads, not cameras. Below roughly a 137 px face box
the old rule demanded *more turn than the gate would accept*: refused for
turning too far before being credited with turning far enough.

So **a student standing more than about a metre back could never mark
attendance**, box drawn green, nothing logged. That is precisely the failure
the user's blink-detection refusal was meant to avoid, already shipped in the
mechanic that was kept. Thresholds are in face-width units now.

⚠️ **These numbers come from stored images composited into frames, not from a
live camera.** They are the right units now, but the *values* want confirming
against a real face at a real distance — §6.

---

## 6. The camera, and what only the user can do

**Handed to the user: a live camera run of the new liveness challenge.** No
agent can do it, and SE-12 is the item that most needs it.

State of the machine as of 2026-08-09:

| index | device | what it does |
|---|---|---|
| 0 | `HD User Facing` (built-in) | opens, reads, **returns black** — the CAM-1 case, now refused |
| 1 | `DroidCam Video` | registered, client running, **`cv2` cannot open it** (14.9 s timeout) |
| 2 | `OBS Virtual Camera` | returns its "not started" placeholder |

**OBS was running and appears to hold the DroidCam device.** The user's
decision (2026-08-09) is **DroidCam direct** — so close OBS, and index 1 should
free up. With CAM-1 fixed, the 0..4 scan now skips the black webcam instead of
stopping at it, and `save_camera_index()` only records a device that passed the
probe, so `selected_camera.txt` (currently `0`, the dead one) self-heals on the
first successful open.

**What to check in the live run, in order:**

1. A session starts and the feed shows a picture, not black. The log names the
   chosen index with its measured statistics.
2. The liveness prompt cycles — `Liveness 1/2: Turn LEFT`, then a second step —
   and **varies between sessions**.
3. **Stand at a realistic distance and confirm it can be passed there.** This
   is the §5 fix, and the values behind it have never met a real face.
4. Close one browser tab mid-session and confirm the session survives (RE-10).
5. Open `/video_feed` in a second tab and confirm a 409, not a second stream.
6. Start a second, different subject without ending the first, and confirm it
   is refused (behaviour change, §3).

---

## 7. What is left

**Phase 3 is done.** Next is **Phase 4 — data model and functional gaps**
([`todo.md`](todo.md) §5). Warnings for it that this sprint generated:

- ⚠️ **RE-3's TOCTOU duplicate check is still there**, in `save_attendance()`,
  with a comment saying so. Phase 4 adds
  `UNIQUE(student_id, subject_code, attendance_date)`, which is the actual
  fix; delete the read-then-write when you do, do not keep both.
- ⚠️ **`db_cursor()` is the only way to reach the database now**, and
  `tests/test_db_access.py` enforces it with AST bans. New Phase 4 code that
  hand-rolls a connection will fail the build — that is intentional. Migrations
  and `setup_db.py` are excluded, deliberately, as pre-application scripts.
- ⚠️ **The `{id}_{name}` dataset folder scheme is unchanged** and Phase 4 is
  where the surrogate-key migration is cheapest (`todo.md` §7.5).
- ⚠️ **`attendance` currently has 0 rows** and should stay that way until the
  first real session. If you run anything that writes to it, clean up
  ([L6](lessons.md)).
- **PE-8 is still open** (pandas handed a raw DBAPI2 connection, whole table
  into memory). It belongs with FS-11 in Phase 5, and `export_excel` now at
  least returns its connection on the exception path.
- **MA-1 is still open**: `app.py` is 1749 lines. Blueprints are Phase 5.

**Cheapest high-value thing available, and it is not in Phase 4:** the GT
impostor set makes a **FAR measurement possible today**, against the current
model, without the recapture. `todo.md` §7 Q3 explains why the impostor side
does not depend on the leakage fix. If the manuscript needs a real
false-acceptance number sooner than Phase 6, that is a few hours.

**Verify at end of Phase 4:** `ruff` clean, `pytest` green, app boots, and
`eval_heldout_accuracy.py` reports a number you have explained. Then write
`handover-phase-4.md` and complete the `todo.md` §8 review row.

---

## 8. Open decisions

**Nothing in Phases 4–6 is blocked on a decision.** Settled this sprint:

1. ✅ **CAM-1 scope** (user, 2026-08-09): fix the frame-content check
   minimally; leave `selected_camera.txt` in place (retiring it is PO-3,
   Phase 5).
2. ✅ **Camera chain** (user, 2026-08-09): DroidCam direct, close OBS. No
   device-name resolution layer.
3. ✅ **Georgia Tech database** (user, 2026-08-09): the Phase 6 **impostor
   set**, not a benchmark and not training data. Recorded in `todo.md` §7 Q3
   and Phase 6.

**One thing to put to the user**, carried over from the last handover because
I did not take it either: the uncommitted `.gitignore` and `tasks/notes.txt`
(§1.3). The ignore rule is what keeps 750 GT face images out of git, so it is
worth committing — but it is theirs.

---

## 9. Suggested first move

```bash
git checkout phase-3-recognition
.venv/Scripts/python.exe -m ruff check .
.venv/Scripts/python.exe -m pytest tests/ -q     # expect 444 passed
```

Then, before writing Phase 4 code, **do the live camera run in §6** — or get
the user to. Phase 3 changed the camera path, the session lifecycle and the
liveness challenge, and every one of those was verified against composited
stored images. They are the right shape; whether they are the right *values*
is a question only a real face at a real distance answers, and finding out
during Phase 4 will look like a Phase 4 bug.

The user is running these phases in separate sessions and asked for this
document to bridge them. Assume the next reader has no memory of this one.
