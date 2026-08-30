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

    _write_synthetic_images(dataset_dir / "23-1-1-0559", 5, seed=1)
    _write_synthetic_images(dataset_dir / "23-1-1-0918", 2, seed=2)

    success, message = train_model.train_model()

    assert success is True
    assert "23-1-1-0918" in message

    # The complete student made it into a usable model.
    assert (trainer_dir / "trainer.yml").exists()

    labels = (trainer_dir / "labels.txt").read_text(encoding="utf-8")
    assert "23-1-1-0559" in labels
    assert "23-1-1-0918" not in labels


def test_all_folders_underpopulated_fails_and_names_them(dataset):
    """
    With nothing trainable there is no model to produce, so this must fail -
    but it still has to say who needs recapturing rather than reporting a
    generic error.
    """
    dataset_dir, _ = dataset

    _write_synthetic_images(dataset_dir / "23-1-1-0559", 1, seed=3)
    _write_synthetic_images(dataset_dir / "23-1-1-0918", 2, seed=4)

    success, message = train_model.train_model()

    assert success is False
    assert "23-1-1-0559" in message
    assert "23-1-1-0918" in message


def test_duplicate_student_id_across_folders_is_rejected(dataset):
    """
    Two folders that resolve to one student ID must fail the run.

    ⚠️ **This got harder to reach and is still reachable, which is why it is
    still here.** Under `{id}_{name}` it was easy - `23-1-1-0559_Old Name`
    beside `23-1-1-0559_New Name`. The folder is the ID now, so two of them
    cannot differ by name; but `parse_dataset_folder()` strips surrounding
    whitespace, so `"23-1-1-0559 "` and `"23-1-1-0559"` are two directories
    that mean one student. Training two LBPH labels for one person is a
    corrupt model, so the run refuses rather than picking one.
    """
    dataset_dir, _ = dataset

    _write_synthetic_images(dataset_dir / "23-1-1-0559", 5, seed=5)

    # ⚠️ Create the second directory *before* writing into it, and check what
    # the filesystem actually did.
    #
    # Windows strips a trailing space from a path component, so this mkdir
    # silently lands on the folder above and there is no duplicate to detect -
    # the situation under test cannot arise here at all. Skipped rather than
    # worked around: a test that quietly asserts something else on one platform
    # is worse than a test that says it did not run.
    with_space = dataset_dir / "23-1-1-0559 "
    with_space.mkdir(exist_ok=True)

    if not any(entry.name.endswith(" ") for entry in dataset_dir.iterdir()):
        pytest.skip("this filesystem normalises trailing spaces away")

    _write_synthetic_images(with_space, 5, seed=6)

    success, message = train_model.train_model()

    assert success is False
    assert "Duplicate" in message


def test_folder_not_matching_the_naming_convention_is_ignored(dataset):
    """
    A stray directory under dataset/ is skipped with a warning, not treated
    as a student and not allowed to fail the run.
    """
    dataset_dir, trainer_dir = dataset

    # ⚠️ Not "notavalidfoldername" any more - that is now a perfectly valid
    # student ID. A stray directory has to be something the ID allowlist
    # actually refuses, and spaces are the everyday case: an operator's
    # "old photos" folder sitting under dataset/.
    _write_synthetic_images(dataset_dir / "23-1-1-0559", 5, seed=7)
    _write_synthetic_images(dataset_dir / "old photos", 5, seed=8)

    success, _ = train_model.train_model()

    assert success is True

    labels = (trainer_dir / "labels.txt").read_text(encoding="utf-8")
    assert "23-1-1-0559" in labels
    assert "old photos" not in labels


# ---------------------------------------------------------------------------
# The CLI contract (D1)
#
# ⚠️ **`train_model.py` run from a shell used to exit 0 after a run that left a
# student out of the model**, because `train_model()` genuinely returns True -
# it is a real success for everyone it did train. A deploy that retrains on
# every version update and gates on the exit status therefore saw an
# unqualified pass while its roster silently shrank, and the student stayed in
# the database, on every roster and in every class list, absent only from the
# thing that does the recognising.
#
# The names were on stdout the whole time, as the last line of the run. Nothing
# automated reads prose.
#
# `BackgroundJob` unpacks exactly `(ok, message)`, so the CLI cannot be handed a
# structured list without changing a contract the web application depends on.
# It reads the message instead - which makes that message an interface, and
# these tests are what stop it drifting back into prose.
# ---------------------------------------------------------------------------


def test_the_skip_marker_is_the_one_the_cli_looks_for(dataset):
    """
    The message a partial run returns must contain `SKIPPED_MARKER` verbatim.

    ⚠️ This is the whole of the coupling. If someone rewords the message
    without moving the constant, the CLI stops recognising a partial retrain
    and goes back to exiting 0 - which is D1, restored, with every test still
    green. That failure would be invisible; this assertion makes it loud.
    """
    dataset_dir, _trainer_dir = dataset

    _write_synthetic_images(dataset_dir / "23-1-1-0559", 5, seed=1)
    _write_synthetic_images(dataset_dir / "23-1-1-0918", 2, seed=2)

    success, message = train_model.train_model()

    assert success is True
    assert train_model.SKIPPED_MARKER in message, (
        "the partial-success message no longer contains SKIPPED_MARKER, so "
        "train_model.py's __main__ can no longer tell a partial retrain from "
        "a complete one and will exit 0 on a shrinking roster (D1)"
    )


def test_a_complete_run_does_not_carry_the_skip_marker(dataset):
    """
    The other half: a full run must not trip the CLI's check.

    A marker that appeared in every message would fail every deploy, which
    gets the guard switched off - and a guard that is switched off is worse
    than one that was never added.
    """
    dataset_dir, _trainer_dir = dataset

    _write_synthetic_images(dataset_dir / "23-1-1-0559", 5, seed=1)
    _write_synthetic_images(dataset_dir / "23-1-1-0918", 5, seed=2)

    success, message = train_model.train_model()

    assert success is True
    assert train_model.SKIPPED_MARKER not in message
