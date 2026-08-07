# Lessons

Patterns worth not repeating. Added after a correction or a near-miss.

---

## L1 — Measure artifacts by regenerating them, not by reading what's on disk

**2026-08-08, audit of the recognition layer.**

I reported the trained model as 878 MB, taken from `ls` on `trainer/trainer.yml`.
It was actually a **partial write truncated at 48%**. A complete model for that
configuration is 1.835 GB. Every scaling figure derived from the 878 MB reading
was understated by half, and I had to correct them after the retrain.

**Why it happened:** a file on disk was treated as a finished product. It was
the wreckage of a crashed process. The absence of its sibling `labels.txt`
was the tell, and I had already noticed it — but I read it as a *separate*
symptom rather than as evidence the same write had died.

**How to apply:** when a build artifact is the subject of an audit finding,
regenerate it before quoting numbers from it. If regeneration is too expensive
to do up front, label the figure as provisional. Treat a missing or
inconsistent sibling artifact (`labels.txt` beside `trainer.yml`, a lockfile
beside a manifest) as evidence about the artifact you *are* measuring, not
just as its own separate finding.

---

## L2 — A documented "optimization" is a hypothesis until it round-trips

**Same session.**

`docs/walkthrough.md` presented `neighbors=12` as a tuning improvement with
100% accuracy. It was the defect that took the system down, and it failed in
two independent ways at once:

1. the model could not be read back (`cv::FileStorage` assertion), and
2. even in memory it matched nobody, because raising `neighbors` shifted LBPH
   distances into 81–107 while `RECOGNITION_THRESHOLD` stayed at 58.0.

Failure (2) is the more instructive one: **a parameter change moved the scale
of a metric while a threshold tuned to the old scale was left untouched.**
Nothing errored. It silently classified every face as unknown.

**How to apply:** when changing a parameter that affects the *magnitude* of a
score, find every threshold compared against that score and re-derive it.
Grep for the threshold constant before merging. And validate model changes
with a full **write → read → predict** round trip — training in memory and
predicting in the same process hides all persistence failures, which is
exactly what the original evaluation scripts did.

---

## L3 — An evaluator that shares no code with production measures nothing

**Same session.**

Three defects, all from the same root:

- `test_accuracy.py` stored student IDs in `label_map` and then joined them
  onto `DATASET_DIR` as folder names. Nothing matched, so it scored **0 images**
  while still printing a clean summary. It had likely never produced a real
  number.
- `test_heldout_accuracy.py` augmented its training set ×4 and hardcoded
  `neighbors=12`, so it measured a configuration that production did not use.
- The LBPH parameters were written out longhand in three files, free to drift.

**How to apply:** evaluators must **import** the production configuration, not
restate it — `LBPH_PARAMS` now lives in `train_model.py` and both evaluators
import it. And a metric harness needs at least one assertion that fails when
it silently measures nothing: `assert total_images > 0`. A summary table full
of zeros should be an error, not a result.

---

## L4 — Run the commands you put in documentation

**2026-08-08, writing the handover and CLAUDE.md.**

I documented a safety check for the single most consequential rule in this
project — never commit biometric data:

```bash
git check-ignore -q dataset trainer && echo SAFE || echo STOP
```

It does not work. `-q` accepts only one pathname, so git exits non-zero with
`fatal: --quiet is only valid with a single pathname`. The command prints
`STOP` **even when both paths are correctly ignored**. I had written it into
two files before running it once.

**Why it matters more than a typo:** a safety check that cries wolf gets
ignored. The next agent sees `STOP` on a clean repository, concludes the check
is unreliable, and stops running it — leaving the real rule unguarded. A
broken check is worse than no check, because it looks like protection.

**How to apply:** any command written into documentation gets executed before
the file is saved — no exceptions for "obvious" one-liners. For a check whose
job is to detect a problem, also run a **negative test**: break the input
deliberately and confirm it actually fails. The replacement was validated both
ways:

```bash
[ "$(git check-ignore dataset trainer | wc -l)" -eq 2 ] && echo SAFE || echo STOP
# passes on a healthy repo; correctly reports STOP when a rule is missing
```

The same applies to reproduction steps, setup instructions, and any `bash`
block in a handover: if it was not run, mark it untested rather than implying
it works.

---

## L5 — "It imports" is not "it runs". Exercise a request, not a module

**2026-08-08, Phase 1 configuration migration.**

I replaced hardcoded credentials with `from config.settings import settings`
at the top of `app.py`. That file already had a route handler named
`settings()` roughly 1,350 lines further down, which rebound the name.

Everything I checked said it was fine:

- `import app` succeeded
- the app booted and logged the model load
- 27 unit tests passed
- `ruff check` was clean
- `py_compile` was clean

All five agreed because `app.secret_key = settings.secret_key` executes at
import, *before* the `def` runs. But `get_db_connection()` executes per
**request**, by which time `settings` was the view function — so every
database call in the application would have raised
`AttributeError: 'function' object has no attribute 'db_kwargs'`. Login,
students, reports, attendance: all of it.

**Why it slipped through.** Python rebinding a module-level name is legal, so
no tool complains. Ruff's F811 covers redefinition of an *unused* name; this
one was used, just earlier in the file. And my verification step was
`import app`, which is precisely the operation that cannot see the problem.

**How to apply:**

- **Verify at the layer the code actually runs at.** For a web app that means
  driving a request through `app.test_client()` and asserting on the status
  code, not importing the module. The fix was confirmed with
  `POST /login` → 302, `GET /students` → 200 against the real database.
- **After adding a module-level import to a large file, check the name is not
  redefined later** — `grep -n "^def <name>\|^<name> ="`. Long route files are
  where this bites, because the import and the collision are thousands of
  lines apart.
- A whole-file `ast` scan is cheap and catches the class rather than the
  instance: `tests/test_no_import_shadowing.py` does this for all eight
  modules without importing any of them, which matters here because importing
  `recognize_face` costs 9 s and importing `capture_dataset` opens a camera.

This is the third defect in this project that **looked like working code**
(after PE-0 and the `test_accuracy.py` zero-image bug). The pattern is
consistent: the check that would have caught it was one layer away from where
the failure lives.
