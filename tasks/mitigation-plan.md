# Mitigation plan — pre-final defense

**Date:** 2026-08-29
**Source:** [`audit-demo-readiness.md`](audit-demo-readiness.md) (rescoped for the
demo machine)
**Constraint:** *do not touch live working parts of the code unless absolutely
necessary.* This plan is therefore **ordered by blast radius, not severity.**
**Status:** ⚠️ **nothing here has been implemented.** It is a plan.

Published as an artifact:
https://claude.ai/code/artifact/4f77be06-11e3-47c5-b2b1-28bbf7dbb77a

> **14 of 16 findings close without editing a working line.** Nine are data or
> procedure, three are new files, two are confined to a code path the running
> application never executes. Only **R1** edits the recognition loop.

---

## 0. Two corrections to the audit

**D1 — the warning does reach stdout.** I said it existed only in `app.log`.
Wrong, and it makes the fix cheaper. Measured by running a real retrain:

```
$ python train_model.py
… 24 lines …
WARNING __main__: These students are NOT in the trained model and will not be
recognised: 12345. Recapture their faces, then train again.
$ echo $?
0                                   <- the actual defect
duration: 17 s  (4 students, 400 images)
```

The message is the **last line** of a 25-line run. **Only the exit code lies.**
So the cheapest mitigation is a grep, not a code change — and D7 now has its
missing number: **a full retrain is 17 s** at current roster size.

**B4 — the picker already says so.** I proposed carrying the FROZEN verdict
into `/start-attendance`. Already done one screen earlier: `/cameras` returns a
`frozen` flag and `static/js/attendance.js` appends *"(still image, not a live
feed)"* to the option label. The mitigation is procedural; the code needs
nothing.

---

## Tier 0 — no code at all (9 findings)

Nothing here can regress the system, because nothing here is a change to it.

| # | Action | Closes |
|---|---|---|
| 1 | **Fill in the class lists.** Manage Students → Classes column. Every student reading "None" needs adding via Class List. No backfill exists; pre-6d students will all read None. | **B3** |
| 2 | **Make the deploy read the retrain output.** `python train_model.py \| tee retrain.log` then `grep -q "NOT in the trained model" retrain.log && exit 1`. Reliable because the warning is the last line. Total-failure case is already covered by the exit code. | **D1** |
| 3 | **Move the demo subject's start time** past your slot. No grace period exists; prepare the answer. | **R3** |
| 4 | **Use the camera picker.** Pick a device with no "(still image…)" annotation; confirm the feed moves before calling the first student. | **B4** |
| 5 | **Deploy freeze + dump.** No version update within an hour of the demo (D5: a session started mid-retrain runs the old model for its whole duration). `mysqldump` before any update carrying migration 008 (D6: DDL commits implicitly; a part-migrated schema refuses to boot). | **D5 D6** |
| 6 | **Re-measure accuracy on the demo machine's model** — `python eval_heldout_accuracy.py`, after removing test identities. Quote that dated figure, not the manuscript's. Caveats unchanged. | **G2** |
| 7 | **Record the retrain duration** in `docs/benchmarks.md`, which does not time one at all. | **D7** |
| 8 | **Write down the erasure benefit** (`docs/data_privacy.md` §7): retraining per deploy flushes `trainer.yml` *and* `.bak`, which is the manual three-step procedure. Also delete `dataset/_migrated_duplicates/` if present on the demo machine — 300 duplicate face images in folders named after students. | **D3 G5** |
| 9 | **Demo-machine checklist:** admin password works (no reset path exists); ≥1 instructor so role separation can be *shown*; attendance history exists so Reports/export are non-empty; no stray test identities in `dataset/`. | **G6 +3** |

---

## Tier 1 — new files only (3 findings)

Additive. Worst case: the new file is wrong and you delete it.

### 1.1 `scripts/preflight.py` — read-only — **D1 D2 B3**

The only mitigation that catches **D2**, since a stale model is invisible to a
grep. Assert, all read-only, exit non-zero on failure:

```
1. every student in `students` appears in trainer/labels.txt   -> D1
2. trainer.yml is newer than the newest dataset/ folder        -> D2
3. no student has zero rows in `enrolments`                    -> B3
4. every label in labels.txt has a row in `students`           -> stray test IDs
5. the demo subject's scheduled start is in the future         -> R3
6. instructor count > 0, and attendance rows exist             -> demo content
7. describe_available_cameras() reports one live device         -> B4
```

Checks 1–2 are worth keeping permanently as the last step of every version
update.

### 1.2 Pin `LBPH_PARAMS` with a test — **D4**

⚠️ **Verified hole, not theoretical: no test asserts the LBPH parameters.**
`tests/test_settings.py` pins the 58.0 threshold and its docstring explains the
threshold is calibrated against the scale `LBPH_PARAMS` produces — but nothing
pins the other half. Changing `neighbors` or the grid passes the whole suite
and CI, then silently invalidates the threshold on every machine at the next
retrain. That is `lessons.md` L2, the mistake that gave 0/60 with nothing
logged. One new test file asserting `radius=2, neighbors=8, grid 8×8`.

