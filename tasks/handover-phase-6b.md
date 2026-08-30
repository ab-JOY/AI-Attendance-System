# Handover — Phase 6b → next

**Sprint:** Phase 6b — Defect I: "retraining is not working on the UI"
**Date:** 2026-08-21
**Status:** ✅ Fixed, verified end-to-end on real data through the code path the
button drives. One defect, one root cause, no follow-on rounds.
**Read with:** [`handover-phase-6a.md`](handover-phase-6a.md) (still required —
**except §1.4, which §4 below corrects**), [`lessons.md`](lessons.md) — **L26 is
new and came out of this sprint**.

Still true from 6a: **there is no git here.** `git checkout --` is not an undo,
and 8 tests skip because they read a pre-refactor blob with `git cat-file`.

---

## 1. The defect, and why it was not where it was reported

Reported as a UI failure. **The UI was never at fault.**
[`static/js/training.js`](../static/js/training.js), the `/train_model` route,
`/train_status` and the `BackgroundJob` were all correct and all did exactly
what they were written to do — including displaying the real failure message,
which is how the root cause was visible in the log at all.

Two guards, written in different sprints, each pointed at the other:

- [`train_model.py`](../train_model.py) refuses the **whole run** if any
  `{student_id}_{name}` folder exists in `dataset/`, and the failure message
  names the remedy: `python scripts/rename_dataset_folders.py --apply`.
- [`scripts/rename_dataset_folders.py`](../scripts/rename_dataset_folders.py)
  **refused that exact case**, because the target `{student_id}` folder already
  existed: `[REFUSE] … already exists; merge them by hand`.

There was no third step. Every retrain — from the button, from `/train_model`,
from the CLI, and the automatic one at the end of an enrolment — failed in
**0.0 s**. From [`logs/app.log`](../logs/app.log), three separate attempts:

```
ERROR train_model:272  3 dataset folder(s) still use the old {student_id}_{name} naming: …
ERROR train_model:278  Run: python scripts/rename_dataset_folders.py --apply
INFO  infra.jobs:188   train_model finished (failed) in 0.0s
…
ERROR rename_dataset_folders:130 [REFUSE] 23-1-1-0559_Chrizol D. Evangelista - 23-1-1-0559 already exists; merge them by hand
ERROR rename_dataset_folders:139 3 collision(s). Nothing was renamed.
```

Neither file was wrong read on its own. That is [L26](lessons.md).

---

## 2. What the data actually was

`dataset/` held three `{id}_{Name}` folders beside their three clean `{id}`
counterparts, 100 images each.

**Verified byte-for-byte identical, twice, independently:** SHA-256 over all
100 files in each of the three pairs (3/3 identical, run before any change),
and then again by the script's own per-file comparison. Their *total byte
counts* also matched — which is why `folders_are_identical()` compares contents
and not sizes. Equal size is not evidence.

---

## 3. What changed

| File | Change |
|---|---|
| [`train_model.py`](../train_model.py) | New `QUARANTINE_DIR_NAME = "_migrated_duplicates"`; the dataset scan skips that directory **by name** (an INFO line, not the "invalid dataset folder" warning it would otherwise emit on every run). |
| [`scripts/rename_dataset_folders.py`](../scripts/rename_dataset_folders.py) | New `folders_are_identical()` / `_files_are_identical()`. `plan_renames()` now returns a **4-tuple** — `(renames, quarantines, skipped, refusals)` — and resolves a collision it can prove is safe by moving the leftover into `dataset/_migrated_duplicates/`. A collision whose contents **differ** is still refused, still with a non-zero exit, still moving nothing. Docstring updated: it now claims four properties, not three. |
| [`tests/test_rename_dataset_folders.py`](../tests/test_rename_dataset_folders.py) | **New, 16 tests.** Covers identity (including same-total-size-different-bytes and the sub-directory case), quarantine planning, the still-refused differing collision, the occupied-quarantine-slot refusal, dry-run-changes-nothing, apply, idempotence, and that `train_model()` ignores the quarantine directory. |
| [`docs/data_privacy.md`](../docs/data_privacy.md) | Erasure is now **four** steps, not three — see §5 below. |
| [`tasks/todo.md`](todo.md) §8, [`tasks/lessons.md`](lessons.md) | Phase 6b review row; lesson L26. |

**Nothing was deleted.** The only filesystem calls are `os.rename` and one
`mkdir`. All 300 images are still on disk, under their original names, in
`dataset/_migrated_duplicates/`. Putting one back is a single `mv`.

