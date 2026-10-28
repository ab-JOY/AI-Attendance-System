"""
Retraining trains only the students whose images changed (todo.md §19).

Every enrolment used to retrain every student - 56 s at 17, and it grows
with the school. The risk in reusing a model is reusing a *stale* one: a
student recaptured, an image deleted, or the training settings changed (L2
is what a model built under other settings does - it rejects everyone, with
no error). Each of those must retrain; everything else must not.
"""

import os
import time

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

import train_model  # noqa: E402  - imported after the cv2 availability check

pytestmark = pytest.mark.slow

STUDENTS = ("23-1-1-0559", "23-1-1-0918", "23-1-1-0920")


def _write_images(folder, count, seed, start=0, age=0.0):
    """`age` seconds in the past: a capture that finished before training."""
    folder.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    for index in range(start, start + count):
        image = rng.integers(0, 256, size=(200, 200), dtype=np.uint8)
        path = folder / f"{index}.jpg"
        assert cv2.imwrite(str(path), image)
        if age:
            past = time.time() - age
            os.utime(path, (past, past))
    if age:
        past = time.time() - age
        os.utime(folder, (past, past))


@pytest.fixture
def school(tmp_path, monkeypatch):
    dataset_dir = tmp_path / "dataset"
    trainer_dir = tmp_path / "trainer"
    dataset_dir.mkdir()
    trainer_dir.mkdir()

    monkeypatch.setattr(train_model, "DATASET_DIR", str(dataset_dir))
    monkeypatch.setattr(train_model, "TRAINER_DIR", str(trainer_dir))
    monkeypatch.setattr(train_model, "MODELS_DIR", str(trainer_dir / "students"))
    monkeypatch.setattr(train_model, "LABELS_FILE", str(trainer_dir / "labels.txt"))
    monkeypatch.setattr(train_model, "MIN_IMAGES_PER_STUDENT", 3)

    for seed, student_id in enumerate(STUDENTS, start=1):
        _write_images(dataset_dir / student_id, 4, seed, age=60)

    assert train_model.train_model()[0]
    return dataset_dir, trainer_dir / "students"


def _retrained(caplog):
    return {
        line.split("Processing: ")[1]
        for line in caplog.text.splitlines()
        if "Processing: " in line
    }


def _train(caplog, **kwargs):
    caplog.clear()
    with caplog.at_level("INFO"):
        success, _ = train_model.train_model(**kwargs)
    assert success
    return _retrained(caplog)


def _later():
    """Nothing to wait for: the models are stamped before the run started."""


def test_nothing_changed_trains_no_one_and_keeps_every_model(school, caplog):
    _, models = school
    before = {sid: (models / f"{sid}.yml").read_bytes() for sid in STUDENTS}

    assert _train(caplog) == set()

    assert "3 person(s): 0 trained from 0 image(s), 3 unchanged" in caplog.text
    assert {sid: (models / f"{sid}.yml").read_bytes() for sid in STUDENTS} == before
    assert (models.parent / "labels.txt").read_text(encoding="utf-8") == (
        "0,23-1-1-0559\n1,23-1-1-0918\n2,23-1-1-0920\n"
    )


def test_a_recaptured_student_is_the_only_one_trained(school, caplog):
    dataset_dir, _ = school
    _later()
    _write_images(dataset_dir / "23-1-1-0918", 2, seed=9, start=4)

    assert _train(caplog) == {"23-1-1-0918"}


def test_a_deleted_image_retrains_that_student(school, caplog):
    """No newer file is left behind; only the folder's own mtime changes."""
    dataset_dir, _ = school
    _later()
    os.remove(dataset_dir / "23-1-1-0559" / "0.jpg")

    assert _train(caplog) == {"23-1-1-0559"}


def test_a_new_student_is_trained_and_the_others_reused(school, caplog):
    dataset_dir, models = school
    _later()
    _write_images(dataset_dir / "23-1-1-1000", 4, seed=7)

    assert _train(caplog) == {"23-1-1-1000"}
    assert (models / "23-1-1-1000.yml").exists()


def test_a_removed_student_leaves_the_model(school, caplog):
    import shutil

    dataset_dir, models = school
    shutil.rmtree(dataset_dir / "23-1-1-0920")

    assert _train(caplog) == set()
    assert not (models / "23-1-1-0920.yml").exists()
    assert "23-1-1-0920" not in (models.parent / "labels.txt").read_text(encoding="utf-8")


def test_changed_training_settings_retrain_everyone(school, caplog, monkeypatch):
    monkeypatch.setattr(train_model, "training_fingerprint", lambda: "something else\n")

    assert _train(caplog) == set(STUDENTS)


def test_full_trains_everyone(school, caplog):
    assert _train(caplog, full=True) == set(STUDENTS)


def test_a_reused_model_predicts_exactly_as_before(school, caplog):
    """Reuse is the same file, so the same histograms - checked, not assumed."""
    dataset_dir, models = school
    probe = cv2.imread(str(dataset_dir / "23-1-1-0559" / "1.jpg"), cv2.IMREAD_GRAYSCALE)
    probe = train_model.preprocess_for_lbph(np.fliplr(probe).copy())

    def predict():
        model = cv2.face.LBPHFaceRecognizer_create()
        model.read(str(models / "23-1-1-0559.yml"))
        return model.predict(probe)

    before = predict()
    _train(caplog)

    assert predict() == before


def test_an_image_saved_just_before_training_is_trained_again(school, caplog):
    """
    The boundary case that was measured: an image 2 ms older than the start of
    training had the same mtime as the start. Inside the margin, retrain.
    """
    dataset_dir, _ = school
    _write_images(dataset_dir / "23-1-1-0918", 1, seed=9, start=4)  # age 0

    assert _train(caplog) == {"23-1-1-0918"}
