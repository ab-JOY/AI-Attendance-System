# Audit — demo readiness for the pre-final defense

**Date:** 2026-08-29
**Scope:** full tree, end to end. Driven against the live database and the real
camera on this machine.
**Rescoped 2026-08-29 (user):** the dev machine's environment, database and
cameras do not describe the demo machine, which is **complete, running, on its
own database instance, and retrained on every version update.** Machine-shaped
findings are dropped; findings in code are kept; **D1–D7 are new and exist
because of the retrain policy.**
**Read with:** [`handover-phase-6d.md`](handover-phase-6d.md) ·
[`assessment-phase-6c.md`](assessment-phase-6c.md) §2 · [`todo.md`](todo.md) §7

Published as an artifact:
https://claude.ai/code/artifact/aa64f4c1-a48c-4631-bf75-a9b1938bd355

---

## 0. What the rescope drops

**Dropped as dev-machine-only:** B1 (no virtualenv), B2 (MySQL down), B4's
hardware half (no camera present), R2's camera-scan portion, R8 (certificate
SANs pinned to this LAN), R9 (`predict()` at 107 ms), G8 (not a git repo).

**Deliberately not dropped:**

- **B3** — a separate database changes *whose* rows are empty, not whether the
  code refuses a student in no class. It becomes a check on the demo database
  rather than a measured fact, and the structural cause travels (§1).
- **B4** — the camera findings had a code half under the hardware half: the app
  accepted a frozen device and returned `success: true`. That half travels.

---

## 1. Live findings — code, not environment

| ID | Finding | Fix |
|---|---|---|
| **D1** ⚠️ new | **A deploy retrain can drop a student from the model and still exit 0.** `train_model()` skips any folder under `MIN_IMAGES_PER_STUDENT = 70`, keeps training the rest, and returns `(True, "…Skipped (too few images…): <names>")`. But `__main__` is `success, msg = train_model(); sys.exit(0 if success else 1)` — **`msg` is never printed.** An automated retrain checking the exit code sees a clean pass. The only trace is a `WARNING` in `app.log` that nothing in the deploy reads. The student stays in the database, on every roster and in the class list, and is never recognised again. Images are dropped by the *quality gates* too, so a folder can hold 100 files and still fall under 70 usable. | Two lines in `__main__`: print `msg`, and exit non-zero when `skipped_folders` is non-empty. |
| **R1** | **Liveness only advances on frames LBPH could read.** Unchanged — pure recognition-path code, still open: `recognize_face.py:943` returns before `:1004` `state.liveness.update()`. Measured live: **4 min 07 s** to record one student (74% identity churn, 15% liveness resets). | Evaluate the challenge from landmarks on every frame with a face. 6c's highest-value item. Take it early enough to re-run the evaluator. |
| **B3** | **Every student enrolled before Phase 6d is in no class list, on any database.** Structural and it travels: until 6d, `/enrol/finish` wrote the `students` row and nothing else; `web/subjects.py` is the only writer of `enrolments`. **Nothing backfills** — migration 002 created the table, no migration populates it. The register then refuses them: `save_attendance(...) -> NotRecorded.NOT_IN_CLASS`, `register_for(subject) -> 0 rows`, and ending the session writes a blank register. | Manage Students → Classes column. Anything reading "None" needs adding to a class list. 2 minutes. |
| **D2** ⚠️ new | **A failed retrain leaves new code running on the previous model.** The atomic write with `.bak` rotation means a failed retrain cannot *destroy* the model — it also means it is survivable: the deploy proceeds and recognition runs against the last successful retrain. One case fails the whole run (any folder still using `{id}_{name}` returns `False` → exit 1), but unchecked the outcome is deployed code + stale model, which is the PE-0 shape. | Assert on the artefact, not the exit code: after retraining, check `trainer/labels.txt` has one line per expected student. Catches D1 and D2 together. |
| **B4** | **A degraded camera is accepted and reported as a successful start.** Verified here: picker rejected the blank device, fell through to a virtual camera serving one still image, logged `FROZEN`, and returned `200 {"success": true}`. Dormant on healthy hardware; wakes on any device contention — another app, a USB drop, a driver wedge, or the post-hard-kill case 6c documents as unfixable. | Carry the `FROZEN` verdict into the `/start-attendance` response. Failing that, confirm the feed moves before calling the first student. |
| **R3** | **No grace period.** `derive_status()` is `"Late" if arrival > scheduled else "Present"` — one second past the scheduled minute is Late, on the overlay, register and report. | Give the demo subject a start time just after your slot, and expect the grace-period question. |

---

## 2. What the retrain-on-every-update policy changes

Beyond D1 and D2 above.

