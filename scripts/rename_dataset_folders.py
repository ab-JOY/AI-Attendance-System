"""
One-shot rename: `dataset/{student_id}_{name}` -> `dataset/{student_id}`.

todo.md §7.5. The folder used to carry the student's display name, which made
the name load-bearing for storage: renaming `Jose Cruz` to `Jose C. Cruz` in
the database left the images behind under the old path, and delete, edit and
recapture then operated on a directory with nothing at it. The code no longer
writes or reads that scheme; this moves the data that already exists.

    python scripts/rename_dataset_folders.py            # dry run, changes nothing
    python scripts/rename_dataset_folders.py --apply
    python scripts/rename_dataset_folders.py --dataset-dir path/to/copy --apply

**Run it against a copy first.** `dataset/` is the biometric data this whole
system is about, it is gitignored, and there is no second copy in the
repository to restore from.

Four properties, each deliberate:

* **Idempotent.** A folder already named for a valid student ID is reported and
  left alone, so running it twice is safe and so is running it on a machine
  that is already migrated.
* **Refuses on collision it cannot prove is safe.** If both `23-1-1-0559_Juan`
  and `23-1-1-0559` exist and their contents differ, the two are not obviously
  the same dataset and merging them is a decision this script has no basis for
  making. It stops with a non-zero exit and renames nothing.
* **Resolves a collision it *can* prove is safe.** If the two folders hold the
  same file names with byte-identical contents, the old one is a leftover copy
  carrying no data the new one lacks. Refusing there deadlocked the system:
  `train_model()` refuses every run while a `{id}_{name}` folder exists and
  tells the operator to run this script, and this script then refused too, so
  retraining could not be started from the UI at all. Such a folder is moved
  into `dataset/_migrated_duplicates/` rather than renamed.
* **Nothing is deleted, ever.** The only filesystem calls are `os.rename` and
  the `mkdir` for the quarantine directory. A quarantined folder keeps its
  name and its images; putting it back is one `mv`.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# The project uses a flat layout, so scripts/ needs the root importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.logging_config import configure_logging  # noqa: E402
from config.settings import settings  # noqa: E402
from train_model import (  # noqa: E402
    QUARANTINE_DIR_NAME,
    looks_like_a_pre_migration_folder,
    parse_dataset_folder,
)

logger = logging.getLogger("rename_dataset_folders")

# infra/dataset_store.py rotates an existing folder to `{name}.previous` while
# promoting a recapture and removes it afterwards. One left on disk is the
# residue of an interrupted promotion; it is not a dataset and must not become
# one by being renamed onto a live path.
PREVIOUS_SUFFIX = ".previous"

# Read in blocks rather than whole files. A dataset folder is 100 images and
# the comparison runs per pair; this keeps the peak resident size flat.
_COMPARE_BLOCK_BYTES = 64 * 1024


def _files_are_identical(left: Path, right: Path) -> bool:
    """True if two files hold exactly the same bytes."""
    if left.stat().st_size != right.stat().st_size:
        return False

    with left.open("rb") as left_handle, right.open("rb") as right_handle:
        while True:
            left_block = left_handle.read(_COMPARE_BLOCK_BYTES)
            right_block = right_handle.read(_COMPARE_BLOCK_BYTES)

            if left_block != right_block:
                return False

            if not left_block:
                return True


def folders_are_identical(left: Path, right: Path) -> bool:
    """
    True if two dataset folders hold the same file names, byte for byte.

    Deliberately strict, because the answer decides whether a folder of face
    images is treated as redundant. Same set of names, same bytes under every
    name, and no sub-directories on either side - anything else is a difference
    this script must not paper over. Compares contents rather than sizes: three
    folders in this project's own `dataset/` matched on total byte count, and
    that is not evidence of anything on its own.
    """
    try:
        left_entries = sorted(entry.name for entry in left.iterdir())
        right_entries = sorted(entry.name for entry in right.iterdir())

    except OSError:
        return False

    if left_entries != right_entries:
        return False

    for name in left_entries:
        left_path = left / name
        right_path = right / name

        if left_path.is_dir() or right_path.is_dir():
            return False

        try:
            if not _files_are_identical(left_path, right_path):
                return False

        except OSError:
            return False

    return True


def plan_renames(dataset_dir: Path):
    """
    Work out what would change. Returns `(renames, quarantines, skipped,
    refusals)`, each a list of pairs.

    Nothing here touches the filesystem beyond listing and reading it, so the
    dry run and the real run make exactly the same decisions - the only
    difference between them is whether the moves are carried out afterwards.
    """
    renames = []
    quarantines = []
    skipped = []
    refusals = []

    quarantine_dir = dataset_dir / QUARANTINE_DIR_NAME

    for entry in sorted(dataset_dir.iterdir()):
        if not entry.is_dir():
            continue

        if entry.name == QUARANTINE_DIR_NAME:
            skipped.append((entry.name, "the quarantine directory itself"))
            continue

        if entry.name.endswith(PREVIOUS_SUFFIX):
            skipped.append((entry.name, "leftover from an interrupted recapture"))
            continue

        if parse_dataset_folder(entry.name) is not None:
            skipped.append((entry.name, "already named for a student ID"))
            continue

        if not looks_like_a_pre_migration_folder(entry.name):
            skipped.append((entry.name, "not a recognisable dataset folder"))
            continue

        student_id = entry.name.split("_", 1)[0]
        target = entry.parent / student_id

        if target.exists():
            # The collision is only a decision when the two folders actually
            # differ. Identical contents mean the old folder holds nothing the
            # new one does not, so there is no merge to make - just a leftover
            # to get out of train_model()'s way.
            if folders_are_identical(entry, target):
                destination = quarantine_dir / entry.name

                if destination.exists():
                    refusals.append(
                        (
                            entry.name,
                            f"{QUARANTINE_DIR_NAME}/{entry.name} already exists; remove it by hand",
                        )
                    )
                    continue

                quarantines.append((entry, destination))
                continue

            refusals.append(
                (
                    entry.name,
                    f"{student_id} already exists and its contents differ; merge them by hand",
                )
            )
            continue

        renames.append((entry, target))

    return renames, quarantines, skipped, refusals


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform the renames. Without it, nothing is changed.",
    )
    parser.add_argument(
        "--dataset-dir",
        type=Path,
        default=settings.dataset_dir,
        help="the dataset directory to migrate (default: the configured one)",
    )

    args = parser.parse_args(argv)
    dataset_dir = args.dataset_dir.resolve()

    configure_logging()

    if not dataset_dir.is_dir():
        logger.error("No such dataset directory: %s", dataset_dir)
        return 2

    logger.info("Dataset directory: %s", dataset_dir)

    renames, quarantines, skipped, refusals = plan_renames(dataset_dir)

    for name, reason in skipped:
        logger.info("[skip]    %s - %s", name, reason)

    for name, reason in refusals:
        logger.error("[REFUSE]  %s - %s", name, reason)

    for source, target in renames:
        logger.info("[rename]  %s -> %s", source.name, target.name)

    for source, _ in quarantines:
        logger.info(
            "[quarantine] %s - identical to its target; moving to %s/",
            source.name,
            QUARANTINE_DIR_NAME,
        )

    # Refuse the whole run, not just the colliding folder. A partial migration
    # leaves `dataset/` in two naming schemes at once, and train_model.py then
    # refuses everything anyway - better to change nothing and say why.
    if refusals:
        logger.error(
            "%d collision(s). Nothing was renamed. Resolve them and run again.",
            len(refusals),
        )
        return 1

    if not renames and not quarantines:
        logger.info("Nothing to do - %d folder(s) already in place.", len(skipped))
        return 0

    if not args.apply:
        logger.warning(
            "DRY RUN: %d folder(s) would be renamed, %d quarantined. Re-run with --apply.",
            len(renames),
            len(quarantines),
        )
        return 0

    if quarantines:
        (dataset_dir / QUARANTINE_DIR_NAME).mkdir(exist_ok=True)

    for source, target in renames:
        os.rename(source, target)
        logger.info("Renamed %s -> %s", source.name, target.name)

    for source, target in quarantines:
        os.rename(source, target)
        logger.info(
            "Quarantined %s -> %s/%s",
            source.name,
            QUARANTINE_DIR_NAME,
            target.name,
        )

    logger.info(
        "Renamed %d folder(s), quarantined %d. Retrain now: python train_model.py",
        len(renames),
        len(quarantines),
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
