import hashlib
import logging
import os
import shutil
import sys
import threading
import time

import cv2
import numpy as np

import face_preprocessing
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

# One `{student_id}.yml` per student (todo.md §19). It replaced trainer.yml,
# a single model that capped the system near 31 students (PE-2).
MODELS_DIR = str(settings.student_models_dir)

LABELS_FILE = str(settings.labels_file)

# Held while the live students/ + labels.txt pair is renamed, and by
# recognize_face.load_model_and_labels() while it reads them. In-process
# only: training runs on a thread of the web server (services/training.py).
MODEL_FILES_LOCK = threading.Lock()

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


def write_model_atomically(write_models):
    """
    Writes students/ and labels.txt as an all-or-nothing pair.

    `write_models(directory)` writes one `{student_id}.yml` per student into
    `directory` and returns `label_dict`, which maps LBPH label -> student ID;
    each line of labels.txt is `{label},{student_id}`. A callback rather than a
    dict of recognizers so that only one student's model is ever in memory
    (todo.md §19): at 2,000 students the dict would be gigabytes. An empty
    `label_dict` writes nothing and leaves the live model as it was.

    Both halves are written to temporary paths first, so a failure part-way
    through can never destroy the model that is currently in use. The
    previous good pair is kept as .bak for one generation so a bad retrain can
    be reverted by hand. Returns `label_dict`.
    """

    temporary_models = MODELS_DIR + ".tmp"
    temporary_labels = LABELS_FILE + ".tmp"

    backup_models = MODELS_DIR + ".bak"
    backup_labels = LABELS_FILE + ".bak"

    def remove_temporaries():
        if os.path.isdir(temporary_models):
            shutil.rmtree(temporary_models)
        if os.path.exists(temporary_labels):
            os.remove(temporary_labels)

    # Clear leftovers from an earlier interrupted run.
    remove_temporaries()
    os.makedirs(temporary_models)

    # 1. Write the new pair to temporary paths.
    #    The live model is still untouched at this point, so any
    #    failure here leaves the running system exactly as it was.
    try:
        label_dict = write_models(temporary_models)

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
        remove_temporaries()
        raise

    if not label_dict:
        remove_temporaries()
        return label_dict

    # Steps 2 and 3 rename the live pair, which Windows refuses while this
    # process is reading it (lessons.md L36). The attendance model is now
    # also read in the background after every retrain, so the renames wait
    # for a read in progress instead of failing the retrain.
    with MODEL_FILES_LOCK:
        # 2. Move the previous good pair aside. A pair is only worth
        #    keeping if both halves are present; an orphaned students/
        #    with no labels.txt is unusable and is simply replaced.
        had_previous_model = (
            os.path.isdir(MODELS_DIR)
            and os.path.exists(LABELS_FILE)
        )

        if had_previous_model:
            if os.path.isdir(backup_models):
                shutil.rmtree(backup_models)
            if os.path.exists(backup_labels):
                os.remove(backup_labels)

            os.replace(MODELS_DIR, backup_models)
            os.replace(LABELS_FILE, backup_labels)

        elif os.path.isdir(MODELS_DIR):
            # os.replace() overwrites a file but not a directory.
            shutil.rmtree(MODELS_DIR)

        # 3. Promote the new pair, restoring the backup if this fails.
        try:
            os.replace(temporary_models, MODELS_DIR)
            os.replace(temporary_labels, LABELS_FILE)

        except Exception:
            if had_previous_model:
                if os.path.isdir(MODELS_DIR):
                    shutil.rmtree(MODELS_DIR)
                os.replace(backup_models, MODELS_DIR)
                os.replace(backup_labels, LABELS_FILE)
            raise

    return label_dict


# =====================================================
# UNCHANGED STUDENTS ARE NOT RETRAINED (todo.md §19)
#
# Every enrolment used to retrain every student: 56 s at 17 students, which
# grows with the school. A student's model is now reused when nothing it was
# built from has changed - their images, and the training configuration.
# =====================================================

# See `started_at` in train_model().
REUSE_MARGIN_SECONDS = 2.0

# Written beside the models. Its leading underscore keeps it from ever being
# read as a student's model: validate_student_id refuses one.
TRAINING_FINGERPRINT_FILE = "_training.txt"


def training_fingerprint():
    """
    Everything a model depends on besides its images.

    The OpenCV version, LBPH_PARAMS and the preprocessing source. Any change
    retrains everyone, so a model is never reused under settings it was not
    built with - which is how L2 broke recognition, silently.
    """
    with open(face_preprocessing.__file__, "rb") as source:
        preprocessing = hashlib.sha256(source.read()).hexdigest()

    return (
        f"opencv {cv2.__version__}\n"
        f"lbph {sorted(LBPH_PARAMS.items())}\n"
        f"preprocessing {preprocessing}\n"
    )


def live_training_fingerprint():
    try:
        with open(
            os.path.join(MODELS_DIR, TRAINING_FINGERPRINT_FILE),
            encoding="utf-8",
        ) as handle:
            return handle.read()
    except OSError:
        return None


def newest_change(folder_path):
    """
    The latest mtime of the folder and anything in it.

    The folder's own mtime is what catches a *deleted* image, which leaves no
    newer file behind.
    """
    newest = os.path.getmtime(folder_path)

    with os.scandir(folder_path) as entries:
        for entry in entries:
            newest = max(newest, entry.stat().st_mtime)

    return newest


