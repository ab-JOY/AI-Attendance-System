# Handover — Phase 6e → next

**Sprint:** Phase 6e — the demo-readiness audit and its code mitigations
**Date:** 2026-08-29
**Status:** ✅ **All code mitigations implemented and verified.** R1 (the
liveness latency defect), D1 and D2 (both created by the demo machine's
retrain-on-every-version-update policy), D4 (an unguarded calibration pair) and
part of G7. **The data findings were deliberately not actioned** — populating
the database is another team's work and has been handed to them (§5).
**Read with:** [`audit-demo-readiness.md`](audit-demo-readiness.md) — the
findings · [`mitigation-plan.md`](mitigation-plan.md) — the plan these came
from · [`assessment-phase-6c.md`](assessment-phase-6c.md) §2 — R1's diagnosis ·
[`handover-phase-6d.md`](handover-phase-6d.md)

---

## 0. What changed, in one table

| Finding | Change | Files |
|---|---|---|
| **R1** | The liveness challenge now advances on frames LBPH could not read | `recognize_face.py`, `tests/test_liveness_identity_hold.py` |
| **D1** | A partial retrain exits **3**, not 0, and prints its result | `train_model.py`, `config/exit_codes.py`, `tests/test_train_model_skips.py` |
| **D1 D2 B3** | `scripts/preflight.py` — read-only readiness checks with a meaningful exit status | `scripts/preflight.py`, `tests/test_preflight.py` (both new) |
| **D4** | `LBPH_PARAMS` is pinned by a test for the first time | `tests/test_lbph_params.py` (new) |
| **D3** | The erasure procedure's automatic half is written down | `docs/data_privacy.md` |
| **D7** | Retrain cost measured and recorded | `docs/benchmarks.md` |
| **G7** | `build/` deleted (stale duplicate source tree, referenced by nothing) | — |

| Gate | Result |
|---|---|
| `pytest` | **1327 passed, 51 skipped** (1307 before; **20 added**) |
| `pytest tests/integration/` | **40 passed** against real MariaDB |
| `ruff check .` | clean |
| `eval_heldout_accuracy.py` | **80/80, avg 33.46 — identical before and after R1** |

---

## 1. R1 — the liveness challenge advances on unreadable frames

**The one change in this sprint that touches the recognition path.** Diagnosed
in Phase 6c, left open there, and measured at **4 min 07 s** to record a single
student in a live session.

`process_confirmed_track()` returned from its `candidate_id is None` branch
before `state.liveness.update()` was ever reached, so the challenge could only
advance on frames where LBPH produced a usable match. That is the wrong
precondition, because the requested movement is what destroys the match: a
turning head is motion-blurred, a blurred crop is refused by
`RECOGNITION_QUALITY.min_blur_variance`, and a profile view pushes the distance
past `RECOGNITION_THRESHOLD`. The system asked for a turn and discarded exactly
the frames the turn produced, while the 8 s step timeout ran on wall-clock.

**The change is one line**, inside a guard that already existed:
`state.liveness.update(current_yaw)`, in the `candidate_id is None` branch. The
yaw was already computed and already passed in — `get_face_yaw()` needs only
the FaceMesh landmarks, which are present whenever MediaPipe found a face.

### Traps

- ⚠️ **This does not recognise anybody, and the tests say so out loud.**
  Completing the sequence sets `passed`. Attendance additionally requires
  `post_match_frames` of *real* matches, and `note_identity_mismatch()` — which
  still runs on every frame reaching this branch — holds that counter at zero.
  The challenge proves a live person moved on cue; the re-check proves it was
  still this student. **Those must stay separate.**
  `test_advancing_on_unreadable_frames_does_not_confirm_anybody` drives 20+
  unreadable frames through a completed challenge and asserts
  `identity_reconfirmed` is still False, with `save_attendance()` stubbed to
  raise. If a future change makes the challenge sufficient on its own, that
  test fails rather than attendance being recorded for a face never read.
- ⚠️ **The second half of the 6c diagnosis was deliberately NOT taken.**
  `note_identity_mismatch()` is also called on unreadable frames, resetting the
  post-challenge count, and 6c names that as related. It is a second
  behavioural change in the same branch, and bundling them means a regression
  cannot be attributed to one. **Measure the effect of this change on real
  hardware first**, then decide.
- ⚠️ **The overlay still reads "Hold still - looking for your face" on these
  frames**, which is now mildly contradictory: the student's turn counts, while
  the screen asks them to stop. It was not changed, because it is a UI decision
  rather than part of the defect, and the fix works either way — the turn is
  registered on the frames where it happens. Worth revisiting with a camera in
  front of you.