- **D3 — it genuinely improves erasure. Say so.** A deleted student survives in
  `trainer.yml` until a retrain and in `trainer.yml.bak` until a *second* one;
  `docs/data_privacy.md` describes erasure as three manual steps for that
  reason. Retraining on every deploy flushes both generations as a matter of
  course.
  This is a point in your favour and is written down nowhere.
- **D4 — the threshold is only safe while `LBPH_PARAMS` never moves.** 58.0 is
  calibrated to the distance scale those params produce. Changing `neighbors`
  or the grid would invalidate it on every machine at once, on the next deploy,
  with no error anywhere — `lessons.md` L2, the mistake that once gave 0/60
  silently. Automated retraining does not cause it; it removes the pause in
  which someone might have noticed.
- **D5 — a session started mid-retrain runs on the old model.** The reload is
  keyed on `model_signature()` and checked at *session start*. Correct
  behaviour, but a newly enrolled student is not recognised until the session
  is ended and restarted. **Do not deploy in the hour before the demo.**
- **D6 — migrations self-apply at startup, and DDL cannot roll back.**
  `AUTO_MIGRATE` defaults on. MySQL commits DDL implicitly, so a migration
  failing halfway leaves a part-migrated schema and the app **refuses to boot**.
  Loud and deliberate — and unrecoverable ten minutes before a defense.
  `mysqldump` before any update carrying migration 008.
- **D7 — retrain duration is unmeasured.** `docs/benchmarks.md` §7 does not
  even list it. ~18 MB per student, every image read with augmentation, so the
  deploy step slows as the roster grows on a curve nobody has plotted. A
  non-issue at current size; worth one measurement so the deploy has a known
  duration.

---

## 3. Check on the demo machine

Each was a finding against the dev database. The code behaves identically, so a
"no" is the same problem measured here.

1. Does every student you will demo show a **class count, not "None"** in Manage Students? (B3 — ends a demo with a blank register.)
2. Does **`trainer/labels.txt` hold one line per expected student**? (The direct test for D1.)
3. Is there **at least one instructor account**? Role separation is enforced properly and can otherwise only be described.
4. Is there **attendance history** from a previous session? Reports and the export render correctly and are empty without it.
5. What is the **scheduled start time** of the demo subject? (R3.)
6. Are there **test identities in the demo machine's `dataset/`**? Anything there is retrained into the model on *every* update — not a one-time cleanup, and it enters the accuracy figures.

---

## 4. Panel questions — unchanged by the rescope

- **G1 — liveness is replay-defeatable.** Two fixed poses, no depth/texture/blink; `vision/liveness.py` says so itself. Claim raised effort, never anti-spoofing.
- **G2 — retraining means the manuscript's figure is not the demo's model.** Sharper, not softer. Docs quote **60/60, avg 34.95** (3 identities); this tree reproduces **80/80, avg 33.46** (4). On a machine retrained per update, the demoed model is by definition not the one that produced the published number. Re-run `eval_heldout_accuracy.py` on the demo machine and quote *that*, with its date. The four caveats are the part that matters and are unaffected.
- **G4 — the scale ceiling is a decision, not an oversight.** ~18 MB/student, linear predict cost, ~280–400 ms/face/frame at 30 students, ceiling near 31.
- **G5 — privacy is documented, not implemented — except erasure (D3).** No consent record, no encryption at rest, no automated retention. Check the demo machine for `dataset/_migrated_duplicates/`; this tree holds 300 duplicate face images there in folders named after the students.
- **G6 — no password reset.** The admin hash here is not the shipped default, so it was changed. Confirm the demo machine's password works well before the day; recovery means editing the database.
- **G7 — dead code contradicts the docs.** `capture_dataset.py` (43 KB) still exists although `docs/limitations.md` and a comment in `tests/test_dataset_paths.py` say Phase 5 deleted it; `build/lib/` is a stale duplicate of most of the tree.
- **G10 — logout does not end a session** (deliberate, 6c §2). After a re-login, starting a *different* subject is refused until the running one is ended.

---

## 5. Verified working — machine-independent

`ruff` clean · **1347 tests pass** (1307 unit + 40 integration against real
MariaDB, the only ones exercising production SQL) · every admin page renders
200 · schema current through migration 007 · model loads and matches at
distance 17–23 against a threshold of 58 · CSRF enforced · access control
denies by default, with a test walking the whole URL map · the FS-16
session-subject guard refuses a mismatched end with 409 · Phase 6d class-list
UI live on both student screens · **model writes are atomic with a backup
generation**, so no failed retrain can destroy a working model · startup
failures name the one thing to fix.

---

## 6. State left behind on the dev machine

MySQL was started for this audit and **shut down cleanly**. One
`attendance_sessions` row created while driving `/start-attendance` was
**deleted**. The scratch integration database was dropped by its own fixture.
`dataset/`, `trainer/` and `.env` were not modified.
