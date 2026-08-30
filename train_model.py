import logging
import os
import sys

import cv2
import numpy as np

from config.settings import settings
from face_preprocessing import preprocess_for_lbph
from security.paths import UnsafeStudentPathError, validate_student_id

logger = logging.getLogger(__name__)


# =====================================================
# SETTINGS
#
# Paths come from config/settings.py (PO-2). They stay module-level strings
# rather than being read from `settings` at each use, because
# write_model_atomically() reads them as globals and the unit tests in
# tests/test_write_model_atomically.py monkeypatch them to a tmp_path.
# =====================================================

DATASET_DIR = str(settings.dataset_dir)

TRAINER_DIR = str(settings.trainer_dir)

TRAINER_FILE = str(settings.trainer_file)

LABELS_FILE = str(settings.labels_file)

# The new capture program normally creates 100 images.
# Requiring at least 70 prevents incomplete datasets from
# entering the trained model.
MIN_IMAGES_PER_STUDENT = 70

# =====================================================
# LBPH CONFIGURATION - single source of truth
#
# Imported by test_accuracy.py and test_heldout_accuracy.py so the
# evaluators always measure the configuration that actually ships.
#
# neighbors=8 (OpenCV's default) is deliberate. neighbors=12 was
# measured to fail in two independent ways:
#
#   1. It yields 262,144-dim histograms -> a 1.8 GB model that
#      cv::FileStorage writes but cannot read back
#      (persistence.cpp:1613 assertion failure).
#   2. It shifts LBPH distances into the 81-107 band while
#      recognize_face.RECOGNITION_THRESHOLD is 58.0, so every face is
#      rejected as unknown - 0/60 on a held-out split.
#
# neighbors=8 gives 16,384 dims, distances around 35, and 60/60 on the
# same split. See docs/walkthrough.md for the measured comparison.
# =====================================================

LBPH_PARAMS = {
    "radius": 2,
    "neighbors": 8,
    "grid_x": 8,
    "grid_y": 8
}

VALID_IMAGE_EXTENSIONS = (
    ".jpg",
    ".jpeg",
    ".png"
)

# Where scripts/rename_dataset_folders.py parks a pre-migration folder whose
# contents are byte-identical to the folder it would have been renamed onto.
# It lives inside dataset/ so the images stay under the one gitignored path,
# and it is named with a leading underscore so validate_student_id can never
# accept it as a student ID. Skipped by name below rather than left to fall
# through as an "invalid dataset folder", which would warn on every run about
# something this project put there on purpose.
QUARANTINE_DIR_NAME = "_migrated_duplicates"

# The phrase that marks a *partial* success in train_model()'s returned
# message: a model was written, and at least one student is missing from it.
#
# ⚠️ A constant rather than a literal in two places, because two places is
# exactly how this stops working. `train_model()` returns `(bool, str)` and
# `infra.jobs.BackgroundJob` unpacks precisely two values, so the CLI cannot
# be given a structured list of skipped students without changing a contract
# the web application depends on. It therefore has to read the message - and
# a message read by code is an interface, not prose. Reword it here and the
# CLI follows; reword it in only one of the two and
# `test_train_model_skips.py` fails rather than the deploy silently going
# back to exiting 0 on a shrinking roster (D1).
SKIPPED_MARKER = "Skipped (too few images, not recognisable): "


# =====================================================
# HELPERS
# =====================================================

def parse_dataset_folder(folder_name):
    """
    Return the student ID a dataset folder belongs to, or None.

    A folder is named for the student's ID and nothing else. It used to be
    `{student_id}_{student_name}` and this function split on the first
    underscore; todo.md §7.5 removed the name, because a display name that is
    load-bearing for storage means renaming a student orphans their images.

    Validation is `security.paths.validate_student_id` rather than a second
    rule written here - the module that decides what may become a path is the
    module that decides what a folder name may be.
    """
    try:
        return validate_student_id(folder_name)
    except UnsafeStudentPathError:
        return None