def reuse_unchanged_model(student_id, folder_path, directory):
    """
    Put the live model of an unchanged student into `directory`. True if done.

    A hard link, so nothing is copied and the live file is untouched; a copy
    where links are not available. The model keeps its mtime either way, and
    that mtime is the training start time of the run that built it.
    """
    live_model = os.path.join(MODELS_DIR, f"{student_id}.yml")

    try:
        model_time = os.path.getmtime(live_model)
    except OSError:
        return False

    if newest_change(folder_path) >= model_time:
        return False

    target = os.path.join(directory, f"{student_id}.yml")

    try:
        os.link(live_model, target)
    except OSError:
        shutil.copy2(live_model, target)

    return True


# =====================================================
# TRAIN MODEL FUNCTION
# =====================================================

def train_model(report=None, full=False):
    """
    Retrain the LBPH model from `dataset/`. Returns `(ok, message)`.

    `report` is an optional callable taking one string, used to name the stage
    the run has reached. PE-5 moved this off the request thread, and the
    polling UI needs something true to display; stages are that, where a
    percentage would have to be invented. It defaults to None so every existing
    caller and every test is unaffected.

    Only students whose images changed are trained; the rest keep their model
    (`reuse_unchanged_model`). `full` trains everyone regardless.
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

    skipped_folders = []
    image_count = 0
    reused = []

    # The models built here are stamped with this time, not with when they
    # were written: an image that lands while training runs is then newer
    # than its student's model, and the next run trains them again.
    #
    # ⚠️ Minus a margin. Measured 2026-10-28: an image written 2 ms before
    # training started carried an mtime *equal* to time.time() at the start,
    # so "before" and "at" cannot be told apart near the boundary. Anything
    # inside the margin is treated as changed and retrained once more - extra
    # seconds, never a skipped image.
    started_at = time.time() - REUSE_MARGIN_SECONDS

    fingerprint = training_fingerprint()
    reuse_allowed = not full and live_training_fingerprint() == fingerprint

    if not full and not reuse_allowed:
        logger.info(
            "Training configuration changed (or first run): training every student"
        )

    logger.info(
        "Training start: shared preprocessing (resize 200x200 + CLAHE), "
        "each dataset image processed exactly once"
    )

    logger.info("Training one LBPH model per student with %s", LBPH_PARAMS)

    def write_models(directory):
        """
        Train and write each student's model in turn (todo.md §19).

        One student's images and model are in memory at a time, where the
        single model needed every image of every student at once.
        """
        nonlocal image_count

        label_dict = {}

        for number, record in enumerate(folder_records, start=1):
            folder_name = record["folder_name"]
            folder_path = record["folder_path"]

            stage(f"Training student {number} of {len(folder_records)}")

            if reuse_allowed and reuse_unchanged_model(
                record["student_id"], folder_path, directory
            ):
                logger.info("Unchanged, model reused: %s", folder_name)
                label_dict[len(label_dict)] = record["student_id"]
                reused.append(folder_name)
                continue

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

            # The student ID, not the folder name. They are the same string
            # today and this line is what keeps that an implementation detail:
            # labels.txt is a map from LBPH label to *student ID*, and the
            # recognition side looks the display name up in the database
            # from it. Labels follow folder order, as they did in the single
            # model, which is what keeps tie-breaking identical
            # (vision/student_models.py).
            label = len(label_dict)
            label_dict[label] = record["student_id"]

            recognizer = cv2.face.LBPHFaceRecognizer_create(
                **LBPH_PARAMS
            )

            recognizer.train(
                student_faces,
                np.full(len(student_faces), label, dtype=np.int32)
            )

            model_path = os.path.join(
                directory, f"{record['student_id']}.yml"
            )
            recognizer.write(model_path)
            os.utime(model_path, (started_at, started_at))

            image_count += len(student_faces)

        with open(
            os.path.join(directory, TRAINING_FINGERPRINT_FILE),
            "w",
            encoding="utf-8",
        ) as handle:
            handle.write(fingerprint)

        return label_dict

    try:
        label_dict = write_model_atomically(write_models)

    except Exception as error:
        logger.exception(
            "Could not save the trained model. The previously trained "
            "model was left in place."
        )
        return False, f"Could not save the trained model: {error}"

    if not label_dict:
        logger.error("No training images were loaded")

        if skipped_folders:
            return False, (
                "No student had enough usable images. Recapture: "
                + ", ".join(skipped_folders)
            )

        return False, "No training images were loaded."

    logger.info(
        "Dataset summary: %d person(s): %d trained from %d image(s), %d unchanged",
        len(label_dict),
        len(label_dict) - len(reused),
        image_count,
        len(reused)
    )

    models_size = sum(
        os.path.getsize(os.path.join(MODELS_DIR, name))
        for name in os.listdir(MODELS_DIR)
    )

    logger.info("Training completed successfully")
    logger.info(
        "Models : %s (%d file(s), %s bytes)",
        MODELS_DIR,
        len(label_dict),
        f"{models_size:,}"
    )
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
    success, message = train_model(full="--full" in sys.argv)

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
