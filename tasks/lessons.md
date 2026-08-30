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

## L11 — A schema constraint is not a guard. Test the migration against the data you are afraid of

**2026-08-11, Phase 4, migration 004.**

Migration 004 moves `attendance` off a free-text `subject_code` onto a
`subject_id` foreign key. The backfill can only resolve codes that are
unambiguous, so I wrote it to leave the rest NULL and let the schema stop the
run, and I wrote in the commit message that `MODIFY subject_id INT NOT NULL`
would "fail loudly rather than deleting or guessing".

It does not. Two drafts, two wrong assumptions, both found by running the
migration against a scratch database holding one deliberately ambiguous row:

1. **`MODIFY ... NOT NULL` does not fail on a NULL here.** This server's
   `sql_mode` is `NO_ZERO_IN_DATE,NO_ZERO_DATE,NO_ENGINE_SUBSTITUTION` — no
   `STRICT_TRANS_TABLES` — so it silently converted NULL to **0**.
2. **Adding the foreign key first did not catch it either.** With
   `foreign_key_checks = 1`, the `ALTER TABLE` rebuild did not re-validate the
   constraint. The run reported **success** and left an attendance row
   pointing at `subject_id = 0`, a subject that does not exist.

And by the time anything noticed, `DROP COLUMN subject_code` had already
committed. DDL cannot be rolled back, so the column the backfill would have
needed was gone. A "successful" migration had produced a corrupt referential
state and destroyed the evidence needed to repair it.

The fix was an explicit gate that does not depend on engine behaviour, placed
before anything destructive:

```sql
SELECT CASE WHEN (SELECT COUNT(*) FROM attendance WHERE subject_id IS NULL) > 0
       THEN (SELECT 1 FROM (SELECT 1 UNION ALL SELECT 2) unmapped_rows_exist)
       ELSE 0 END;
```

`CASE` evaluates lazily, so a clean table returns 0 and a dirty one raises
error 1242. Verified both ways before being relied on.

**How to apply:**

- **Never let a constraint be the guard.** `NOT NULL`, a foreign key and a
  `CHECK` describe the state you want to end in; they are not a decision
  procedure for whether the data is fit to migrate. Write the check as a
  statement whose only job is to stop the run, and put it first.
- **Order destructive statements last, always.** DDL auto-commits, so a
  migration is not a transaction however much it looks like one. Everything
  before the first `DROP` is the part you can still recover from — make that
  region as large as possible.
- **Check `sql_mode` before trusting any conversion to fail.** Non-strict mode
  turns whole categories of error into silent coercion: NULL to 0, an
  unparseable time to midnight, an over-long string to a truncated one. This
  project already avoided that once, in the same set of migrations, for
  `subjects.time_in` — and then walked straight into it for `subject_id`.
- **Migrate a scratch database holding the data you are afraid of**, not an
  empty one. Applying 004 to an empty table passed every time and proved
  nothing; one ambiguous row exposed two independent wrong assumptions in
  under a minute.
