"""
Tests for the dataset folder migration script, and for the deadlock it used to
create.

The deadlock, observed in the UI: `train_model()` refuses every run while any
`{student_id}_{name}` folder exists and tells the operator to run
`scripts/rename_dataset_folders.py --apply`; that script then refused as well,
because the clean `{student_id}` folder already existed. Neither side could
move, so the Retrain button failed in 0.0s every time with no way forward.

The resolution is narrow on purpose. A collision is only decidable when the two
folders hold the same file names with byte-identical contents - then the old
folder holds nothing the new one lacks and is moved aside into
`dataset/_migrated_duplicates/`. Contents that differ are still a refusal,
because merging two different sets of a student's face images is a decision
this script has no basis for making.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import rename_dataset_folders as migration  # noqa: E402

from train_model import QUARANTINE_DIR_NAME  # noqa: E402


def _folder(root, name, files):
    """Create `root/name` holding `files` as {filename: bytes}."""
    folder = root / name
    folder.mkdir(parents=True)

    for filename, payload in files.items():
        (folder / filename).write_bytes(payload)

    return folder


IMAGES = {"0.jpg": b"\xff\xd8image-zero", "1.jpg": b"\xff\xd8image-one"}


# ==========================================================
# folders_are_identical
# ==========================================================


def test_identical_contents_are_identical(tmp_path):
    left = _folder(tmp_path, "left", IMAGES)
    right = _folder(tmp_path, "right", IMAGES)

    assert migration.folders_are_identical(left, right)


def test_same_names_but_different_bytes_are_not_identical(tmp_path):
    left = _folder(tmp_path, "left", IMAGES)
    right = _folder(tmp_path, "right", dict(IMAGES, **{"1.jpg": b"\xff\xd8other"}))

    assert not migration.folders_are_identical(left, right)


def test_same_total_size_but_different_bytes_is_not_identical(tmp_path):
    """
    The case that motivated a content comparison rather than a size one.

    This project's own dataset held three pairs whose total byte counts matched
    exactly. Equal size is not evidence, and a size-only check would quarantine
    a folder that still held data of its own.
    """
    left = _folder(tmp_path, "left", {"0.jpg": b"aaaa", "1.jpg": b"bbbb"})
    right = _folder(tmp_path, "right", {"0.jpg": b"bbbb", "1.jpg": b"aaaa"})

    assert not migration.folders_are_identical(left, right)


def test_extra_file_on_one_side_is_not_identical(tmp_path):
    left = _folder(tmp_path, "left", dict(IMAGES, **{"2.jpg": b"extra"}))
    right = _folder(tmp_path, "right", IMAGES)

    assert not migration.folders_are_identical(left, right)


def test_a_subdirectory_defeats_identity(tmp_path):
    left = _folder(tmp_path, "left", IMAGES)
    right = _folder(tmp_path, "right", IMAGES)
    (left / "nested").mkdir()
    (right / "nested").mkdir()

    assert not migration.folders_are_identical(left, right)


# ==========================================================
# plan_renames
# ==========================================================


def test_a_plain_pre_migration_folder_is_renamed(tmp_path):
    _folder(tmp_path, "23-1-1-0559_Juan Cruz", IMAGES)

    renames, quarantines, _, refusals = migration.plan_renames(tmp_path)

    assert [(s.name, t.name) for s, t in renames] == [("23-1-1-0559_Juan Cruz", "23-1-1-0559")]
    assert quarantines == []
    assert refusals == []


def test_an_identical_collision_is_quarantined_not_refused(tmp_path):
    """The deadlock, resolved. This is the case the live dataset was in."""
    _folder(tmp_path, "23-1-1-0559_Juan Cruz", IMAGES)
    _folder(tmp_path, "23-1-1-0559", IMAGES)

    renames, quarantines, _, refusals = migration.plan_renames(tmp_path)

    assert renames == []
    assert refusals == []
    assert [(s.name, t.parent.name, t.name) for s, t in quarantines] == [
        ("23-1-1-0559_Juan Cruz", QUARANTINE_DIR_NAME, "23-1-1-0559_Juan Cruz")
    ]


def test_a_differing_collision_is_still_refused(tmp_path):
    _folder(tmp_path, "23-1-1-0559_Juan Cruz", IMAGES)
    _folder(tmp_path, "23-1-1-0559", {"0.jpg": b"a different capture"})

    renames, quarantines, _, refusals = migration.plan_renames(tmp_path)

    assert renames == []
    assert quarantines == []
    assert len(refusals) == 1
    assert "contents differ" in refusals[0][1]


def test_an_occupied_quarantine_slot_is_refused(tmp_path):
    _folder(tmp_path, "23-1-1-0559_Juan Cruz", IMAGES)
    _folder(tmp_path, "23-1-1-0559", IMAGES)
    _folder(tmp_path, f"{QUARANTINE_DIR_NAME}/23-1-1-0559_Juan Cruz", IMAGES)

    _, quarantines, _, refusals = migration.plan_renames(tmp_path)

    assert quarantines == []
    assert len(refusals) == 1
    assert "already exists" in refusals[0][1]


def test_the_quarantine_directory_is_never_itself_migrated(tmp_path):
    _folder(tmp_path, f"{QUARANTINE_DIR_NAME}/23-1-1-0559_Juan Cruz", IMAGES)

    renames, quarantines, skipped, refusals = migration.plan_renames(tmp_path)

    assert (renames, quarantines, refusals) == ([], [], [])
    assert [name for name, _ in skipped] == [QUARANTINE_DIR_NAME]


def test_planning_changes_nothing_on_disk(tmp_path):
    _folder(tmp_path, "23-1-1-0559_Juan Cruz", IMAGES)
    _folder(tmp_path, "23-1-1-0559", IMAGES)

    before = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    migration.plan_renames(tmp_path)
    after = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))

    assert before == after


# ==========================================================
# main - the dry run / apply contract
# ==========================================================


def test_dry_run_quarantines_nothing(tmp_path):
    _folder(tmp_path, "23-1-1-0559_Juan Cruz", IMAGES)
    _folder(tmp_path, "23-1-1-0559", IMAGES)

    assert migration.main(["--dataset-dir", str(tmp_path)]) == 0
    assert (tmp_path / "23-1-1-0559_Juan Cruz").is_dir()
    assert not (tmp_path / QUARANTINE_DIR_NAME).exists()


def test_apply_moves_the_duplicate_and_keeps_every_image(tmp_path):
    _folder(tmp_path, "23-1-1-0559_Juan Cruz", IMAGES)
    _folder(tmp_path, "23-1-1-0559", IMAGES)

    assert migration.main(["--dataset-dir", str(tmp_path), "--apply"]) == 0

    quarantined = tmp_path / QUARANTINE_DIR_NAME / "23-1-1-0559_Juan Cruz"

    assert not (tmp_path / "23-1-1-0559_Juan Cruz").exists()
    assert quarantined.is_dir()
    # Nothing was deleted: the images are all still there, byte for byte.
    assert {p.name: p.read_bytes() for p in quarantined.iterdir()} == IMAGES
    assert {p.name: p.read_bytes() for p in (tmp_path / "23-1-1-0559").iterdir()} == IMAGES


def test_apply_is_idempotent(tmp_path):
    _folder(tmp_path, "23-1-1-0559_Juan Cruz", IMAGES)
    _folder(tmp_path, "23-1-1-0559", IMAGES)

    assert migration.main(["--dataset-dir", str(tmp_path), "--apply"]) == 0
    assert migration.main(["--dataset-dir", str(tmp_path), "--apply"]) == 0

    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
        [QUARANTINE_DIR_NAME, "23-1-1-0559"]
    )


def test_a_differing_collision_exits_non_zero_and_moves_nothing(tmp_path):
    _folder(tmp_path, "23-1-1-0559_Juan Cruz", IMAGES)
    _folder(tmp_path, "23-1-1-0559", {"0.jpg": b"a different capture"})

    assert migration.main(["--dataset-dir", str(tmp_path), "--apply"]) == 1
    assert (tmp_path / "23-1-1-0559_Juan Cruz").is_dir()
    assert not (tmp_path / QUARANTINE_DIR_NAME).exists()


# ==========================================================
# The other half of the deadlock
# ==========================================================


@pytest.mark.slow
def test_train_model_ignores_the_quarantine_directory(tmp_path, monkeypatch):
    """
    A quarantined folder must not re-trigger the refusal it was moved to end.

    train_model() refuses the whole run on a `{id}_{name}` folder, so if the
    quarantine directory were scanned as one - or its `{id}_{name}` children
    were - the migration would have achieved nothing.
    """
    pytest.importorskip("cv2")
    import train_model

    dataset_dir = tmp_path / "dataset"
    _folder(dataset_dir, f"{QUARANTINE_DIR_NAME}/23-1-1-0559_Juan Cruz", IMAGES)
    monkeypatch.setattr(train_model, "DATASET_DIR", str(dataset_dir))

    ok, message = train_model.train_model()

    # It fails for the honest reason - there is no student data to train on -
    # and specifically not for the pre-migration naming reason.
    assert not ok
    assert "old naming" not in message
    assert "No valid student dataset folders" in message
