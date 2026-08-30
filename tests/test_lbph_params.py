"""
`LBPH_PARAMS` is pinned, because `RECOGNITION_THRESHOLD` is calibrated to it (D4).

⚠️ **This file exists because half of a pair was guarded and the other half was
not.** `tests/test_settings.py` asserts that `RECOGNITION_THRESHOLD` still
resolves to 58.0, and its docstring says why: that value is calibrated against
"the LBPH distance scale in train_model.LBPH_PARAMS, and the two moving apart
is exactly the failure recorded in tasks/lessons.md L2".

Nothing asserted the scale. So changing `neighbors` or the grid passed the
entire suite and CI, and then moved the distance scale out from under a
threshold that a test was busy defending - the guarded number staying put
while the thing it was calibrated to walked away.

**What that failure looks like, because it has already happened here.** L2 and
todo.md PE-0: `neighbors` was raised from 8 to 12 as an apparent optimisation.
Genuine distances went from around 35 to 81-107, every one of them above the
unchanged 58.0 threshold, and recognition returned **0/60 on a held-out split
with no error logged anywhere**. The system started, opened the camera,
tracked faces, drew boxes, and recognised nobody. It also made the model
unloadable - 262,144 dimensions per image against 16,384 - so the two symptoms
masked each other.

**Why the demo machine makes this sharper rather than milder.** It retrains on
every version update. A parameter change therefore reaches every deployed
model on the next release, with no human pause in which somebody might have
noticed a number looking wrong - the retrain is automated, and an automated
retrain of the wrong parameters is just as silent as a manual one, only
faster and on more machines.

**This test does not say 8 is the correct value.** It says the value may not
change without someone also dealing with the threshold that was derived from
it, and re-running the held-out evaluator to show what happened. If you are
here because this test failed, that is the work - not editing the expectation.
"""

from __future__ import annotations

import train_model
from config.settings import settings

# The measured, working configuration. radius=2 with neighbors=8 gives 16,384
# dimensions per image, genuine distances around 35, and 60/60 (now 80/80) on
# the held-out split at a threshold of 58.0.
EXPECTED_LBPH_PARAMS = {
    "radius": 2,
    "neighbors": 8,
    "grid_x": 8,
    "grid_y": 8,
}


def test_lbph_params_are_unchanged():
    """
    The distance scale `RECOGNITION_THRESHOLD` was calibrated against.

    ⚠️ Do not update this expectation to make a failure go away. Every one of
    these four numbers moves the distance scale, and 58.0 is only meaningful
    on the scale these produce.
    """
    assert train_model.LBPH_PARAMS == EXPECTED_LBPH_PARAMS, (
        "LBPH_PARAMS changed. RECOGNITION_THRESHOLD (58.0) is calibrated "
        "against the distance scale these parameters produce, and nothing "
        "else in the suite would have caught this: raising `neighbors` to 12 "
        "once pushed every genuine distance to 81-107 against an unchanged "
        "58.0 threshold, and recognition silently returned 0/60. If this "
        "change is deliberate, re-derive the threshold, update "
        "tests/test_settings.py in the same commit, and re-run "
        "eval_heldout_accuracy.py as the evidence."
    )


def test_neighbors_is_the_one_that_broke_it_before():
    """
    Called out separately because it is the parameter with the history.

    A dimension count is `2 ** neighbors` per cell. At 8 that is 256 bins per
    cell and 16,384 across the 8x8 grid; at 12 it is 4,096 and 262,144, of
    which at least 86% are structurally zero at this face size (todo.md PE-1).
    The model became both unpersistable and unmatchable in one change.
    """
    assert train_model.LBPH_PARAMS["neighbors"] == 8


def test_the_threshold_and_the_scale_are_still_a_matched_pair():
    """
    Both halves, asserted together, so the pairing is visible in one place.

    `test_settings.py` pins the threshold and this file pins the scale. Read
    on their own, each looks like an arbitrary constant being frozen. The
    reason either matters is that they were derived together, and this test
    is where that is written down as an executable statement rather than a
    comment.
    """
    assert settings.recognition_threshold == 58.0
    assert train_model.LBPH_PARAMS["neighbors"] == 8
    assert train_model.LBPH_PARAMS["radius"] == 2
