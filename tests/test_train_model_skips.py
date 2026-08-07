"""
Tests for the skip-don't-abort behaviour in train_model() (FS-2).

The original implementation failed the entire training run if any single
dataset folder held too few images. That is how one cancelled enrolment made
the whole system unrecoverable: the incomplete folder blocked training for
every other student, so no model could be produced at all.

The fixed behaviour is to skip the under-populated folder, train everyone
else, and name the skipped student in the returned message so the operator
knows who needs a recapture.

These tests train a real LBPH model, so they are marked `slow` - but on
synthetic 200x200 images with MIN_IMAGES_PER_STUDENT patched down to 3 it is
a fraction of a second, not the minutes a real dataset takes.
"""

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

import train_model  # noqa: E402  - imported after the cv2 availability check

pytestmark = pytest.mark.slow


def _write_synthetic_images(folder, count, seed):
    """
    Deterministic noise images, one per file.

    preprocess_for_lbph() only resizes and applies CLAHE - there is no face
    detection in the training path - so arbitrary pixel data exercises the
    same code path a real capture would.
    """
    folder.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)

    for index in range(count):
        image = rng.integers(0, 256, size=(200, 200), dtype=np.uint8)
        assert cv2.imwrite(str(folder / f"{index}.jpg"), image)


@pytest.fixture
def dataset(tmp_path, monkeypatch):
    """Redirect every path train_model() touches into tmp_path."""
    dataset_dir = tmp_path / "dataset"
    trainer_dir = tmp_path / "trainer"
    dataset_dir.mkdir()
    trainer_dir.mkdir()

    monkeypatch.setattr(train_model, "DATASET_DIR", str(dataset_dir))
    monkeypatch.setattr(train_model, "TRAINER_DIR", str(trainer_dir))
    monkeypatch.setattr(train_model, "TRAINER_FILE", str(trainer_dir / "trainer.yml"))
    monkeypatch.setattr(train_model, "LABELS_FILE", str(trainer_dir / "labels.txt"))

    # 70 real images per student would make this test slow for no extra
    # coverage - the threshold comparison is what is under test, not its value.
    monkeypatch.setattr(train_model, "MIN_IMAGES_PER_STUDENT", 3)

    return dataset_dir, trainer_dir


def test_underpopulated_folder_is_skipped_and_the_rest_still_train(dataset):
    """One incomplete folder must not block every other student."""
    dataset_dir, trainer_dir = dataset

    _write_synthetic_images(dataset_dir / "23-1-1-0559_Complete Student", 5, seed=1)
    _write_synthetic_images(dataset_dir / "23-1-1-0918_Partial Student", 2, seed=2)

    success, message = train_model.train_model()

    assert success is True
    assert "23-1-1-0918_Partial Student" in message

    # The complete student made it into a usable model.
    assert (trainer_dir / "trainer.yml").exists()

    labels = (trainer_dir / "labels.txt").read_text(encoding="utf-8")
    assert "23-1-1-0559_Complete Student" in labels
    assert "23-1-1-0918_Partial Student" not in labels


def test_all_folders_underpopulated_fails_and_names_them(dataset):
    """
    With nothing trainable there is no model to produce, so this must fail -
    but it still has to say who needs recapturing rather than reporting a
    generic error.
    """
    dataset_dir, _ = dataset

    _write_synthetic_images(dataset_dir / "23-1-1-0559_Alpha Student", 1, seed=3)
    _write_synthetic_images(dataset_dir / "23-1-1-0918_Beta Student", 2, seed=4)

    success, message = train_model.train_model()

    assert success is False
    assert "23-1-1-0559_Alpha Student" in message
    assert "23-1-1-0918_Beta Student" in message


def test_duplicate_student_id_across_folders_is_rejected(dataset):
    """
    Two folders for one student ID would train two labels that both map back
    to the same person, so training refuses rather than guessing which is
    current.
    """
    dataset_dir, _ = dataset

    _write_synthetic_images(dataset_dir / "23-1-1-0559_Old Name", 5, seed=5)
    _write_synthetic_images(dataset_dir / "23-1-1-0559_New Name", 5, seed=6)

    success, message = train_model.train_model()

    assert success is False
    assert "Duplicate" in message


def test_folder_not_matching_the_naming_convention_is_ignored(dataset):
    """
    A stray directory under dataset/ is skipped with a warning, not treated
    as a student and not allowed to fail the run.
    """
    dataset_dir, trainer_dir = dataset

    _write_synthetic_images(dataset_dir / "23-1-1-0559_Valid Student", 5, seed=7)
    _write_synthetic_images(dataset_dir / "notavalidfoldername", 5, seed=8)

    success, _ = train_model.train_model()

    assert success is True

    labels = (trainer_dir / "labels.txt").read_text(encoding="utf-8")
    assert "23-1-1-0559_Valid Student" in labels
    assert "notavalidfoldername" not in labels