def looks_like_a_pre_migration_folder(folder_name):
    """
    True for a folder still named `{student_id}_{student_name}`.

    Worth telling apart from any other unusable name: it means this checkout
    is newer than its `dataset/`, and the fix is a documented one-line command
    rather than a mystery. Without this, a machine that pulled the change and
    never ran the rename would see "no valid student dataset folders" and have
    to work out why.
    """
    student_id, separator, remainder = folder_name.partition("_")

    return bool(
        separator
        and remainder.strip()
        and parse_dataset_folder(student_id) is not None
    )


def write_model_atomically(recognizer, label_dict):
    """
    Writes trainer.yml and labels.txt as an all-or-nothing pair.

    `label_dict` maps LBPH label -> student ID, and each line of labels.txt is
    `{label},{student_id}`. It used to be `{label},{student_id}_{name}`;
    the name is now read from the `students` table by
    `recognize_face.load_model_and_labels()`, so a rename cannot leave the
    overlay showing a stale one (todo.md §7.5).

    Both files are written to temporary paths first, so a failure
    part-way through can never destroy the model that is currently
    in use. The previous good pair is kept as .bak for one
    generation so a bad retrain can be reverted by hand.
    """

    temporary_trainer = TRAINER_FILE + ".tmp"
    temporary_labels = LABELS_FILE + ".tmp"

    backup_trainer = TRAINER_FILE + ".bak"
    backup_labels = LABELS_FILE + ".bak"

    # Clear leftovers from an earlier interrupted run.
    for leftover in (temporary_trainer, temporary_labels):
        if os.path.exists(leftover):
            os.remove(leftover)

    # 1. Write the new pair to temporary paths.
    #    The live model is still untouched at this point, so any
    #    failure here leaves the running system exactly as it was.
    try:
        recognizer.write(temporary_trainer)

        with open(
            temporary_labels,
            "w",
            encoding="utf-8"
        ) as labels_file:
            for label, student_id in label_dict.items():
                labels_file.write(
                    f"{label},{student_id}\n"
                )

    except Exception:
        for leftover in (temporary_trainer, temporary_labels):
            if os.path.exists(leftover):
                os.remove(leftover)
        raise

    # 2. Move the previous good pair aside. A pair is only worth
    #    keeping if both halves are present; an orphaned trainer.yml
    #    with no labels.txt is unusable and is simply overwritten.
    had_previous_model = (
        os.path.exists(TRAINER_FILE)
        and os.path.exists(LABELS_FILE)
    )

    if had_previous_model:
        for backup in (backup_trainer, backup_labels):
            if os.path.exists(backup):
                os.remove(backup)

        os.replace(TRAINER_FILE, backup_trainer)
        os.replace(LABELS_FILE, backup_labels)

    # 3. Promote the new pair, restoring the backup if this fails.
    try:
        os.replace(temporary_trainer, TRAINER_FILE)
        os.replace(temporary_labels, LABELS_FILE)

    except Exception:
        if had_previous_model:
            os.replace(backup_trainer, TRAINER_FILE)
            os.replace(backup_labels, LABELS_FILE)
        raise


# =====================================================
# TRAIN MODEL FUNCTION
# =====================================================