- This is [L4](#l4) at the schema layer — a check whose job is to detect a
  problem must be run against the problem — and [L1](#l1)'s point that a
  successful-looking artifact can be wreckage.

---

## L10 — Two thresholds on one metric scale will find the gap between them, and it will be silent

**2026-08-11, Phase 4, reported from a live run by the user.**

A student stood in front of the camera and the overlay read
`Verifying 20/20 100% (56.1)` indefinitely. Twenty of twenty frames agreed,
unanimously, on the right person. Nothing was recorded. Nothing was logged.
No error was raised anywhere.

Two thresholds, both on the LBPH distance scale, set in two places:

- `RECOGNITION_THRESHOLD = 58.0` — a frame's prediction is good enough to vote.
- `TrackConfig.confirmation_confidence = 52.0` — the window's *average* must
  beat this to lock an identity.

Any student whose live distance settled in `(52.0, 58.0]` was recognised on
every single frame and could never be marked present. The held-out evaluator
could not see it: it scores stored crops at an average of 34.95, nowhere near
the band. Only a real face in a real room lands there.

**This is the third time this project has produced the same shape of bug.**
L2 was `neighbors=12` moving the distance scale while the threshold stayed
put. SE-12 was liveness thresholds in frame-normalised units against a gate
working in face widths. This is the same failure with neither a unit change
nor a parameter change — just two numbers, written down separately, that were
never required to agree.

**What made it expensive was the silence, not the gap.** A yellow box and a
plausible-looking status is indistinguishable from "still working on it". The
operator has no way to tell a slow success from a permanent failure, so the
report that finally arrived was "attendance is stuck", not "attendance is
broken" — and it arrived from a user, not from a test or a log.

**How to apply:**

- **Two thresholds compared against the same metric must be derived from one
  another, not restated.** `confirmation_confidence` is now
  `RECOGNITION_THRESHOLD`, and a test fails if a gap reopens. If a future
  calibration wants them apart, that is a decision with a DET curve attached,
  not a literal someone typed.
- **A guard that can refuse forever must say which condition it is refusing
  on.** `can_confirm()` returned a bare `False` for five different reasons.
  It is now expressed through `confirmation_blocker()`, which names the unmet
  condition — the same relationship `is_valid_face_candidate()` has with
  `rejection_reason()` in `vision/validation.py`, and for the same reason.
  That pattern already existed in this codebase; it just had not been applied
  here.
- **A state that persists indefinitely is an error state, and should be logged
  once and shown to the operator**, however normal each individual frame
  looks. Throttle it on transition, not on time, so it appears exactly once
  per stuck track.
- **Before changing a threshold, measure what the change costs.** The
  impostor set answered it in twenty minutes: 750 unenrolled faces score 62.2
  at the closest, so the 52.0–58.0 band was protecting against nothing that
  exists. That is a defensible number in a thesis; "it seemed too strict" is
  not.
- **An evaluator that never reaches the failing region cannot rule the failure
  out.** 60/60 at avg 34.95 was true, reproducible, and completely blind to
  this. When a metric harness and a live run disagree, the harness is the one
  operating out of distribution — which is the §3 leakage caveat turning into
  a concrete outage rather than a footnote.

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

---

## L12 — A test that greps the source will match the comment that explains the code

**2026-08-15, Phase 4b, FS-10.**

Two of the tests I wrote to protect the correction route asserted on its
*source text*:

```python
body = source[source.index("def correct_attendance("):end]
assert "FOR UPDATE" in body          # the row is locked before it is changed
assert "TIME_IN =" not in body       # no arrival time is invented
```

Both were **vacuous**, and I only found out because I mutated the route to
check. The route's own docstring and comments explain *why* it locks the row
and *why* it leaves `time_in` alone — so the strings "FOR UPDATE" and
"time_in" appear in the function whether or not the SQL does. Deleting
`FOR UPDATE` from the actual statement left the test green. `grep -c "FOR
UPDATE" app.py` returned **2**: one SQL, one comment.

The fix was to stop reading the function as text and start reading what it
executes — pull the string constants out of the `cursor.execute()` calls with
`ast`, and assert on those:

```python
reads = [sql for sql in correction_route_sql()
         if sql.lstrip().startswith("SELECT") and "FROM ATTENDANCE" in sql]
assert any("FOR UPDATE" in sql for sql in reads)
```

Re-mutated afterwards; both now fail as they should.

**Why this is worth its own entry when [L4](#l4) already exists.** L4 says a
check whose job is to detect a problem must be run against the problem. I
believed I had internalised that — the same session's integration tests were
*all* mutation-tested before being trusted, which is how these two were caught.
The gap was that I applied the discipline to the tests I thought of as
"real tests" and not to the two source-level assertions, which felt like
assertions about the code rather than tests of it. **The rigour has to cover
the checks you wrote casually, and those are exactly the ones you will not
think to verify.**

**How to apply:**

- **A source-level assertion must parse, not grep.** `ast` is already used for
  this in `tests/test_no_import_shadowing.py`, `tests/test_db_access.py` and
  `tests/test_migrations.py`, precisely so reformatting cannot change a
  result. Text search additionally cannot tell code from the prose about it,
  and this codebase comments heavily *by design* — the better the comments,
  the more likely a grep-based test passes for the wrong reason.
- **The more carefully a decision is documented, the more certainly a text
  search for it will succeed.** That is an inverse relationship between
  documentation quality and test validity, and it will bite hardest in exactly
  the files worth protecting.
- **Mutate every assertion, including the ones that felt too obvious to
  check.** Six of eight caught their defect first time; the two that did not
  were the two I would have skipped if I had been choosing.
- This is the fifth defect in this project that **looked like working code**,
  and the first one that was in a *test*. A green test asserting nothing is
  the same object as `test_accuracy.py` scoring 0 images and printing a clean
  summary ([L3](#l3)) — a measurement apparatus reporting success while
  measuring nothing.

---

## L16 — A test document is written for the tester, not about the work

**2026-08-16, corrected by the user.**

I revised `docs/uat_manual.md` for the UAT and filled it with what had just
been fixed: a banner listing four defects and the cases they touched, cases
annotated *"never passed before 2026-08-16"*, a priority note explaining which
routes had been broken and how invisibly, and a table of limitations that had
stopped being true and why.

The user's correction was one line: **"UAT does not need bug explanation,
testers do not need to be informed what was fixed before the testing."**

**Why it is wrong, not just verbose.** A UAT manual asks somebody to observe
behaviour and report what they see. Telling them what was recently broken
tells them what to expect — which is the one thing an acceptance test must not
supply. "This never worked before today" primes a tester to accept a marginal
result as success, or to attribute an unrelated failure to the thing they were
told about. The history is also *unverifiable to them*: they cannot check it,
so it is asking for trust in a document whose job is to collect evidence.

The same content was already in the right places — `tasks/review-phase-5.md`,
the handover, `todo.md` §8. I duplicated it into the one document whose
audience does not want it.

**How to apply:**

- **Write each document for who reads it, and name that person before
  starting.** The tester wants *what to do* and *what should happen*. The next
  agent wants *what changed and why*. The manuscript wants *what was measured*.
  The same fact belongs in different words in each, or in only one of them.
- **In a test case, `Expected` is a description of correct behaviour, full
  stop.** No "it used to", no "⚠️ this was broken", no finding IDs. If a case
  needs the tester to be careful, say what to be careful about.
- **Operational notes are not history.** "Sign in fresh before you start" is
  useful; "because sessions created before this build lack a key" is not, to
  this reader.
- **Length is the symptom, not the disease.** Trimming words would have left
  the same priming in place. The cut is *by audience*, and it removes whole
  categories rather than shortening sentences.

---

## L15 — A warning nobody verified, repeated by four documents, was a person forgetting to press start

**2026-08-16, corrected by the user.**

`handover-phase-4.md` recorded that *"the database server stops on its own"*,
found down mid-sprint with **nothing written to `mysql_error.log`**. Phase 4b
repeated it. Phase 5 called it the third occurrence. I hit it during the final
verification sweep, confirmed the log ended at `ready for connections` with no
shutdown recorded, and wrote it up as the **fourth** — adding, with some
confidence, that *"four occurrences across four sessions is a pattern, not bad
luck"*, and that it belonged in the tester's manual.

The user's correction: **XAMPP's MySQL does not start automatically after a
Windows restart, and they start it by hand.** Every occurrence was a session
that began after a reboot. The missing shutdown line — the detail each of us
treated as the *evidence of an anomaly* — is exactly what a clean OS restart
looks like. There was never anything to investigate.

**Why four agents got this wrong in the same way.** Each of us read the
previous handover, found a warning already framed as unexplained, and checked
only that the *symptom* matched. It did, every time. Confirming a symptom
against a prior description is not the same as testing the explanation
attached to it — and the more documents carry a claim, the more it reads as
established rather than as one unverified observation copied forward. The
escalation is the tell: "found down once" became "stops on its own twice per
session" became "a pattern, not bad luck", with no new evidence at any step.

**And the check was free.** The machine has an operator. One sentence — *"does
MySQL start on its own here, or do you start it?"* — would have closed it four
sprints ago. I ran `netstat`, read the error log, restarted the server and
re-verified the row counts. Every one of those investigated the symptom, and
none of them could have found the answer, because the answer was not in the
system.

**How to apply:**

- **This is [L8](#l8) with the failure mode inverted.** L8 says an inherited
  warning must be verified before you plan around it, and I applied that
  literally — I verified the *symptom*. Verify the **explanation**, which is
  the part that was never measured. A reproducible symptom does not
  authenticate the story attached to it.
- **When a system has a human operator, they are a source of evidence.**
  Cheaper than a log dive, and the only source for anything outside the
  system's own boundary — reboots, manual steps, what was running at the time.
  Ask before instrumenting.
- **Watch for claims that grow between documents.** A finding that gains
  confidence without gaining evidence is a finding nobody re-derived. Grep the
  handovers for the strongest version of a claim and trace it back to its first
  statement; if the first statement was a single observation, the strong
  version is unearned.
- **Absence of a log line is not evidence of anything on its own.** It has at
  least as many innocent explanations as guilty ones, and here the innocent one
  was the boring one.
- Corrected in `handover-phase-4.md`, `-5.md`, `-5b.md`, `todo.md` §8 and
  `docs/uat_manual.md` §0.2 — **all five**, because the whole point is that a
  wrong line propagates unchallenged into the next sprint's plan.

---

## L14 — A review's prescribed fix is a hypothesis about the schema too

**2026-08-16, clearing the Phase 5 defect register.**

The end-to-end review found R4: `insert_absences()` writes
`time_in = NOW()` for every Absent row, so the register shows an arrival time,
identical across the whole cohort, for students who never arrived. The finding
was right, the evidence was a SQL literal read directly, and the prescribed fix
was one word:

> **Fix.** Write `NULL` for `time_in` on an absence.
> `services/reporting.py::_cell()` already renders `None` as an empty cell, and
> `attendance_live` already guards on it, so the display side needs nothing.

Every clause of that is true and it would not have worked. `attendance.time_in`
is `TIME NOT NULL` (migration 001), and this server's `sql_mode` is
`NO_ZERO_IN_DATE,NO_ZERO_DATE,NO_ENGINE_SUBSTITUTION` — **no
`STRICT_TRANS_TABLES`**. Inserting NULL there is not refused; it is silently
converted to `00:00:00`. The fix would have swapped a fabricated end-of-class
time for a fabricated midnight, on an INSERT reporting success, and the
register would have looked more plausible than before.

**Two checks the review had done, and one it had not.** It verified the
*writer* (the SQL literal) and the *readers* (`_cell()`, `attendance_live`).
It did not verify the *column*, and the column is the only participant whose
behaviour depends on server configuration rather than on this repository.

**This is [L11](#l11) arriving from a new direction, which is why it is worth
its own entry.** L11 was about a migration trusting `NOT NULL` to stop a bad
value. This is the mirror image: code trusting that a value it writes will be
stored as written. Same server property, same silence, opposite side of the
statement. L11's advice — "check `sql_mode` before trusting any conversion to
fail" — did not obviously cover "check the column before writing a value you
have not written before", and it does now.

**How to apply:**

- **A finding names a defect; its suggested fix is untested code.** Read the
  review for the *evidence*, then re-derive the remedy. A review that reads
  files cannot see the schema, and on this project the schema is where the
  silence lives.
- **Before writing a value a column has never held — NULL above all — check
  `SHOW COLUMNS` for that column.** Two seconds, and on a non-strict server it
  is the difference between a fix and a better-disguised version of the bug.
- **Assert against the column, not the Python value.** `row["time_in"] is None`
  reads a coerced `00:00:00` as a `timedelta` and passes for the wrong reason;
  `SELECT time_in IS NULL` cannot. The test written for this asserts both the
  value in SQL *and* that `SHOW COLUMNS` reports `Null: YES`, because those are
  two different claims and only the second survives a database restored from an
  older dump.
- The same reasoning found nothing wrong with the review's other thirteen
  findings, all of which reproduced exactly as described. **The point is not
  that reviews are unreliable — it is that "fix" and "finding" have different
  evidentiary standing, and only one of them was measured.**

---

## L13 — `git checkout --` is not an undo. Restore from the copy you made

**2026-08-16, Phase 5, mutation-testing the template bans.**

I mutation-test every new assertion before trusting it (L4, L12), and the
pattern I use is: copy the file aside, break it, run the test, restore the
copy. For three of four mutations that is exactly what I did. For the fourth I
typed:

```bash
git checkout -- templates/subjects.html
```

It worked, in the sense that the mutation was gone. It also silently discarded
**every Phase 5 change to that file** — the `base.html` conversion and the
US-5 `data-confirm` fix — because those changes were not committed yet. The
working tree went back to the last commit, which is what the command means and
not what I wanted.

**How it was caught, and how it nearly was not.** The very next mutation run
reported a failure in `subjects.html` that had nothing to do with the mutation
I had just applied. Had I run the mutations in a different order, or stopped
after the third, the file would have gone into the commit reverted — and the
commit message would have claimed a fix that the diff did not contain. The
tests are what caught it: `test_every_page_extends_the_layout[subjects.html]`
failed, which is the whole reason to write bans that sweep every file rather
than the file you are thinking about.

**How to apply:**

- **Undo a deliberate mutation with the copy you made, never with git.**
  `cp file /tmp/x.bak` … `cp /tmp/x.bak file` is symmetric and knows nothing
  about what is committed. `git checkout --` restores *HEAD*, and during a
  sprint HEAD is by definition missing the work in progress.
- **`git stash` has the same trap** and adds a second one: it moves every
  other uncommitted file too.
- **Prefer a sweep to a spot check.** The ban that caught this was
  parametrised over every template. A ban naming the file under test would
  have passed on a reverted file, because a reverted file has no mutation in
  it.
- This is [L6](#l6) with the blast radius pointed at the working tree instead
  of the database: a destructive step taken to *prove* something, whose damage
  is not where you are looking.

---

## L17 — "Safe" names a context. `tojson` in a double-quoted attribute is not safe

**2026-08-21, UAT: "camera is not opening on enrollment".**

`templates/enrol.html` carried:

```html
data-record="{{ record | tojson }}"
```

`tojson` escapes `<`, `>`, `&` and `'`. It does **not** escape `"`, because it
is built for a script element or a *single*-quoted attribute. In a
double-quoted one the first `"` of the JSON closes the attribute, so the
browser handed `getAttribute("data-record")` the single character `{`,
`JSON.parse` threw at `enrol.js` line 34, and the whole IIFE died — before
`openCamera(null)` on its last line.

The page then sat on every placeholder the server had rendered: "Preparing the
camera…", "Waiting for camera permission…", "0 of 100 images". No error
anywhere, because the failure was in the browser and the log is on the server.

**Three things made this survive to a UAT.**

1. **It was mode-dependent.** Recapture passes `record = {}` — no quote, so the
   page worked. Only Add Student passes four keys. Whoever last tested
   enrolment could easily have tested the half that works.
2. **The comment above it asserted the opposite:** *"`tojson` was already
   guarding the two that carry a student's name and record; an attribute value
   needs no guarding at all."* That is [L9](#l9) — prose describing a safety
   property the code does not have, sitting directly above the code that does
   not have it.
3. **This is [US-5](todo.md) one context over, and US-5 has a parsed test.**
   That test bans inline `on*` handlers. Nothing checked whether a value put
   in an attribute *survives being read back out*.

**How to apply:**

- **An escaping filter's contract names a context. Find out which one you are
  in before trusting it.** `tojson` → script element or single-quoted
  attribute. The failure is silent in exactly the case the filter was not
  written for.
- **Test the round trip, not the escaping.** Render with a hostile value,
  parse with `html.parser`, and assert you get back what you put in. That is
  one assertion and it covers every escaping rule at once, including the ones
  nobody has read.
- **Render through the application's own environment.** jinja2 ships one
  `tojson` and Flask replaces it with another; they already differ observably
  (Flask sorts keys). A test driving jinja2's would have been measuring a
  filter production does not use, on the exact question of what production's
  filter escapes — [L3](#l3), inside the test written to close this.
- **A module-scope `JSON.parse` takes the whole file down.** `enrol.js` guards
  `if (!page) return;` for a missing element and then throws two lines later on
  a malformed one. Anything parsed at IIFE scope needs the same care as the
  element lookup above it.

---

## L18 — Where a guard runs decides more than what it checks

**Same session. The UAT's second report: "identity verified correctly but on
the liveness check the same verified student keeps getting an unknown
verdict."**

`RECOGNITION_PROFILE` refuses a face whose nose sits more than 0.28 × face
width from the eye centre. The comment beside it is right about *why*: "an
attendance mark is a decision and a decision wants a frontal face."

But the check lived inside `is_valid_face_candidate()`, and
`_stream_frames()` calls that **before `tracker.assign()`**, with a bare
`continue` on failure. So a check whose stated job was *"is this face
recognisable?"* was actually answering *"does this face exist?"* — and a
frame that does not exist never refreshes its track's `last_seen`, so
`expire_old_tracks()` deleted the track 1.5 s later.

Meanwhile the liveness challenge was telling the student **"Turn LEFT"**.

So the system asked for a movement and deleted anyone who made it. They turned
back to find themselves at "Verifying 0/20", re-confirmed, drew a fresh random
challenge, and repeated — indefinitely. Nothing was logged by any of it.

**The measurements that mattered, and the ones that wasted time.** Three
plausible causes were tested first and all three were wrong: LBPH matches
turned faces fine (held-out, stratified by enrolment stage: LEFT 9/9 and
RIGHT 9/9 correct at threshold 58, median distance 34 — the same as frontal);
the demanded turn is well inside the training set (enrolment's LEFT/RIGHT sit
at 0.146w / 0.135w against a 0.10w requirement); the duplicate
`dataset/{id}_{Name}` folders are inert. **The bug was not in any of the parts
— it was in where one of them was called from.**

**How to apply:**

- **A gate placed before a state machine's update is part of the state
  machine.** Ask what a rejected input skips, not only what it fails. Here it
  skipped four things — the box, the challenge, the mismatch counter, and the
  liveness timer — and only the fourth was fatal.
- **When two subsystems constrain the same physical quantity, write down the
  band between them.** Liveness demanded ≥ 0.10 face-widths of turn; the gate
  refused past ≈ 0.32 in the same units. A real window, unmarked, uncommunicated
  to the student, and with the *enrolment* instruction ("turn **slightly**")
  already reaching 80% of the ceiling. This is [L10](#l10) — two thresholds on
  one metric scale — with the gap in the other direction.
- **Split the question rather than loosening the threshold.** Frontality moved
  from "may I see you" to "may I decide about you". The security property is
  unchanged — an attendance mark still needs a frontal face — and the failure
  is now unrepresentable rather than merely less likely.

---

## L19 — "It does not agree" has two opposites, and they are not the same evidence

**Same defect, second cause, and the one that would have kept biting after the
gate was fixed.**

```python
candidate_agrees = candidate_id is not None and candidate_id == locked_id

if not candidate_agrees:
    state.mismatch_frames += 1
```

One branch, two observations:

- **a contradiction** — LBPH read a *different enrolled student* on a locked
  track. Evidence of a track switch, which is precisely what the counter
  exists to stop.
- **no evidence at all** — LBPH read nothing usable: a crop the quality gate
  refused, a face blurred by the head movement the challenge had just asked
  for, a distance over threshold.

Both incremented the same counter. At 7–15 fps, **two frames (0.13 s) redrew
the challenge and five (0.33–0.7 s) destroyed the identity** — on exactly the
frames the challenge itself produces. A student following the instruction
correctly was the input most likely to trip it.

**The tell was in the variable name.** `candidate_agrees` folds a three-valued
answer — *this student*, *another student*, *no reading* — into a boolean, and
the two `False` cases are opposite kinds of evidence. Absence of evidence had
been silently promoted to evidence of absence.

**How to apply:**

- **Before writing `if not x_agrees`, count how many ways it can disagree.** If
  one of them is "I could not tell", it is not a disagreement and must not be
  counted as one.
- **A None-vs-mismatch conflation is invisible in a test that only supplies
  one of them.** The suite had cases for a track switch and none for an
  unreadable frame, so the branch was covered and the distinction was not.
- **Guard the relaxation with the case it was conflated with.** The tests that
  matter here come in pairs: the unreadable frame must survive *and* the
  different student must still be fatal. Half of that pair is a security
  regression wearing a bug fix's clothes.

---

## L20 — A missing stylesheet is a silent feature failure, not a cosmetic one

**2026-08-21, second UAT round: "capture is too slow, camera view is
overflowing the screen and capture direction is ambiguous."**

Three reports, and two of them were one cause: **not a single class on the
enrolment capture page had a CSS rule.** Ten classes, zero rules.

`enrol.js` was already doing everything the page needed. None of it rendered:

| the JS did this | with no CSS it |
|---|---|
| `drawBox()` painted the face rectangle onto `.capture-overlay` | drew onto a `<canvas>` **sibling** of the video which, without `position:absolute`, stacked underneath as its own block. The box had never appeared on the picture, once |
| `progressFill.style.width = "21%"` | set a width on a `<div>` with no height and no background |
| marked each stage `data-state="done" / "current" / "todo"` | all three states looked identical |
| assumed a mirrored preview — `drawBox()` says it mirrors coordinates "to match the preview, which is flipped" | nothing flipped it. Turning left made the picture appear to turn right, which is most of "capture direction is ambiguous" |
| asked getUserMedia for 1920x1080 | `<video>` laid out at its intrinsic size, so a 1920px video went into a ~1500px column and pushed the page off the viewport |

**Nothing raised.** Every one of those behaviours was *correct* and *invisible*,
which is why 1,205 tests and a clean `ruff` were green through all of it. The
suite checks structure, endpoints, escaping and access — a missing stylesheet
is none of those.

**The tell was in the template.** `enrol.html` carried inline `style=`
attributes on the camera picker (`style="margin-top:16px; padding:16px"`).
Those are not a shortcut; they are the symptom. Somebody needed one element to
look deliberate and the only place left to say so was the tag.

**How to apply:**

- **Treat "a class with no rule" as a defect class, not untidiness.** A class
  named in markup and absent from the stylesheet costs one parse to detect,
  and it is the cheapest signal for a whole page that shipped unstyled.
- **When JS drives an appearance, the CSS is part of the feature.** A progress
  bar, a state badge and an overlay canvas are not decoration — they are the
  entire output of the code that computes them. US-8 moved the JS out of the
  templates; the stylesheet for the page it moved out of never arrived.
- **Inline `style=` in a codebase that has a stylesheet is a smell worth
  chasing to its cause**, which is usually that the cause is much larger than
  the attribute.
- **A `<canvas>` positioned over a `<video>` needs the pair pinned by value,
  not by existence.** A test asserting `.capture-overlay` merely *has* a rule
  passes on `{color:red}`. Assert `position:absolute` and the containing
  block's `position:relative`.

---

## L21 — A grep over CSS matches the comment that documents the CSS

**Same session, caught by a mutation run rather than by review.**

The test written to close [L20](#l20) scanned `style.css` with
`re.findall(r"\.([A-Za-z0-9_-]+)")` and asserted every class used in the markup
appeared. Deleting the entire `.capture-instruction` rule left it **green**.

The stylesheet documents itself, and the comment introducing the capture page
names `.capture-overlay`, `.capture-stage` and `#preview` in prose. The scan
found a "rule" for anything merely *discussed* there.

This is [L12](#l12) exactly — *"a test that greps the source will match the
comment that explains the code"* — occurring **inside the test written to
prevent a different silent failure**, in a file whose own docstring opens with
*"Everything here parses the HTML. Nothing greps it."* The rule was known,
written down, and stated at the top of the very module.

**Why it was not caught by reading it.** The test passed, and it passed against
the real stylesheet for the right reason. Only removing the thing it was
supposed to protect showed that it would also pass for the wrong one.

**How to apply:**

- **The reason to mutation-test is not to confirm the test works. It is to find
  out what else makes it pass.** A test that goes green on the fixed code tells
  you nothing about which of the many differences it is reading.
- **Strip comments before scanning any format you cannot parse.** CSS has no
  standard-library parser, so `re.sub(r"/\*.*?\*/", " ", ...)` first is the
  nearest honest equivalent to L12's "parse, don't grep". Strip declaration
  blocks too — `content:"done"` and font stacks carry dots.
- **A lesson written down is not a lesson applied.** L12 has existed since
  Phase 5 and is quoted in this file's own module docstring; it still happened.
  Assume the failure mode applies to the code you are writing *now*, including
  the code written to prevent it.

---

## L22 — A per-item cost that should have been a per-stage cost

**Same session, the "capture is too slow" half.**

`EnrolmentSession._save()` called `_reset_hold()`. So a subject had to re-earn
`hold_frames` — six uploaded frames — for **every one of the 15 images in a
stage**, while standing perfectly still in a pose they had never left.

Measured: 6 frames at the 200 ms upload tick is 1.2 s per image, and 85 of the
100 images sit in six-frame stages. **102 of the 114 s floor**, on a server
spending **15 ms** judging a frame. The whole thing was waiting on a timer that
existed to pace something else.

Two compounding errors:

1. **The hold answers a question about entering a stage** — "has this person
   settled into the pose?" — and was being asked once per *image*.
2. **The 200 ms tick was justified by a figure carried from the wrong
   subsystem.** Its comment read "the server spends roughly 50-150 ms on a
   frame". That is the *recognition* loop, which runs LBPH on every tracked
   face. Enrolment does MediaPipe plus one alignment and no matching at all:
   15 ms. Nobody had measured the path the constant was pacing — [L9](#l9),
   a benchmark's assumptions travelling further than the benchmark.

Deleting one line and re-measuring took the floor from **114 s to 42 s**.

**How to apply:**

- **When something feels slow, measure the floor before choosing a lever.**
  Three levers looked equally plausible here (tick rate, delay, image count);
  the measurement showed one of them was 90% of the cost and the other two
  barely mattered until it was gone.
- **Ask what question a gate is answering, and how often that question
  changes.** A cost paid per item for a property that only changes per group is
  the shape to look for.
- **A performance constant needs the measurement for its own path.** Copying
  one from a neighbouring subsystem is how a 15 ms operation ends up on a
  200 ms budget.
- **Relaxing a guard needs its opposite tested in the same commit.** The hold
  still resets on *any* gate failure, so a broken pose must be settled again —
  and that has its own test, because without it a subject could drift between
  images and collect a worse dataset at full speed.

---

## L23 — A threshold in the wrong units is a bug three sprints running

**2026-08-21, round 3 of the UAT: "it can't detect looking forward face."**

`get_head_pose()` compared yaw and pitch against thresholds expressed as
fractions of the **frame**. `pitch = nose.y - eye_centre.y` grows with the
face, so STRAIGHT - which required pitch between `PITCH_UP` (0.095) and
`PITCH_DOWN` (0.110) - was **a 0.015-wide window**, and that window is not a
statement about a head. It is a statement about standing at one distance.

Measured on the 300 real enrolment images, composited at a range of sizes,
asking what a face **looking directly at the camera** was called:

| face box | verdict |
|---:|---|
| 400 px | **UP** (15/15) |
| 500 px | STRAIGHT 13, UP 11 |
| 560 px | STRAIGHT 15, DOWN 8 |
| 600 px | **DOWN** 22/24 |
| 800–1050 px | **DOWN**, plus occasional RIGHT |

Step back and you are UP; lean in and you are DOWN. STRAIGHT is the first
stage and every later stage waits behind it, so enrolment was unusable outside
a ~15% band of distance.

**This is the third appearance of one bug.**

1. **SE-12, Phase 3** — the liveness challenge compared frame-normalised yaw
   against a constant while the geometry gate worked in face widths, so past a
   ~137 px face box the challenge demanded more turn than the gate allowed.
   Fixed there, in `vision/liveness.py`.
2. **[L18](#l18), earlier this session** — frontality gating *tracking* rather
   than the decision, so turning as instructed deleted the track.
3. **Here.**

And `vision/pose.py` **knew**. Its docstring carried a ⚠️ naming SE-12, saying
these thresholds had the same shape of bug, and then argued it was contained:
*"the enrolment stages also constrain distance - CLOSE/MEDIUM/FAR pin the face
ratio into narrow bands, and `good_distance()` bounds it everywhere else."*
The first clause covers **15 of 100 images**. The five POSE stages pin nothing,
and `good_distance()` admits a 45× range of face area. **The containment
argument was never checked against the stages it claimed to cover.**

A test even pinned the defect as a feature:
`assert pytest.approx(0.015) == PITCH_DOWN - PITCH_UP`, under the docstring
*"STRAIGHT is a narrow band ... spelled out because it is surprising."* It was
surprising because it was wrong.

**How to apply:**

- **A ratio needs its denominator named in the constant.** `YAW_TURNED = 0.015`
  says nothing; `STRAIGHT_MAX_YAW_OF_WIDTH = 0.085` cannot be misread, and
  `get_head_pose()` now *requires* the face box so the frame-relative version
  is unrepresentable rather than merely discouraged.
- **A deferral with a containment argument is a claim, and claims get
  measured.** "It is bounded elsewhere" was written down, never tested, and
  survived two sprints. One script over the existing dataset would have shown
  it covered 15% of the images.
- **When a known bug shape recurs, sweep for it rather than fixing the
  instance.** After SE-12 the right move was to grep every threshold in the
  codebase for its units. Two more instances were sitting there, both
  documented, both waiting.
- **"Surprising" in a comment is a smell.** Behaviour that needs to be
  "spelled out because it is surprising" is usually not a subtlety worth
  preserving.

---

## L25 — Naming a limitation is not controlling for it

**2026-08-21, UAT round 4: "enrolment still can't detect front facing face at
any distance."**

I audited the detector with the project's own dataset composited into canonical
frames, and produced a confident, wrong answer: **MediaPipe cannot see a face
below ~22% of frame width.** It came with a measured table, a named mechanism
(FaceMesh's BlazeFace *short-range* detector), two controls that isolated
fraction-of-frame from pixel size and sharpness, and a remedy validated at
39/39. It was internally consistent at every point.

The user offered their webcam. **On a real face the detection rate was 100% at
every size, including 12.5% of frame width where the synthetic test had scored
0/21.** There is no reach problem. The real cause was two floors below:
`ENROLMENT_PROFILE`'s eye-separation ceiling of 0.70 sitting on a population
whose median is 0.68–0.72, reported through a `"No face detected."` message
that names a failure which never happened.

The stimulus was the whole error. A tight 200x200 grayscale aligned crop pasted
into flat grey has no neck, no shoulders, no hair past the crop and a hard
square edge. The detector needs that context; the gates being tested do not.
**I tested the gates with an input valid for the gates and invalid for the
thing upstream of them.**

**Why this is [L9](#l9) and still earns its own entry.** L9 says a benchmark's
assumptions are measurements too. I knew that — the first version of the audit
**listed the synthetic input as its stated limitation, in a section called
"Limitations", in writing**, and then reported the headline finding anyway with
"expect the floor to move a few percent, not to disappear". Writing the caveat
discharged the feeling of having handled it. It handled nothing.

**How to apply:**

- **A limitation you can state is a limitation you can test.** If the sentence
  "these are not real frames" is true enough to write down, it is the next
  experiment, not a footnote. The cost here was one question to the user.
- **When a system has a human operator with the real hardware, the real input
  is one ask away** — [L15](#l15)'s point, arriving from the input side rather
  than the environment side. I ran four synthetic sweeps before asking.
- **Controls prove your harness is consistent, not that it is relevant.**
  Controls A and B were sound and answered "is it fraction or pixels?" perfectly.
  Neither could answer "is this a face?", because both were built from the same
  invented stimulus. Rigour inside a harness does not propagate outward.
- **Rank hypotheses by how cheaply they can be tested against reality, not by
  how well the evidence you already have fits them.** The gate breakdown that
  actually found the defect was a fifteen-line change to a script I had already
  written, and it named the cause in one sitting.
- **A wrong audit is more dangerous than no audit**, because CLAUDE.md makes
  the next agent read it. The correction goes *in the document*, at the top,
  with the retracted claim spelled out — not quietly amended.

---

## L24 — Two wrongs that cancel in one line and disagree everywhere else

**Same session. Found while fixing the capture page's missing CSS.**

`enrol.js` mirrored every uploaded frame:

```js
// Undo the preview's mirror before uploading: the operator sees a mirror,
// the server must see the real orientation or LEFT and RIGHT are swapped.
context.translate(scratch.width, 0);
context.scale(-1, 1);
context.drawImage(video, 0, 0);
```

Right about the stakes, wrong about the mechanism. **`drawImage(video)` samples
the decoded video frame; no CSS transform touches it.** So there was no
preview mirror to undo - the flip *introduced* one, and the server saw a
mirrored world. `LEFT` is `nose.x > eye_centre.x`, a turn to the subject's own
left as the camera sees it, so a student told **"TURN SLIGHTLY LEFT" could only
satisfy it by turning right**.

Worse than the labels: the saved crop was a mirror image of the face. Neither
`capture_dataset.py` nor `recognize_face.py` mirrors anything, so browser
enrolment would have trained the model on mirrored faces and then matched
un-mirrored ones against them - on a face that is not symmetric.

**The part worth keeping.** `drawBox()` carried the comment *"Mirrored
horizontally to match the preview, which is flipped so it behaves like a
mirror"*. That sentence was false twice over - the preview had no CSS at all,
and the box arrived in mirrored coordinates - and the two errors **cancelled
exactly in that one function**. It was the only place in the codebase where the
mirroring was self-consistent, and it was self-consistent for the wrong reason.
Fixing either error alone would have broken the face box.

**How to apply:**

- **Establish where a transform actually applies before reasoning about
  undoing it.** Presentation-layer transforms (CSS) and data-layer ones
  (canvas, upload) are different pipelines; a comment that conflates them will
  read as correct forever.
- **When a comment describes a compensation, check both halves exist.** "Undo
  X before Y" is two claims: that X happens, and that this undoes it. Here
  neither held.
- **A line that works can be evidence of two bugs, not zero.** `drawBox()`
  was the one correct-looking thing in the chain and the reason nobody looked
  further. Compensating errors are found by checking each stage against
  reality, never by checking the end result.
- Related: [L20](#l20) - the missing stylesheet is what made the preview
  un-mirrored, so this and that are the same root cause wearing two symptoms.


---

## L26 - Two guards that each point at the other are an outage, not a safeguard

**Reported as:** "retraining is not working on the UI."

`train_model()` refuses the whole run while any `{student_id}_{name}` folder
exists and tells the operator to run `scripts/rename_dataset_folders.py
--apply`. That script refuses when the target `{student_id}` folder already
exists, and says "merge them by hand". Both refusals are individually
defensible and were written in different sprints. Together they were a
deadlock: the Retrain button failed in 0.0s, the failure message named the
remedy, the remedy refused, and there was no documented third step. Nothing
in either file was wrong when read on its own.

`dataset/` had drifted into exactly that state - three `{id}_{Name}` folders
beside their three `{id}` counterparts. `handover-phase-6a.md` §1.4 had
already looked at those folders and recorded them as **"inert"**, because
`parse_dataset_folder()` returns None for them and training skips what it
cannot parse. That was true when it was written and false by the time it was
read: a later sprint added the pre-migration guard, which turned the same
folders from ignored into fatal. The handover was not wrong; it aged.

**How to apply:**

- **When an error message names a remedy, run the remedy before believing
  it.** The whole defect lived in the gap between "the fix is one documented
  command" and what that command does on this machine. One dry run - which
  changes nothing - would have found it in seconds.
- **A guard that refuses is only finished when some action clears it.** Refuse,
  say why, and leave a path. "Merge them by hand" is not a path when the other
  half of the system refuses every state a hand-merge can reach.
- **"Inert" is a claim about the code as it stood.** A finding that says data
  is harmless is invalidated by any later commit that adds a check over that
  data, and nothing links the two. Re-verify inertness claims against the
  current code, not against the handover that made them ([L8](#l8)).
- **Equal byte totals are not equal contents.** All three folder pairs matched
  on total size, which is suggestive and proves nothing. The decision to move
  a folder of face images aside was made on a per-file byte comparison, and
  `folders_are_identical()` is written that way on purpose.


---

## L27 - A counter nobody reads is not instrumentation

`CameraReader` counted `_read_failures` from the day it was written and
exposed it as a property. **Nothing in the codebase ever read it.** So a camera
that died mid-session - unplugged, taken by another application, driver
wedged - produced a silent permanent stall: `read()` returned `(False, None)`,
the recognition loop `continue`d, the operator saw a frozen picture, and not
one line was written anywhere. The counter had been "monitoring" for three
phases and had never once told anybody anything.

The same shape twice more in the same area:

- **`generate_frames()` released the stream slot in a `finally`.** That reads
  as airtight. But a generator's `finally` runs when the generator is *closed
  or collected*, and for an MJPEG response whose browser navigated away that is
  not prompt - so the slot stayed held and every later `/video_feed` was
  refused with "already open in another window". The cleanup existed; the
  guarantee people assumed from it did not.
- **Nothing released the camera at process exit.** No `atexit`, no signal
  handler, no shutdown path in `app.py`, and `logout()` is `session.clear()`.
  The device stayed claimed after the process ended, and Windows then reported
  it as healthy while refusing to open it - which reads as a hardware fault and
  sent the investigation to drivers and reboots.

**How to apply:**

- **Grep for a caller before trusting a counter, a flag or a health field.** If
  the only reference is the assignment, it is decoration. Either give it a
  threshold and a log line, or delete it - leaving it implies a safety net that
  is not there.
- **`finally` guarantees ordering, not timeliness.** For anything holding a
  scarce exclusive resource - a camera, a lock, a device handle - pair the
  tidy path with one that does not depend on the holder behaving: a heartbeat
  and a takeover, a timeout, a lease.
- **Ask what releases a resource when the process *ends*, not only when the
  request does.** Every release path here was per-request. None was
  per-process, and the failure only showed up outside the application, in
  another program, as an error code that named neither.
- **Verify the limits of a fix as well as the fix.** `atexit` was confirmed to
  fire on a normal exit and on `KeyboardInterrupt`, and confirmed **not** to
  fire on a hard kill - all three in real subprocesses. The docstring says so
  because it was measured, not because it sounded right.

---

## L28 - A guard that reads process memory can be switched off by the client's call order

`/end-attendance` refuses a submission naming a subject other than the one
running. The check was written deliberately, documented at length, and pinned
by a whole test file. **It never once ran in production.**

`static/js/attendance.js` POSTed `/stop_camera` and submitted the End form in
its `.then()`. `RecognitionSession.stop()` sets `_subject = None`. So the route
always read `active = None`, skipped the comparison, took its "the server has
forgotten, trust the form" fallback, and wrote the absent register for a class
that was never held - `session_id` NULL, the real session's row left open, the
dashboard counting it active for the rest of the day. HTTP 200 throughout.

Nobody wrote a bug. Two correct pieces were composed in an order neither of
them stated: the browser released a device, and in doing so erased the fact the
server was about to make a decision on.

**Why the tests could not see it.** `tests/test_end_attendance_subject.py` set
`FakeSession.subject` and posted the form. Its fake `stop_camera` recorded a
boolean and, unlike the real one, **left the subject in place** - so the fixture
could not express the only state production ever reaches. Seven tests, all
green, all describing a system that does not exist. The same shape as
[L15](#l15) and [L26](#l26): the test agreed with the code because both were
written from the same wrong picture.

**How to apply:**

- **Ask what the client calls *before* the request the guard lives in.** A
  server-side check is only as good as the state it reads, and a separate
  endpoint that mutates that state is part of the guard's input whether or not
  anyone wrote it down. Read the JavaScript, not just the route.
- **Prefer state that was written down over state a process remembers.** The
  fix was not the deleted `fetch`; that only stops *this* client creating the
  situation. It was consulting the `attendance_sessions` row, which exists
  before the camera opens and survives a stop, a restart, a second browser and
  a cached page still running last week's script.
- **A fallback branch is a trust decision, not an edge case.** "The server has
  forgotten, so trust the form" was written for a restart and quietly became
  the *only* path taken. Whenever a branch relaxes a check, ask what makes it
  rare - and if the answer is "the client does not normally do that", it is not
  rare.
- **Make the fake do what the real one does, or the fixture becomes the
  specification.** The one-line faithfulness fix - clearing the subject in the
  fake `stop_camera` - is what let the finding be written as a test at all.
- **When a test greps source to ban a construction, strip the comments first.**
  This codebase explains its bugs in prose beside the code, so a search for
  the banned thing matches the paragraph describing its absence
  ([L12](#l12), [L21](#l21)).

---

## L29 - Adding a check without adding the path that satisfies it moves the failure, it does not remove it

FS-3 was a real finding and its fix was correct: `save_attendance()` checks
`enrolments`, because being in the database is not being in the class. Phase 4
added the table, the check, and a screen that fills it in.

**Nothing added the step to the flow people actually use.** `/enrol/finish`
wrote the `students` row and started a retrain; `web/subjects.py` was the only
writer of `enrolments` in the entire codebase, reached from Subjects → Class
List, which the enrolment page never linked to or mentioned. So the obvious
sequence - enrol a student, start a session, stand them in front of the camera
- ended in **"Not Enrolled" for a student who had just been enrolled.** The
word meant two different things on two screens.

The constraint did its job perfectly. The system was still unusable by the
route an operator would take, and the audit that found FS-3 could not see it,
because the schema and the code were both right.

**And the message hid a third thing.** `save_attendance()` returned a bare
`None` for *not a student*, *not in this class* **and** *the database raised*,
all rendered as one orange "Not Enrolled". A MySQL outage looked like somebody
forgetting to add a name to a class list - the failure that gets reported as
paperwork instead of an outage.

**How to apply:**

- **After adding a constraint, walk the primary flow that has to satisfy it.**
  Not the schema, not the tests - the screens, in order, as an operator. If the
  step that satisfies the new rule lives somewhere the flow never mentions,
  the rule is a trap rather than a guard.
- **Grep for the writers of a new relation.** If everything writes it from one
  administrative screen and nothing writes it from the flow that creates the
  rows it points at, that gap is the finding.
- **A rejection message with one wording and three causes is three bugs
  wearing one coat.** Split it by what the person reading it can *do*: "add
  them to a class" and "the database is down" are different actions, so they
  are different messages and different colours.
- **Fix the visibility before the convenience.** A control on the registration
  form only helps students enrolled *after* it exists. Whoever is already in no
  class, and whoever gets unenrolled next term, is reached only by the screen
  that says so - which is why that half was built first.

---

## L30 - When a refactor removes what a guard was protecting, the guard becomes pure cost

`validate_student_name()` refused any name ending in a period. The reasoning
was sound and written down beside it: the dataset folder was
`{student_id}_{student_name}`, the name was therefore a path component, and
Windows silently strips trailing dots from those - `Jose Jr.` would have been
created as `Jose Jr` and never found again by a lookup that rebuilt the path
from the database.

**Phase 5 moved the folder to `dataset/{student_id}`.** The name stopped being
part of any path. The module header says so in as many words, and
`student_folder_name()` two functions below carries a warning that it *"took a
`name` argument until Phase 5"* - and the rule stayed, in the same file,
rejecting `Juan Dela Cruz Jr.` and every other `Jr.` and `Sr.` in a system
deployed in the Philippines.

Nobody wrote a bug and nobody removed the wrong thing. The migration did
exactly what it set out to do; what it did not do was ask which *other* rules
existed only because of the scheme it was replacing. A test pinned the old
behaviour, so the suite defended it all the way through.

**How to apply:**

- **When you remove a constraint from the design, grep for the rules that
  existed to satisfy it.** "The name is no longer a path" should have been a
  search for every check whose justification says *path*, *folder* or
  *Windows*. The justification was right there in the comment, which is what
  made it findable and what nobody re-read.
- **A guard whose premise is gone does not become harmless, it becomes pure
  cost.** It still rejects, it just no longer buys anything - and it is harder
  to spot than a missing check, because the code, the comment and the test all
  agree with each other. Compare [L8](#l8): an inherited warning is a claim
  with a date on it.
- **Measure which inputs actually broke before writing it up.** The rule was
  positional, on the last character, so `Ma. Teresa Santos` was never affected.
  Asserting otherwise would have put a wrong claim into three documents.
- **Replace it with a rule in the right vocabulary rather than deleting it.**
  "Must contain at least one letter or digit" refuses `.` and `..` - what the
  old check was still usefully catching - and it is a statement about names, so
  the next filesystem migration cannot silently invalidate it.
- **Two validators that look inconsistent may both be right.** An ID *is* the
  folder name and still refuses a trailing dot; a name is not and does not.
  That is stated in both places and pinned by a test, because the next reader's
  first instinct will be to make them match.
