"""
Regression tests for train_model.write_model_atomically() (RE-1).

Why this function has tests before anything else in the codebase does: the
version it replaced deleted trainer.yml and labels.txt *before* writing their
replacements, so any failure part-way through destroyed the working model
with no way back. Combined with PE-0 - a configuration that produced a model
OpenCV could write but not read - that is what took the system down and left
it unable to self-heal.

The four cases below are the ones the Phase 0 implementation was validated
against, including the exact failure that caused the outage.

These run in milliseconds. write_model_atomically() only calls
`recognizer.write(path)`, so a stub with a write() method exercises every
branch without OpenCV, without a dataset, and without training anything.
"""

import os

import pytest

import train_model


class FakeRecognizer:
    """Stands in for cv2.face.LBPHFaceRecognizer. Writes a marker file."""

    def __init__(self, payload="model-contents", fail=False):
        self.payload = payload
        self.fail = fail

    def write(self, path):
        if self.fail:
            raise RuntimeError("simulated cv::FileStorage failure")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(self.payload)


@pytest.fixture
def model_paths(tmp_path, monkeypatch):
    """
    Point the module-level path globals at a temporary directory.

    write_model_atomically() reads TRAINER_FILE and LABELS_FILE as globals, so
    monkeypatching the module attributes is what redirects it. Without this
    the tests would overwrite the real trained model.
    """
    trainer = tmp_path / "trainer.yml"
    labels = tmp_path / "labels.txt"

    monkeypatch.setattr(train_model, "TRAINER_FILE", str(trainer))
    monkeypatch.setattr(train_model, "LABELS_FILE", str(labels))

    return trainer, labels


def test_writes_both_files_when_no_previous_model_exists(model_paths):
    """First-ever train: both files appear, and no .bak is invented."""
    trainer, labels = model_paths

    train_model.write_model_atomically(
        FakeRecognizer("first-model"),
        {0: "23-1-1-0559_Chrizol D. Evangelista"},
    )

    assert trainer.read_text(encoding="utf-8") == "first-model"
    assert labels.read_text(encoding="utf-8") == (
        "0,23-1-1-0559_Chrizol D. Evangelista\n"
    )

    assert not trainer.with_suffix(".yml.bak").exists()
    assert not labels.with_suffix(".txt.bak").exists()


def test_previous_model_is_rotated_to_bak_on_overwrite(model_paths):
    """A retrain promotes the new pair and keeps one generation as .bak."""
    trainer, labels = model_paths

    train_model.write_model_atomically(FakeRecognizer("old-model"), {0: "old_folder"})
    train_model.write_model_atomically(FakeRecognizer("new-model"), {0: "new_folder"})

    assert trainer.read_text(encoding="utf-8") == "new-model"
    assert labels.read_text(encoding="utf-8") == "0,new_folder\n"

    backup_trainer = tmp_sibling(trainer, ".bak")
    backup_labels = tmp_sibling(labels, ".bak")

    assert backup_trainer.read_text(encoding="utf-8") == "old-model"
    assert backup_labels.read_text(encoding="utf-8") == "0,old_folder\n"


def test_failure_during_write_leaves_live_model_untouched(model_paths):
    """
    This is the outage scenario.

    The model write blows up part-way. The previously trained model must
    survive completely intact, and no temporary files may be left behind for
    the next run to trip over.
    """
    trainer, labels = model_paths

    train_model.write_model_atomically(FakeRecognizer("good-model"), {0: "good_folder"})

    with pytest.raises(RuntimeError, match="simulated cv::FileStorage failure"):
        train_model.write_model_atomically(
            FakeRecognizer("doomed", fail=True), {0: "doomed_folder"}
        )

    assert trainer.read_text(encoding="utf-8") == "good-model"
    assert labels.read_text(encoding="utf-8") == "0,good_folder\n"

    assert not os.path.exists(str(trainer) + ".tmp")
    assert not os.path.exists(str(labels) + ".tmp")


def test_failure_during_promotion_restores_the_backup(model_paths, monkeypatch):
    """
    The new pair is written fine, but promoting it fails half way - the
    trainer is swapped in and the labels swap then dies. The rollback must put
    the previous good pair back, because a trainer.yml from one generation
    beside a labels.txt from another maps faces to the wrong students.
    """
    trainer, labels = model_paths

    train_model.write_model_atomically(FakeRecognizer("good-model"), {0: "good_folder"})

    real_replace = os.replace
    calls = {"n": 0}

    def failing_replace(src, dst):
        # Let the .bak rotation through, then fail on the second promotion -
        # the labels half - so the pair is briefly mismatched on disk.
        if str(dst) == str(labels) and calls["n"] == 0:
            calls["n"] += 1
            raise OSError("simulated failure promoting labels.txt")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", failing_replace)

    with pytest.raises(OSError, match="simulated failure promoting labels.txt"):
        train_model.write_model_atomically(
            FakeRecognizer("new-model"), {0: "new_folder"}
        )

    monkeypatch.setattr(os, "replace", real_replace)

    assert trainer.read_text(encoding="utf-8") == "good-model"
    assert labels.read_text(encoding="utf-8") == "0,good_folder\n"


def tmp_sibling(path, suffix):
    """Path with `suffix` appended to the full filename, matching the code."""
    return path.parent / (path.name + suffix)