---

## 4. ⚠️ Correction to `handover-phase-6a.md` §1.4

6a examined those same three folders and recorded them as **"inert"**, on the
grounds that `validate_student_id()` refuses a name with spaces and dots, so
`parse_dataset_folder()` returns None and training skips them.

**That was true when written and false by the time it was read.** A later
sprint added the pre-migration guard at
[train_model.py:271](../train_model.py#L271), which turned the same folders
from *ignored* into *fatal for the entire run*. The handover did not become
wrong; it aged, and nothing linked the new guard to the old finding. Treat any
"this data is harmless" claim in an earlier handover as needing re-verification
against current code.

---

## 5. ⚠️ New privacy consequence — read before the next erasure request

`dataset/_migrated_duplicates/` is a **second location holding face images**.
*Manage Students → Delete* removes `dataset/{student_id}/` and knows nothing
about it, so an otherwise complete erasure now leaves a copy behind.

`docs/data_privacy.md` §5 has been updated to a fourth step and the checklist
carries a new line. It is inside `dataset/`, so it is covered by the existing
gitignore rule and the two `git check-ignore` checks in `CLAUDE.md` still hold —
no new commit risk. The directory exists only to make the migration reversible;
**it can be deleted outright once you are satisfied**, and nothing reads from
it.

---

## 6. Verified state (all re-measured today, after the fix)

| Thing | Value |
|---|---|
| Test suite | **1240 passed, 50 skipped** — measured after the change. The pre-change pass count was **not** measured, so do not quote a delta; 16 of these tests are new. The skip count is unchanged from 6a's 50 and is the git-blob baseline |
| `ruff check` | clean on all changed files |
| `dataset/` | `12345` (0 images), `23-1-1-0559`, `23-1-1-0918`, `23-1-1-0920`, `test-111` (100 each), `_migrated_duplicates/` (3 folders, 100 each) |
| `trainer/trainer.yml` | **72,103,331 bytes** — ⚠️ 54,984,159 in 6a; four students now, not three. ≈18.0 MB/student, consistent with 6a's 18.3 |
| `trainer/labels.txt` | 4 identities — `23-1-1-0559`, `23-1-1-0918`, `23-1-1-0920`, `test-111` |
| Training job | `state: succeeded`, message *"Training completed for 4 student(s). Skipped (too few images, not recognisable): 12345"*. Wall time not timed precisely — it was still running at the 3 s poll and finished before the 6 s one |

**How the fix was verified** — not by the CLI alone. The `BackgroundJob` the
Retrain button starts was driven in-process (`from web.enrolment import
training_job; training_job.start(...)`, then polled `status()` exactly as
`/train_status` does) and reported `succeeded`. That is the same object, the
same thread target and the same status dict the browser receives.

---

## 7. Traps for whoever is next

- **`12345/` is empty and that is fine.** It is skipped, not fatal
  ([train_model.py:404](../train_model.py#L404) — one incomplete folder must
  not block the rest, FS-2). Training warns that `12345` will not be
  recognised. Do not "fix" it by adding a guard; that guard is this sprint's
  bug.
- **`test-111` is now in the trained model.** It is UAT residue from Phase 6a's
  enrolment test, not a real student. If you want it gone: delete
  `dataset/test-111/`, retrain, and mind the `.bak` generation
  (`docs/data_privacy.md` §5).
- **The `students` table is still empty** (6a §1.3, unchanged). All four trained
  identities will be *recognised* but refused at `save_attendance()` as "Not
  Enrolled" until rows exist. This is a data state, not a bug, and it is
  unrelated to this sprint.
- **`plan_renames()` returns four values now.** Any caller written against the
  old 3-tuple will unpack wrongly. `main()` is the only in-repo caller and was
  updated.
- **The `.venv` has a stale installed copy of this project in
  `site-packages/`** (`web/`, `config/`, …). Running a script from outside the
  project root imports *that* copy, not the working tree, and it fails with a
  pydantic `secret_key` error. Put the project root on `sys.path` first, or run
  from the root. This cost time and is not written down anywhere else.
- **The Bash tool in this environment has no coreutils** — no `ls`, `cat`,
  `sed`, `grep`. Use PowerShell or the file tools.

---

## 8. Open decisions (unchanged, still the user's)

Nothing new. `tasks/todo.md` §7 stands as it was. The one decision this sprint
raised — whether to delete the duplicate folders or move them aside — was put
to the user, who chose to **quarantine, not delete**, and to fix the script
rather than only the data. That is what was built.