def train_model(report=None):
    """
    Retrain the LBPH model from `dataset/`. Returns `(ok, message)`.

    `report` is an optional callable taking one string, used to name the stage
    the run has reached. PE-5 moved this off the request thread, and the
    polling UI needs something true to display; stages are that, where a
    percentage would have to be invented. It defaults to None so every existing
    caller and every test is unaffected.
    """
    def stage(name):
        if report is not None:
            report(name)

    logger.info("OpenCV version: %s", cv2.__version__)

    if not hasattr(cv2, "face"):
        logger.error(
            "OpenCV Contrib is not installed. Install it with: "
            "python -m pip install opencv-contrib-python==4.10.0.84"
        )
        return False, "OpenCV Contrib is not installed."

    if not os.path.isdir(DATASET_DIR):
        logger.error("Dataset folder was not found: %s", DATASET_DIR)
        return False, f"Dataset folder was not found: {DATASET_DIR}"

    os.makedirs(
        TRAINER_DIR,
        exist_ok=True
    )

    folder_records = []
    student_id_to_folders = {}
    pre_migration_folders = []

    for folder_name in sorted(
        os.listdir(DATASET_DIR)
    ):
        folder_path = os.path.join(
            DATASET_DIR,
            folder_name
        )

        if not os.path.isdir(folder_path):
            continue

        if folder_name == QUARANTINE_DIR_NAME:
            logger.info(
                "Skipping the migration quarantine folder: %s", folder_name
            )
            continue

        student_id = parse_dataset_folder(
            folder_name
        )

        if student_id is None:
            if looks_like_a_pre_migration_folder(folder_name):
                pre_migration_folders.append(folder_name)
            else:
                logger.warning(
                    "Ignoring invalid dataset folder: %s", folder_name
                )
            continue

        folder_records.append(
            {
                "folder_name": folder_name,
                "folder_path": folder_path,
                "student_id": student_id
            }
        )

        student_id_to_folders.setdefault(
            student_id,
            []
        ).append(folder_name)

    # ⚠️ Named separately from any other unusable folder, and refused rather
    # than ignored. Training on whatever *did* parse would quietly produce a
    # model missing every student whose folder still carries a name - a model
    # that loads, runs, and cannot recognise them.
    if pre_migration_folders:
        logger.error(
            "%d dataset folder(s) still use the old {student_id}_{name} "
            "naming: %s",
            len(pre_migration_folders),
            ", ".join(pre_migration_folders),
        )
        logger.error(
            "Run: python scripts/rename_dataset_folders.py --apply"
        )
        return False, (
            f"{len(pre_migration_folders)} dataset folder(s) still use the "
            "old naming. Run scripts/rename_dataset_folders.py --apply, then "
            "train again."
        )

    duplicate_student_ids = {
        student_id: folders
        for student_id, folders
        in student_id_to_folders.items()
        if len(folders) > 1
    }

    if duplicate_student_ids:
        logger.error("Duplicate student dataset folders found")

        for student_id, folders in (
            duplicate_student_ids.items()
        ):
            logger.error(
                "  student %s appears in: %s",
                student_id,
                ", ".join(folders)
            )

        logger.error(
            "Keep only one correct dataset folder for each student ID, "
            "then run training again."
        )
        return False, "Duplicate student dataset folders found."

    if not folder_records:
        logger.error("No valid student dataset folders found")
        return False, "No valid student dataset folders found."

    faces = []
    labels = []
    label_dict = {}
    skipped_folders = []

    current_label = 0

    logger.info(
        "Training start: shared preprocessing (resize 200x200 + CLAHE), "
        "each dataset image processed exactly once"
    )

    stage(f"Reading images for {len(folder_records)} student(s)")

    for record in folder_records:
        folder_name = record[
            "folder_name"
        ]

        folder_path = record[
            "folder_path"
        ]

        logger.info("Processing: %s", folder_name)

        student_faces = []
        skipped_images = 0
        usable_count = 0

        for filename in sorted(
            os.listdir(folder_path)
        ):
            if not filename.lower().endswith(
                VALID_IMAGE_EXTENSIONS
            ):
                continue

            image_path = os.path.join(
                folder_path,
                filename
            )

            image = cv2.imread(
                image_path,
                cv2.IMREAD_GRAYSCALE
            )

            if image is None:
                logger.warning("Skipped unreadable image: %s", filename)
                skipped_images += 1
                continue

            try:
                processed_face = preprocess_for_lbph(
                    image
                )

            except Exception:
                logger.warning(
                    "Skipped image with preprocessing error: %s",
                    filename,
                    exc_info=True
                )
                skipped_images += 1
                continue

            if processed_face.shape != (200, 200):
                logger.warning(
                    "Skipped image with unexpected size: %s %s",
                    filename,
                    processed_face.shape
                )
                skipped_images += 1
                continue

            usable_count += 1
            student_faces.append(
                processed_face
            )

        logger.info(
            "  usable images: %d, skipped: %d",
            usable_count,
            skipped_images
        )

        # One incomplete folder must not block every other student.
        # Skip it, keep training the rest, and report it back to the
        # caller so the operator knows who still needs a recapture.
        if usable_count < MIN_IMAGES_PER_STUDENT:
            logger.warning(
                "Skipping %s - too few usable images (required %d, found "
                "%d). Recapture this student, then train again.",
                folder_name,
                MIN_IMAGES_PER_STUDENT,
                usable_count
            )

            skipped_folders.append(folder_name)
            continue

        # The student ID, not the folder name. They are the same string today
        # and this line is what keeps that an implementation detail: labels.txt
        # is a map from LBPH label to *student ID*, and the recognition side
        # looks the display name up in the database from it.
        label_dict[current_label] = record["student_id"]

        faces.extend(
            student_faces
        )

        labels.extend(
            [current_label] * len(student_faces)
        )

        current_label += 1

    if not faces:
        logger.error("No training images were loaded")

        if skipped_folders:
            return False, (
                "No student had enough usable images. Recapture: "
                + ", ".join(skipped_folders)
            )

        return False, "No training images were loaded."

    logger.info(
        "Dataset summary: %d person(s), %d image(s)",
        len(label_dict),
        len(faces)
    )

    recognizer = cv2.face.LBPHFaceRecognizer_create(
        **LBPH_PARAMS
    )

    logger.info("Training LBPH model with %s", LBPH_PARAMS)

    stage(f"Training on {len(faces)} image(s) from {len(label_dict)} student(s)")

    recognizer.train(
        faces,
        np.asarray(
            labels,
            dtype=np.int32
        )
    )

    logger.info("Saving model")

    stage("Saving model")

    try:
        write_model_atomically(
            recognizer,
            label_dict
        )

    except Exception as error:
        logger.exception(
            "Could not save the trained model. The previously trained "
            "model was left in place."
        )
        return False, f"Could not save the trained model: {error}"

    trainer_size = os.path.getsize(
        TRAINER_FILE
    )

    logger.info("Training completed successfully")
    logger.info("Trainer: %s (%s bytes)", TRAINER_FILE, f"{trainer_size:,}")
    logger.info("Labels : %s", LABELS_FILE)

    for label, student_id in (
        label_dict.items()
    ):
        logger.info("  label %s -> student %s", label, student_id)

    if skipped_folders:
        logger.warning(
            "These students are NOT in the trained model and will not be "
            "recognised: %s. Recapture their faces, then train again.",
            ", ".join(skipped_folders)
        )

    if skipped_folders:
        return True, (
            f"Training completed for {len(label_dict)} student(s). "
            + SKIPPED_MARKER
            + ", ".join(skipped_folders)
        )

    return True, "Training completed successfully."


