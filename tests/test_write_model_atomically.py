"""
Regression tests for train_model.write_model_atomically() (RE-1).

Why this function has tests before anything else in the codebase does: the
version it replaced deleted the model and labels.txt *before* writing their
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


def fake_models(payload="model-contents", students=("good",), fail=False):
    """
    Stands in for train_model()'s per-student training: a `write_models`
    callback that writes one marker file per student, as
    `cv2.face.LBPHFaceRecognizer.write()` would, and returns the label map.
    """

    def write_models(directory):
        if fail:
            raise RuntimeError("simulated cv::FileStorage failure")
        for student_id in students:
            with open(os.path.join(directory, f"{student_id}.yml"), "w",
                      encoding="utf-8") as handle:
                handle.write(payload)
        return dict(enumerate(students))

    return write_models


@pytest.fixture
def model_paths(tmp_path, monkeypatch):
    """
    Point the module-level path globals at a temporary directory.

    write_model_atomically() reads MODELS_DIR and LABELS_FILE as globals, so
    monkeypatching the module attributes is what redirects it. Without this
    the tests would overwrite the real trained model.
    """
    models = tmp_path / "students"
    labels = tmp_path / "labels.txt"

    monkeypatch.setattr(train_model, "MODELS_DIR", str(models))
    monkeypatch.setattr(train_model, "LABELS_FILE", str(labels))

    return models, labels


def model_of(models, student_id):
    return (models / f"{student_id}.yml").read_text(encoding="utf-8")


def test_writes_both_files_when_no_previous_model_exists(model_paths):
    """First-ever train: both halves appear, and no .bak is invented."""
    models, labels = model_paths

    label_dict = train_model.write_model_atomically(
        fake_models("first-model", students=("23-1-1-0559", "23-1-1-0918"))
    )

    assert label_dict == {0: "23-1-1-0559", 1: "23-1-1-0918"}
    assert model_of(models, "23-1-1-0559") == "first-model"
    assert model_of(models, "23-1-1-0918") == "first-model"
    assert labels.read_text(encoding="utf-8") == (
        "0,23-1-1-0559\n1,23-1-1-0918\n"
    )

    assert not tmp_sibling(models, ".bak").exists()
    assert not tmp_sibling(labels, ".bak").exists()


def test_previous_model_is_rotated_to_bak_on_overwrite(model_paths):
    """
    A retrain promotes the new pair and keeps one generation as .bak.

    A student who is in the old model but not the new one must not survive in
    the live students/: the directory is replaced, not merged into.
    """
    models, labels = model_paths

    train_model.write_model_atomically(fake_models("old-model", students=("old", "gone")))
    train_model.write_model_atomically(fake_models("new-model", students=("new",)))

    assert model_of(models, "new") == "new-model"
    assert not (models / "gone.yml").exists()
    assert labels.read_text(encoding="utf-8") == "0,new\n"

    assert model_of(tmp_sibling(models, ".bak"), "old") == "old-model"
    assert tmp_sibling(labels, ".bak").read_text(encoding="utf-8") == "0,old\n1,gone\n"


def test_failure_during_write_leaves_live_model_untouched(model_paths):
    """
    This is the outage scenario.

    The model write blows up part-way. The previously trained model must
    survive completely intact, and no temporary files may be left behind for
    the next run to trip over.
    """
    models, labels = model_paths

    train_model.write_model_atomically(fake_models("good-model"))

    with pytest.raises(RuntimeError, match="simulated cv::FileStorage failure"):
        train_model.write_model_atomically(fake_models("doomed", fail=True))

    assert model_of(models, "good") == "good-model"
    assert labels.read_text(encoding="utf-8") == "0,good\n"

    assert not tmp_sibling(models, ".tmp").exists()
    assert not tmp_sibling(labels, ".tmp").exists()


def test_no_trainable_student_writes_nothing(model_paths):
    """An empty label map leaves the live model as it was, and no temporaries."""
    models, labels = model_paths

    train_model.write_model_atomically(fake_models("good-model"))

    assert train_model.write_model_atomically(fake_models(students=())) == {}

    assert model_of(models, "good") == "good-model"
    assert labels.read_text(encoding="utf-8") == "0,good\n"
    assert not tmp_sibling(models, ".tmp").exists()
    assert not tmp_sibling(labels, ".bak").exists()


def test_failure_during_promotion_restores_the_backup(model_paths, monkeypatch):
    """
    The new pair is written fine, but promoting it fails half way - the
    models are swapped in and the labels swap then dies. The rollback must
    put the previous good pair back, because a students/ from one generation
    beside a labels.txt from another maps faces to the wrong students.
    """
    models, labels = model_paths

    train_model.write_model_atomically(fake_models("good-model"))

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
            fake_models("new-model", students=("new",))
        )

    monkeypatch.setattr(os, "replace", real_replace)

    assert model_of(models, "good") == "good-model"
    assert not (models / "new.yml").exists()
    assert labels.read_text(encoding="utf-8") == "0,good\n"


def test_the_live_pair_is_not_renamed_while_it_is_being_read(model_paths):
    """
    lessons.md L36: Windows refuses to rename a file this process has open, and
    the attendance model is now read in the background after every retrain.
    So the renames wait for MODEL_FILES_LOCK, which the reader holds.
    """
    import threading

    models, _labels = model_paths
    train_model.write_model_atomically(fake_models("old-model"))

    done = threading.Event()

    def retrain():
        train_model.write_model_atomically(fake_models("new-model"))
        done.set()

    with train_model.MODEL_FILES_LOCK:  # a reader has the files open
        writer = threading.Thread(target=retrain)
        writer.start()

        assert not done.wait(0.3), "renamed the model while it was being read"
        assert model_of(models, "good") == "old-model"

    assert done.wait(5)
    writer.join(5)
    assert model_of(models, "good") == "new-model"


def tmp_sibling(path, suffix):
    """Path with `suffix` appended to the full filename, matching the code."""
    return path.parent / (path.name + suffix)