- **All three new tests were confirmed to FAIL against the pre-change code**
  (and the nine existing tests in that file to pass) before the change was
  made. That ordering is not ceremony here: seven tests in
  `test_end_attendance_subject.py` were green throughout the two sprints FS-16
  was live, because none of them used the browser's ordering.

---

## 2. D1 — a partial retrain reported success

**New finding, and it exists because of the deployment policy** rather than
because of anything that changed in the code: the demo machine retrains on
every version update.

`train_model()` skips a student folder with fewer than
`MIN_IMAGES_PER_STUDENT = 70` **usable** images, trains everybody else, and
returns `True` with the skipped names in its message. That is correct — one bad
folder must not block a class. The entry point was not:

```python
success, msg = train_model()      # msg dropped on the floor
sys.exit(0 if success else 1)     # 0 for a run that shrank the roster
```

A deploy gating on the exit status saw an unqualified pass. The student stayed
in the database, on every roster and in every class list, absent only from the
file that does the recognising — which at the camera is indistinguishable from
the recognition engine failing.

**Measured before and after, on the real dataset** (folder `12345` is empty, so
it is skipped every run):

| | before | after |
|---|---|---|
| Exit status | **0** | **3** (`EXIT_INCOMPLETE`) |
| Result message | discarded | logged at ERROR with the next step |
| `--allow-incomplete` | n/a | exits 0, for a knowingly short roster |

### Traps

- ⚠️ **`__main__` detects a partial run by reading the message, and that is
  forced, not lazy.** `train_model()` returns `(bool, str)` and
  `infra.jobs.BackgroundJob` unpacks exactly two values, so the CLI cannot be
  handed a structured list without changing a contract the web application
  depends on. The message is therefore an **interface**, and
  `SKIPPED_MARKER` is a module constant used at both ends so they cannot
  drift. `test_the_skip_marker_is_the_one_the_cli_looks_for` fails if someone
  rewords the message — without it, D1 would silently return with the whole
  suite still green.
- ⚠️ **Nothing in the web application reaches this block.** The Train Model
  button and the post-enrolment retrain both go through
  `services.training.training_job`, which calls `train_model()` directly. The
  exit-status change is the CLI contract only — which is exactly who needed
  it, because the CLI is what a deploy runs.
- **`EXIT_INCOMPLETE = 3` is in `config/exit_codes.py` beside the FS-12
  constants deliberately.** D1 is FS-12's shape at a second entry point: a
  status of 0 standing for "finished" when it means "finished with part of the
  work not done". That module's docstring now says so.

---

## 3. D2 and B3 — `scripts/preflight.py`

`scripts/preflight.py` is **new, read-only, and safe to run against a live
machine at any time**, including minutes before a demo. No INSERT, no UPDATE,
no file written; the camera is opened only with `--cameras`. It exits 0 if
every check passed and 1 otherwise, so a deploy can end with it.

It catches what D1's exit status cannot: **a model that went stale**. Model
writes are atomic with a `.bak` rotation, deliberately, so a failed retrain
cannot destroy a working model — the cost of that safety is that a failed
retrain is *survivable*. The application starts, recognises against the
previous model, and looks entirely healthy while its roster is a release out of
date. No exit code sees this, and neither does any screen in the UI, because
every screen reads the **database** and the database is not what recognition
uses.

**Run live against the dev database, it independently reproduced five of the
six findings the audit had found by hand**, each with its remedy:

```
[FAIL] every student is in the model        1 of 4 missing: 12345 (test student)
[PASS] model is newer than the dataset      trainer.yml is newer than every image
[FAIL] no stray identities in the model     trained but not a student: test-111
[FAIL] every student is in a class list     4 in no class: …
[FAIL] there is something to demonstrate    no instructor account; no attendance rows
[FAIL] the demo subject starts later today  CS401 started at 08:00:00, which has passed
```

### Traps

- ⚠️ **`COUNT(e.subject_id)`, never `COUNT(*)`**, in the classless-student
  query. A LEFT JOIN with no match still supplies one row of NULLs, so
  `COUNT(*)` reports 1 for a student in no class at all — the exact opposite of
  what the check asks. Same trap as the Classes column in
  `repositories/students.py`; it is commented in place.
- ⚠️ **An unreadable model FAILS rather than passing vacuously.** "Nothing is
  missing" is trivially true of a model that does not exist.
  `test_an_unreadable_model_fails_rather_than_passing_vacuously` pins it.
- ⚠️ **The model is parsed from `labels.txt` directly, not via
  `recognize_face`.** This has to work on a machine where the model is
  unloadable, which is one of the things it checks for.