if __name__ == "__main__":
    # Entry point, so this process owns logging configuration. When
    # train_model() is called from app.py instead, app.py has already
    # configured logging and this block never runs.
    #
    # ⚠️ **Nothing in the web application reaches this block.** The Train
    # Model button and the post-enrolment retrain both go through
    # services.training.training_job, which calls train_model() directly. This
    # is the CLI contract only - which is precisely who needed fixing, because
    # the CLI is what a deploy runs.
    from config.exit_codes import EXIT_FAILURE, EXIT_INCOMPLETE, EXIT_SUCCESS
    from config.logging_config import configure_logging

    configure_logging()

    # ⚠️ **D1: `msg` used to be unpacked and then dropped on the floor**, and
    # the exit status was `0 if success else 1`. `train_model()` returns True
    # for a run that trained four students and skipped a fifth, so a deploy
    # that retrains on every version update and gates on the exit status saw
    # an unqualified pass while its roster silently shrank. The skipped names
    # were on stdout - as a WARNING, the last line of the run - but nothing
    # automated reads prose, and the one machine-readable signal said "fine".
    #
    # This is FS-12 at a second entry point; see config/exit_codes.py.
    success, message = train_model()

    if not success:
        logger.error("%s", message)
        sys.exit(EXIT_FAILURE)

    incomplete = SKIPPED_MARKER in message

    if incomplete and "--allow-incomplete" not in sys.argv:
        logger.error("%s", message)
        logger.error(
            "Exiting %d: students are missing from the model. Recapture them "
            "and train again, or pass --allow-incomplete to accept this "
            "roster.",
            EXIT_INCOMPLETE,
        )
        sys.exit(EXIT_INCOMPLETE)

    logger.info("%s", message)
    sys.exit(EXIT_SUCCESS)
