"""
Exit-code contract for this project's command-line entry points.

FS-12 happened because this contract was implicit. `capture_dataset.py` used
bare `sys.exit()` on its failure paths, which exits with status **0**, and
`app.py` reads 0 as "enrolment completed" and kicks off an automatic retrain.
A missing dependency, a missing argument or an unopenable camera therefore
reported a successful enrolment for a student who had no dataset.

Both sides now import these names instead of writing literals, so the two
halves of the contract cannot drift apart again.

⚠️ **D1 was the same defect at a second entry point, found once the demo
machine began retraining on every version update.** `train_model.py` exits 0
after a run that *skipped* a student for having too few usable images - a
correct partial success, reported as an unqualified one. A deploy gating on
the exit status sees a clean pass, and the student stays in the database, on
every roster and in every class list, while being absent from the model. That
is FS-12's shape exactly: a status of 0 standing for "finished" when it
means "finished with part of the work not done".

EXIT_INCOMPLETE exists so that case has a status of its own, rather than
having to choose between a lie (0) and an overstatement (1, which means no
model was written at all).

This module must stay free of imports and side effects. `app.py` imports it,
and `capture_dataset.py` runs a camera capture session at module scope
(MA-2), so nothing that pulls this in may transitively pull in that.
"""

# Capture completed: MAX_IMAGES images were written to the student's folder.
# app.py treats this - and only this - as success, and retrains the model.
EXIT_SUCCESS = 0

# Capture did not complete: bad arguments, a missing dependency, no usable
# camera, a read failure part-way through, or an unexpected exception.
# The student's dataset folder is absent or incomplete; do not retrain.
EXIT_FAILURE = 1

# The operator cancelled deliberately - ESC, or closing the capture window.
# Not an error: app.py redirects quietly without an error page.
EXIT_CANCELLED = 2

# Training wrote a model, but at least one student folder was skipped for
# having too few usable images, so those students are NOT in the model and
# cannot be recognised (D1).
#
# ⚠️ Deliberately non-zero, and that is the whole point. `train_model()`
# returns True here - it is a real success for everyone it did train, and one
# bad folder must never block a whole class. But a *deploy* that retrains on
# every version update needs to stop and be looked at, because the alternative
# is a roster that quietly shrinks between releases. Pass --allow-incomplete
# to accept a known-short roster and exit 0 instead.
EXIT_INCOMPLETE = 3