- **`_migrated_duplicates` and any `_`-prefixed folder is skipped**, as the
  training run skips it. Counting it would report a student who does not exist
  and could make a current model look stale.
- The tests stub the cursor entirely (`FakeCursor` matches on a fragment of the
  SQL, not on call order) — **no database, no seeds to keep in step with a
  schema.**

---

## 4. D4 — `LBPH_PARAMS` was not pinned by anything

Found while writing the plan, and it was a live hole rather than a theoretical
one. `tests/test_settings.py` asserts `RECOGNITION_THRESHOLD == 58.0` and its
docstring explains that the value is calibrated against "the LBPH distance
scale in `train_model.LBPH_PARAMS`". **Nothing asserted the scale.** Changing
`neighbors` or the grid passed the entire suite and CI, then moved the distance
scale out from under a threshold a test was busy defending.

That is `lessons.md` L2 / PE-0: `neighbors` 8 → 12 once pushed genuine
distances from ~35 to 81–107 against an unchanged 58.0, and recognition
returned **0/60 with no error logged anywhere**.

`tests/test_lbph_params.py` is new and closes it. **Verified by making the
historical mistake**: setting `neighbors = 12` fails all three tests; reverting
passes them.

⚠️ **Retraining on every version update makes this sharper, not milder.** A
parameter change now reaches every deployed model on the next release, with no
human pause in which somebody might have noticed a number looking wrong.

---

## 5. ⚠️ Handed to the other team — NOT actioned here

**Populating the database is not this team's work**, so every data finding was
left alone deliberately. None of them is fixed. All of them are checkable with
`python scripts/preflight.py`:

| ID | What they need to do | Why it matters |
|---|---|---|
| **B3** | Add every student to a class list | The register is scoped to `enrolments`. With none, recognition refuses every face with *"Not in this class"* and ending a session writes a blank register. **Nothing backfills** — until Phase 6d, `/enrol/finish` never wrote the relation, and no migration populates it. |
| **R3** | Set the demo subject's start time past the demo slot | `derive_status()` has **no grace period**. One second past the scheduled minute is Late, on the overlay, register and report. |
| — | Add at least one instructor | Role separation is enforced and currently cannot be demonstrated. |
| — | Run one full session so history exists | Reports and the Excel export render correctly and are empty. |
| — | Remove stray test identities from `dataset/` | `test-111` is trained into the model with no `students` row. **On a machine that retrains every release, a stray folder comes back every release** — this is not a one-time cleanup. |

---

## 6. Verified state, and what was NOT touched

**Untouched:** every threshold, `LBPH_PARAMS`, the model, the enrolment capture
path, `vision/` except through its existing public surface, every web route,
every repository, every template. The only edit to a live recognition path is
the single `update()` call in §1.

⚠️ **`recognize_face.process_confirmed_track()` changed behaviour, not
signature.** Callers are unaffected. What changed is that a challenge can now
complete without a single readable frame — which is the point, and which the
§1 traps qualify.

⚠️ **Still no version control in this working copy.** There is no `.git`, so
there is no rollback and eight tests still skip because they read a pre-rewrite
blob via `git cat-file`. Backups of the two files edited in a live path were
taken to `%TEMP%/aa-backup/` before editing, which is a substitute and not a
replacement. **Getting this tree under git is still the single most useful
thing to do before any further change.** `dataset/` and `trainer/` must never
be committed.

⚠️ **Nothing here was run against a working camera.** The dev machine's
cameras produce no picture (audit B4), so R1's effect on the real 4 min 07 s
was **not** measured end to end — only that the challenge now advances on
unreadable frames, that recognition is unchanged, and that the suite is green.
**Measure the live timing on the demo machine before quoting an improvement.**

⚠️ **The `.env` in this copy is the dev machine's.** The demo machine has its
own database instance; none of the numbers in §3's live run describe it.

---

## 7. Open

- **R1's live effect is unmeasured.** See §6. This is the first thing to do on
  the demo machine.
- **The second half of the 6c liveness diagnosis** (`note_identity_mismatch()`
  resetting the post-challenge count on unreadable frames) — deliberately not
  taken, §1.
- **The overlay message during unreadable frames** — §1, needs a camera.
- **`capture_dataset.py` still exists** although `docs/limitations.md` and a
  comment in `tests/test_dataset_paths.py` say Phase 5 deleted it. Kept: a test
  drives it as a subprocess, so removing it is two changes, not one. The
  documentation is what is wrong.
- Everything still open from 6c: **LBPH is linear in stored images** — ~280–400
  ms per face per frame at 30 students, which reopens §7 Q1 with evidence that
  did not exist when Q1 was decided. **Q1 is the user's.**
