"""
One LBPH model per student, answering as if they were one model (todo.md §19).

The single `trainer.yml` grew ~18.7 MB per student (318 MB for 17, measured
2026-10-28) and `cv::FileStorage` cannot read a model back past 0.57-1.84 GB
(PE-0), which capped the system at roughly 31 enrolled students (PE-2).

## Why splitting the model changes no prediction

LBPH `predict()` is a nearest neighbour: it compares the face with every
stored histogram and returns the label of the closest one, keeping the first
on a tie (`dist < min`). Each student's model returns the distance to that
student's closest histogram, so the smallest of those, taken in label order
with the same strict comparison, is the same histogram the single model would
have picked - same label, same distance. `RECOGNITION_THRESHOLD` (58.0) is on
the same scale and stays valid. That is a claim to measure, not to trust:
§19 step 1 compared both on every dataset image and the numbers are in the
handover.

## Speed, measured 2026-10-28 (17 students, 12 CPUs)

Every `predict()` call first turns the face into its own LBP histogram, so N
models pay that N times where one model paid it once: 302 ms per face against
212 ms, run one after another. OpenCV releases the GIL inside `predict()`, so
`pool` spreads the students over the CPUs: **96 ms**, results equal. With
several faces in a frame the faces are already spread over the CPUs
(`recognize_face._PREDICT_POOL`), and then the total work counts - about 40%
more than the single model.

This module imports no OpenCV. The recognizers are passed in, so tests can use
stubs with a `predict()` method.
"""

from __future__ import annotations

import math
from concurrent.futures import Executor
from typing import Any, Iterable


def _closest(models, face) -> tuple[int, float]:
    best_label, best_distance = -1, math.inf

    for label, recognizer in models:
        _, distance = recognizer.predict(face)

        # Strictly less, as OpenCV's own collector does: on a tie the lower
        # label wins, exactly as it did inside one model.
        if distance < best_distance:
            best_label, best_distance = label, distance

    return best_label, best_distance


class StudentModels:
    """
    Per-student LBPH recognizers behind the one `predict()` the pipeline calls.

    `models` is `(label, recognizer)` pairs. Each recognizer holds one
    student's histograms; the label it was trained with is ignored and the
    label given here - the one in labels.txt - is returned instead.

    `pool`, when given, runs the students in `workers` interleaved groups at
    once. ⚠️ It must not be the pool that calls `predict()`: a worker waiting
    on its own pool can deadlock it.
    """

    def __init__(
        self,
        models: Iterable[tuple[int, Any]],
        pool: Executor | None = None,
        workers: int = 1,
    ):
        self._models = sorted(models, key=lambda pair: pair[0])
        self._pool = pool
        self._groups = [
            group
            for group in (self._models[i::workers] for i in range(max(workers, 1)))
            if group
        ]

    def __len__(self) -> int:
        return len(self._models)

    def predict(self, face) -> tuple[int, float]:
        """`(label, distance)` of the closest student, or `(-1, inf)` for none."""
        if self._pool is None or len(self._groups) < 2:
            return _closest(self._models, face)

        results = self._pool.map(lambda group: _closest(group, face), self._groups)

        # Each group keeps its lowest label on a tie; so does this.
        return min(results, key=lambda result: (result[1], result[0]))
