"""
Exit-code contract between app.py and the capture_dataset.py subprocess.

FS-12 happened because this contract was implicit. `capture_dataset.py` used
bare `sys.exit()` on its failure paths, which exits with status **0**, and
`app.py` reads 0 as "enrolment completed" and kicks off an automatic retrain.
A missing dependency, a missing argument or an unopenable camera therefore
reported a successful enrolment for a student who had no dataset.

Both sides now import these names instead of writing literals, so the two
halves of the contract cannot drift apart again.

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
