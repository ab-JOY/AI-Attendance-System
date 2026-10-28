"""
StudentModels: per-student LBPH models answering as one (todo.md §19).

The claim the design rests on is that splitting the single model changes no
prediction. The stub tests below pin the rule - smallest distance, lowest
label on a tie, the manifest's label rather than the file's. The test that
trains real LBPH models checks the claim itself against OpenCV: one combined
model and the per-student models must return the same label and the same
distance for every probe.
"""

import math

import cv2
import numpy as np
import pytest

from train_model import LBPH_PARAMS
from vision.student_models import StudentModels


class FixedDistance:
    """A recognizer whose closest histogram is always `distance` away."""

    def __init__(self, distance, own_label=99):
        self.distance = distance
        self.own_label = own_label

    def predict(self, face):
        return self.own_label, self.distance


def test_the_closest_student_wins():
    models = StudentModels([(0, FixedDistance(70.0)), (1, FixedDistance(41.3)), (2, FixedDistance(66.9))])

    assert models.predict("face") == (1, 41.3)


def test_a_tie_goes_to_the_lower_label_as_inside_one_model():
    """OpenCV keeps the first histogram on a tie, and histograms are in label order."""
    models = StudentModels([(2, FixedDistance(40.0)), (1, FixedDistance(40.0))])

    assert models.predict("face") == (1, 40.0)


def test_the_label_is_the_manifests_not_the_files():
    models = StudentModels([(5, FixedDistance(30.0, own_label=0))])

    assert models.predict("face") == (5, 30.0)


def test_no_models_predicts_no_one():
    label, distance = StudentModels([]).predict("face")

    assert label == -1
    assert math.isinf(distance)


def _faces(seed, count):
    rng = np.random.default_rng(seed)
    base = rng.integers(0, 256, size=(64, 64), dtype=np.uint8)
    noise = rng.integers(-40, 40, size=(count, 64, 64))
    return [np.clip(base + n, 0, 255).astype(np.uint8) for n in noise]


@pytest.mark.skipif(not hasattr(cv2, "face"), reason="needs opencv-contrib")
def test_per_student_models_match_one_combined_model():
    """
    The equivalence, on real OpenCV LBPH: same label and same distance.

    Three synthetic "students", each a base image plus noise, so that probes
    are genuinely closer to one student and the distances are not trivial.
    """
    students = [_faces(seed, 6) for seed in (1, 2, 3)]

    combined = cv2.face.LBPHFaceRecognizer_create(**LBPH_PARAMS)
    combined.train(
        [face for faces in students for face in faces],
        np.asarray([label for label, faces in enumerate(students) for _ in faces], dtype=np.int32),
    )

    per_student = []
    for label, faces in enumerate(students):
        model = cv2.face.LBPHFaceRecognizer_create(**LBPH_PARAMS)
        model.train(faces, np.full(len(faces), label, dtype=np.int32))
        per_student.append((label, model))
    split = StudentModels(per_student)

    probes = [face for seed in (1, 2, 3) for face in _faces(seed + 10, 4)]
    probes += [np.fliplr(face).copy() for faces in students for face in faces]

    for probe in probes:
        assert split.predict(probe) == combined.predict(probe)


@pytest.mark.parametrize("workers", [2, 3, 8])
def test_the_pool_gives_the_same_answer_as_one_by_one(workers):
    """
    Groups are interleaved, so the lowest label on a tie can sit in any group;
    the pool must still return it, as the single model would.
    """
    from concurrent.futures import ThreadPoolExecutor

    distances = [50.0, 41.3, 70.0, 41.3, 66.9, 41.3, 90.0]
    pairs = [(label, FixedDistance(d)) for label, d in enumerate(distances)]

    with ThreadPoolExecutor(workers) as pool:
        pooled = StudentModels(pairs, pool=pool, workers=workers)

        assert pooled.predict("face") == StudentModels(pairs).predict("face") == (1, 41.3)
