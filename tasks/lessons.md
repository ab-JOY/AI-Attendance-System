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

---

## L7 — When a refactor "should be" equivalent, prove it. A docstring is not a measurement

**2026-08-08, Phase 3 MA-4 extraction.**

I merged two divergent copies of the face-geometry gate into one profiled
implementation. The originals computed in truncated integers
(`x + int(width * 0.18)`, with `nose_x` truncated too); I wrote the shared
version in floats and put a confident note in the module docstring saying the
difference was "under one pixel on bands tens of pixels wide" and could only
matter for a face sitting exactly on a boundary.

That note was a **guess wearing the clothes of a measurement.** It was written
before anything had been run.

So I built a differential harness instead: the pre-refactor functions were
pulled out of the previous commit's blob with `ast` and exec'd in a clean
namespace — neither module can be imported, one loads a 55 MB model at import
and the other opens a camera — then both implementations were run over 40,000
randomised synthetic meshes.

The recognition gate matched on all 40,000. **The enrolment gate disagreed on
105.** My docstring had been directionally right and quantitatively unfounded,
and I would have shipped it as fact.

**The part that mattered was the second experiment.** Knowing there were 105
mismatches, "it is probably the truncation" is still a guess. So I transcribed
the *original* logic with only the `int()` calls removed and ran that against
the new module: **0 mismatches on all 40,000.** That is what turned "probably
truncation" into "truncation, and nothing else" — a claim about the mechanism,
not a correlation. The residual then had a real bound: worst case 0.865 px
from a band edge, median 0.339 px.

**How to apply:**

- **A behaviour-preserving refactor is a testable claim.** Test it. The old
  code is one `git show` away, and `ast` will lift a function out of a module
  that cannot be imported — which is exactly the case where the refactor is
  riskiest and the temptation to skip verification is strongest.
- **Randomised differential testing beats hand-picked cases** for this. Every
  case I would have written by hand was a face near the middle of a band; all
  105 disagreements lived at the edges.
- **A discrepancy is not explained until an experiment isolates its
  mechanism.** "It is probably X" and "removing X makes the discrepancy
  disappear entirely" are different epistemic objects, and only the second one
  belongs in a commit message or a thesis.
- **Do not let prose do a measurement's job.** If a docstring states a
  quantity, either it came from a run or it is marked as an estimate. This is
  L1 again — *measure artifacts by regenerating them* — but for behaviour
  rather than for files.

---

## L8 — Verify an inherited warning before you plan around it

**Same session.**

The Phase 2 handover flagged MA-4 as "the first Phase 3 task that can move
that figure", meaning the 60/60 held-out accuracy, and told the next agent to
record the number before and after. I planned the sprint around that warning.

It is wrong. `eval_heldout_accuracy.py` imports `settings`,
`preprocess_for_lbph`, `LBPH_PARAMS` and `parse_dataset_folder` — and never
the geometry gate. It scores stored 200×200 crops; the gate decides which
frames are *saved during capture* and *recognised at runtime*. The evaluator
exercises neither, so no change to the gate can move it. The run afterwards
confirmed it: 60/60, avg 34.95, unchanged.

The same handover reported the route table as "4 public, 9 authenticated, 22
admin-only". Measured today, with `app.py` untouched this sprint: **3 public,
9 authenticated, 23 admin-only**.

**Why this matters more than two small errors.** A handover is written to be
trusted by someone with no memory of the session, and this project's CLAUDE.md
makes reading it mandatory *precisely* so findings are not re-derived. That
trust is the point — and it means a wrong line propagates unchallenged into
the next sprint's plan, and from there into the manuscript.

**How to apply:**

- **Inherited claims are evidence, not axioms.** Read the handover first, as
  instructed — then, before a warning changes what you build, spend the two
  minutes to check the thing it describes. Here it was one `grep` of the
  evaluator's imports.
- **Warnings that are cheap to check and expensive to be wrong about go
  first.** "This task can change your headline accuracy number" is exactly
  that shape.
- **When you write a handover, mark which numbers you re-measured** and which
  you carried forward from an earlier document. The next reader cannot
  otherwise tell a fresh measurement from a five-sprint-old one.

---

## L9 — A benchmark's assumptions are measurements too

**2026-08-09, Phase 3 PE-6.**

I fixed the camera reader and benchmarked it properly: the old class lifted out
of git with `ast`, the new one beside it, both driven against a fake capture.
The producer numbers were solid and remain so — a failing camera went from
**94.7% of one core** to 0.0%, which is PE-6's "spins a core at 100%" confirmed
literally.

Then I measured the second half of the finding, "re-copies frames the consumer
never reads", by polling the reader at an assumed consumer cost. I ran it at
45 ms and 10 ms per frame, reported 30% and 81% of frames re-processed, and
wrote in the commit message that the 45 ms row was "the realistic one for this
deployment."

**I had never measured the consumer.** When I did, two commits later, it costs
**48.7 ms with no face in shot and 147.3 ms with one**, against a camera capped
at 33.3 ms. The recognition loop is *always slower than the camera* on this
machine, so it never outran it under the old code either, and that half of the
fix saves nothing measurable here. The number I published was not wrong
arithmetic — it was arithmetic about a machine that does not exist.

**Why this is L7 again and still worth its own entry.** L7 was about a
*docstring* asserting a quantity nobody had run. This is the same failure one
level up: the run happened, the harness was careful, the old code was lifted
from git rather than retyped — and the *parameter* fed to it was a guess
wearing the clothes of a measurement. Rigour in the measuring apparatus does
not launder an invented input, and a benchmark is more dangerous than a
docstring because it comes with a table.

**How to apply:**

- **Before benchmarking a component, measure its collaborators.** If a
  harness takes "how fast is the consumer?" or "how big is the input?" as a
  parameter, that parameter is a finding in its own right and needs its own
  run. Deriving it from the audit, from a comment, or from what sounds
  plausible is the same mistake as never running anything.
- **A component benchmark bounds a component, not a system.** "The reader can
  now do X" and "the system now does X" are different claims. Say which one
  the number supports.
- **Correct it where it was published, not only where you noticed.** The wrong
  figure was in a commit message, which is immutable; the correction therefore
  belongs in `docs/benchmarks.md` §3 and in the handover, both of which say so
  explicitly and name the commit. A benchmarks document that quietly disagrees
  with a commit is worse than either alone.
- The half of the fix that *was* real should still be stated plainly rather
  than dropped in embarrassment. The producer brake matters, and the
  frame-ready signal still removed a 20 ms poll-sleep and made `stop()`
  immediate. Overstating a result and then hiding it are the same failure of
  reporting.