### 1.3 Version control — **G8** — *prerequisite for Tier 2 and 3*

No `.git` here, so no rollback, and 8 tests skip because they read a
pre-rewrite blob via `git cat-file`. ⚠️ **Never commit `dataset/` or
`trainer/`.** Confirm before the first commit:

```bash
[ "$(git check-ignore dataset trainer | wc -l)" -eq 2 ] && echo SAFE || echo STOP
```

---

## Tier 2 — code the app never executes (2 findings)

Optional; Tier 0 item 2 already covers D1. Both changes sit inside
`if __name__ == "__main__":` in `train_model.py` — a block the web app never
reaches, because the background job calls `train_model()` directly. **The
function itself is not touched.**

- **D1 permanently.** `success, msg = train_model()` discards `msg`. Printing it
  is free and cannot change behaviour. The exit code needs a decision: add a
  *trained-but-incomplete* code to `config/exit_codes.py`, or gate strictness
  behind `--strict`. Note the precedent — that module exists because
  `capture_dataset.py` used bare `sys.exit()`, returning 0 on failure, and the
  app read 0 as success and retrained (**FS-12**). D1 is FS-12 one level up.
- **G7 partly.** Delete `build/lib/` — a stale duplicate of the source tree,
  referenced by nothing (checked: tests, `pyproject.toml`, CI). **Keep
  `capture_dataset.py`** — `tests/test_capture_exit_codes.py` drives it as a
  subprocess, so deleting it is two changes, not one. Correct the docs instead.

---

## Tier 3 — the recognition loop (1 finding) — **decide, don't schedule**

### R1 — advance the liveness challenge on frames that have a face

The only item that edits working recognition code, and the difference between a
five-second identification and the measured 4 min 07 s.

**The change is small.** In `process_confirmed_track()`, the
`candidate_id is None` branch returns before the challenge is evaluated. The
yaw is already computed and already passed in, and the challenge needs only the
face-mesh landmarks, present whenever MediaPipe found a face. One to three
lines, inside a guard that already exists.

**What it cannot affect.** No threshold moves. `LBPH_PARAMS`, the model and the
recognition decision are untouched. The held-out evaluator does not exercise
liveness — which is exactly why it is the right regression check: the number
must come back **identical**, not merely good.

**Conditions — all of them, or don't take it:**

```
0. version control in place (Tier 1.3)  - no rollback otherwise
1. write the test FIRST, confirm it FAILS against today's code
2. make the change
3. pytest        -> must stay 1307 passed, 51 skipped
4. ruff check .  -> clean
5. eval_heldout  -> must be IDENTICAL
6. one timed live run: seconds-to-record, before and after
```

Step 1 is this project's own discipline, and the reason is on the record: seven
tests in `test_end_attendance_subject.py` passed throughout the two sprints
FS-16 was live, because none used the browser's ordering. A test never seen to
fail proves nothing.

⚠️ **Resist bundling the second change.** `note_identity_mismatch()` is also
called on unreadable frames, resetting the post-challenge count. Fixing that
too is defensible, but two behavioural changes in one branch means a regression
cannot be attributed. Take one, measure, then decide.

⚠️ **Go/no-go:** only with **a week of demoing on the changed build afterwards.**
If that week is not available, do not take it — rehearse the wait and say
plainly that liveness latency is a known defect with a diagnosed cause and a
scoped fix. A panel accepts that. A recognition loop broken three days out is
not recoverable.

---

## Sequence

| When | Do | Closes |
|---|---|---|
| First sitting | Tier 0 entirely | B3 R3 B4 D1 D3 D5 D6 D7 G5 G6 |
| Same day | Re-run the evaluator on the demo machine, record the dated figure | G2 |
| Next sitting | Version control → preflight script → `LBPH_PARAMS` test | G8 D2 D4 + permanent D1 guard |
| Optional | Tier 2 — `__main__` exit code, delete `build/` | D1 permanently, G7 partly |
| Decide | Tier 3 — R1, only with a week of rehearsal left | R1 |
| Day before | Full run: enrol → retrain → preflight → session → end → report → export. Screenshot it. | the rehearsal |

Each block is safe to stop after; nothing later is a prerequisite for anything
earlier.

---

## Deliberately not doing

- **A configurable grace period.** R3 closes with a subject's start time. The
  feature edits `derive_status()`, which runs on every attendance write — a
  live path, cosmetic gain, before a defense. Post-defense.
- **Plumbing FROZEN into `/start-attendance`.** Would change
  `open_best_camera()`'s return signature, which the recognition start path
  depends on. The picker already surfaces it one screen earlier.
- **Deleting `capture_dataset.py`.** A test drives it; two changes, not one.
- **Anything about the LBPH scaling ceiling.** §7 Q1 is a decision already
  taken on manuscript grounds. Present the 6c latency evidence as a stated
  limitation, do not reopen it as an engineering task.

---

## State changed while preparing this plan

A full retrain was run on the **dev machine** to measure its duration and exit
code. It rewrote `trainer/trainer.yml` atomically with a `.bak` generation;
`labels.txt` came back **byte-identical**, so model membership is unchanged.
Nothing else was modified.
