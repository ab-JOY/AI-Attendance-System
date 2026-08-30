"""
Staging, promotion and discard - the mechanics that make enrolment atomic.

FS-9: the student row used to be inserted before the capture ran, so cancelling
left a student with no images, which then broke every retrain. FS-15: recapture
deleted the old images before taking new ones, so a failure left nothing at all.
Both are properties of *when* files move, so they are tested here against a
temporary directory rather than reasoned about.

No student identifier here is real (lessons.md L6), and nothing touches the
configured dataset directory - every test is pointed at tmp_path.
"""

from __future__ import annotations

import numpy as np
import pytest

from infra import dataset_store
from security.paths import UnsafeStudentPathError, student_image_path

STUDENT = "SEC-TEST-NOBODY"
NAME = "Test Student"
TOTAL = 100


@pytest.fixture
def store(tmp_path, monkeypatch):
    """dataset_store pointed at a scratch tree instead of the real one."""
    from config.settings import settings

    dataset = tmp_path / "dataset"
    staging = tmp_path / "dataset_staging"
    dataset.mkdir()
    staging.mkdir()

    monkeypatch.setattr(settings, "dataset_dir", dataset)
    monkeypatch.setattr(settings, "dataset_staging_dir", staging)

    return dataset, staging


def an_image():
    rng = np.random.default_rng(7)
    return rng.integers(40, 210, size=(200, 200), dtype=np.uint8)


def capture(count, total=TOTAL):
    """Write `count` images into a fresh staging folder."""
    folder = dataset_store.begin(STUDENT)

    for index in range(1, count + 1):
        dataset_store.save_image(folder, index, total, an_image())

    return folder


# ==============================
# Staging
# ==============================


def test_begin_creates_an_empty_folder_under_staging(store):
    dataset, staging = store

    folder = dataset_store.begin(STUDENT)

    assert folder.is_dir()
    assert folder.parent == staging.resolve()
    assert list(folder.iterdir()) == []
    assert list(dataset.iterdir()) == [], "Nothing may appear in dataset/ yet"


def test_begin_clears_a_previous_abandoned_attempt(store):
    capture(5)

    folder = dataset_store.begin(STUDENT)

    assert dataset_store.count_images(folder) == 0, (
        "Images from an abandoned attempt would produce a dataset that is "
        "neither capture"
    )


def test_images_are_named_from_one(store):
    folder = capture(3)

    assert sorted(p.name for p in folder.iterdir()) == ["1.jpg", "2.jpg", "3.jpg"]


def test_an_index_outside_the_plan_is_refused(store):
    folder = dataset_store.begin(STUDENT)

    for bad in (0, -1, TOTAL + 1):
        with pytest.raises(UnsafeStudentPathError):
            dataset_store.save_image(folder, bad, TOTAL, an_image())


def test_a_non_integer_index_is_refused(store):
    """
    The index decides a filename. It comes from the session's own counter, but
    the type check is cheap and the failure mode - a path assembled from
    something else entirely - is not.
    """
    folder = dataset_store.begin(STUDENT)

    for bad in ("1", "../../evil", 1.0, None):
        with pytest.raises(UnsafeStudentPathError):
            student_image_path(folder, bad, TOTAL)


def test_a_boolean_index_is_refused(store):
    """bool is an int in Python, and True would silently mean image 1."""
    folder = dataset_store.begin(STUDENT)

    with pytest.raises(UnsafeStudentPathError):
        student_image_path(folder, True, TOTAL)


def test_images_are_written_greyscale_without_clahe(store):
    """
    The invariant train_model.py depends on. preprocess_for_lbph() applies
    CLAHE exactly once, later; applying it here too would put every dataset
    image through it twice and change what the model learns without changing
    any code that looks wrong.
    """
    import cv2

    folder = capture(1)
    written = cv2.imread(str(folder / "1.jpg"), cv2.IMREAD_UNCHANGED)

    assert written is not None
    assert written.shape == (200, 200), "Not a single-channel 200x200 crop"


# ==============================
# Promotion
# ==============================


