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

---

## L6 — A negative test runs the code you are proving broken. Give it nothing real to break

**2026-08-08, Phase 2 security work.**

L4 says a check whose job is to detect a problem must also be run against
broken input. I did that: I built `tests/test_route_security.py`, then
checked out `main` in a worktree and ran the suite there to prove the tests
actually caught SE-4 and SE-5 rather than passing vacuously. 48 of 74 failed,
exactly as they should.

One of them was:

```python
sign_in_as(client, "instructor")
client.post("/delete_student/23-1-1-0559")   # a real, enrolled student
```

On `main` that route checks only `'user' in session`, so it did not return
403. It **ran**, against the live MySQL database, and deleted the student
row. I noticed twenty minutes later, from an unrelated assertion in the
end-to-end script (`no student row created` — the count was 3, not 4).

**And I under-counted the damage on first inspection.** Having found the
student row, I restored it and moved on. The placeholder subject `CS401` had
gone too, and I only found that an hour later while checking the row counts I
was about to write into the handover. It went the same way, through a
different test: `test_deletes_reject_get` issues `GET /delete_subject/1` to
assert a 405, and on `main` that route *was* a GET route. So the test whose
whole purpose was "this URL must not do anything on a GET" did the thing on a
GET. **When a negative run turns out to have mutated shared state, audit
everything it touched — not just the thing that alerted you.** I had a
complete list available the whole time: the parametrised route table in the
test file.

**Two things stopped this being worse, and neither was a decision I made.**
The dataset folder survived because `main` builds the path as the *relative*
`os.path.join("dataset", ...)` and the worktree had no `dataset/` directory,
so `os.path.isdir` was False — the exact path-handling weakness this phase
existed to fix is what spared 100 face images. The row itself was restorable
because `trainer/labels.txt` still held the student's name, so it could be
reconstructed rather than retyped from memory.

**Why the reasoning failed.** I checked that the tests asserted *denials*, so
they needed no database — true on the fixed code, where the hook refuses
before the route body runs. That is precisely the property the negative run
removes. A test that cannot reach the database on the new code is a test that
sails straight into it on the old.

**How to apply:**

- **Never put a real identifier in a test URL.** Every identifier in
  `test_route_security.py` is now `SEC-TEST-NOBODY` / `SEC-TEST-NOONE`. Had
  it been that from the start, the negative run would have deleted a row
  matching nothing. Note that `/delete_subject/1` was still a real row —
  numeric IDs have no obviously-fake form, which is an argument for a scratch
  database rather than for careful naming.
- **Audit the full blast radius, not the part that alerted you.** Two rows
  were lost; I found one, declared it handled, and found the second by
  accident an hour later.
- **A negative test is an execution of the vulnerable code path.** Before
  running one, ask what it does if the guard is absent — that is the whole
  point of the exercise, so the answer is never "nothing".
- **A worktree is not isolation.** It isolates the *files*. The database,
  `.env` and every other external resource are shared, and the checkout runs
  with the same credentials as everything else.
- Point destructive negative tests at a scratch database, or accept that they
  will mutate the live one and seed a disposable row first.