def test_a_complete_capture_is_promoted(store):
    dataset, staging = store
    capture(4, total=4)

    final = dataset_store.promote(STUDENT, expected=4)

    assert final.parent == dataset.resolve()
    assert dataset_store.count_images(final) == 4
    assert list(staging.iterdir()) == [], "Staging should be empty afterwards"


def test_an_incomplete_capture_is_refused(store):
    """
    Checked against what is on disk, not against the session's counter. The
    counter says what the protocol believes; this says what a retrain will
    find, and FS-2 was possible because those two could differ.
    """
    dataset, _ = store
    capture(3, total=4)

    with pytest.raises(dataset_store.DatasetStoreError, match="3 images on disk"):
        dataset_store.promote(STUDENT, expected=4)

    assert list(dataset.iterdir()) == [], "Nothing may reach dataset/"


def test_promotion_replaces_an_existing_dataset(store):
    capture(4, total=4)
    dataset_store.promote(STUDENT, expected=4)

    capture(6, total=6)
    final = dataset_store.promote(STUDENT, expected=6)

    assert dataset_store.count_images(final) == 6


def test_a_failed_recapture_leaves_the_original_dataset_intact(store, monkeypatch):
    """
    FS-15. The old route deleted the student's images and then captured
    replacements, so any failure left them with nothing at all.
    """
    capture(4, total=4)
    original = dataset_store.promote(STUDENT, expected=4)

    assert dataset_store.count_images(original) == 4

    capture(6, total=6)

    real_rename = dataset_store.os.rename
    calls = {"n": 0}

    def fail_on_the_promotion(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:  # 1 = rotate aside, 2 = promote
            raise OSError("simulated failure part-way through")
        return real_rename(src, dst)

    monkeypatch.setattr(dataset_store.os, "rename", fail_on_the_promotion)

    with pytest.raises(dataset_store.DatasetStoreError):
        dataset_store.promote(STUDENT, expected=6)

    assert dataset_store.count_images(original) == 4, (
        "The student lost the dataset they already had"
    )


def test_no_previous_folder_survives_a_successful_promotion(store):
    dataset, _ = store
    capture(4, total=4)
    dataset_store.promote(STUDENT, expected=4)
    capture(4, total=4)
    dataset_store.promote(STUDENT, expected=4)

    leftovers = [p.name for p in dataset.iterdir() if p.name.endswith(".previous")]

    assert not leftovers, f"Left behind: {leftovers}"


# ==============================
# Discard and sweep
# ==============================


def test_discard_removes_an_in_progress_capture(store):
    dataset, staging = store
    capture(7)

    dataset_store.discard(STUDENT)

    assert list(staging.iterdir()) == []
    assert list(dataset.iterdir()) == []


def test_discarding_nothing_is_harmless(store):
    dataset_store.discard(STUDENT)


def test_sweep_removes_only_folders_older_than_the_cutoff(store):
    import os
    import time

    _, staging = store
    folder = capture(2)

    assert dataset_store.sweep(older_than_seconds=3600) == 0
    assert folder.is_dir()

    old = time.time() - 7200
    os.utime(folder, (old, old))

    assert dataset_store.sweep(older_than_seconds=3600) == 1
    assert not folder.exists()


def test_sweep_on_a_missing_root_is_harmless(tmp_path, monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "dataset_staging_dir", tmp_path / "absent")

    assert dataset_store.sweep() == 0


# ==============================
# Containment
# ==============================


@pytest.mark.parametrize(
    "student_id",
    ["../escape", "..\\escape", "a/b", "C:evil", ""],
)
def test_a_hostile_student_id_never_reaches_the_filesystem(store, student_id):
    with pytest.raises(UnsafeStudentPathError):
        dataset_store.begin(student_id)


def test_staging_and_dataset_are_different_roots(store):
    dataset, staging = store

    from security.paths import student_dataset_path, student_staging_path

    assert student_staging_path(STUDENT).parent == staging.resolve()
    assert student_dataset_path(STUDENT).parent == dataset.resolve()
