import os
import sys
import cv2
import numpy as np

from face_preprocessing import preprocess_for_lbph


# =====================================================
# SETTINGS
# =====================================================

BASE_DIR = os.path.dirname(
    os.path.abspath(__file__)
)

DATASET_DIR = os.path.join(
    BASE_DIR,
    "dataset"
)

TRAINER_DIR = os.path.join(
    BASE_DIR,
    "trainer"
)

TRAINER_FILE = os.path.join(
    TRAINER_DIR,
    "trainer.yml"
)

LABELS_FILE = os.path.join(
    TRAINER_DIR,
    "labels.txt"
)

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


# =====================================================
# HELPERS
# =====================================================

def parse_dataset_folder(folder_name):
    """
    Expected format:
        student_id_student_name
    """

    parts = folder_name.split(
        "_",
        1
    )

    if len(parts) != 2:
        return None

    student_id = parts[0].strip()
    student_name = parts[1].strip()

    if not student_id or not student_name:
        return None

    return student_id, student_name


def write_model_atomically(recognizer, label_dict):
    """
    Writes trainer.yml and labels.txt as an all-or-nothing pair.

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
            for label, folder_name in label_dict.items():
                labels_file.write(
                    f"{label},{folder_name}\n"
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

def train_model():
    print("OpenCV Version:", cv2.__version__)

    if not hasattr(cv2, "face"):
        print("ERROR: OpenCV Contrib is not installed.")
        print(
            "Install it using: python -m pip install "
            "opencv-contrib-python==4.10.0.84"
        )
        return False, "OpenCV Contrib is not installed."

    if not os.path.isdir(DATASET_DIR):
        print("ERROR: Dataset folder was not found:")
        print(DATASET_DIR)
        return False, f"Dataset folder was not found: {DATASET_DIR}"

    os.makedirs(
        TRAINER_DIR,
        exist_ok=True
    )

    folder_records = []
    student_id_to_folders = {}

    for folder_name in sorted(
        os.listdir(DATASET_DIR)
    ):
        folder_path = os.path.join(
            DATASET_DIR,
            folder_name
        )

        if not os.path.isdir(folder_path):
            continue

        parsed = parse_dataset_folder(
            folder_name
        )

        if parsed is None:
            print(
                "WARNING: Ignoring invalid dataset folder:",
                folder_name
            )
            continue

        student_id, student_name = parsed

        folder_records.append(
            {
                "folder_name": folder_name,
                "folder_path": folder_path,
                "student_id": student_id,
                "student_name": student_name
            }
        )

        student_id_to_folders.setdefault(
            student_id,
            []
        ).append(folder_name)

    duplicate_student_ids = {
        student_id: folders
        for student_id, folders
        in student_id_to_folders.items()
        if len(folders) > 1
    }

    if duplicate_student_ids:
        print("=" * 60)
        print("ERROR: DUPLICATE STUDENT DATASET FOLDERS")
        print("=" * 60)

        for student_id, folders in (
            duplicate_student_ids.items()
        ):
            print("Student ID:", student_id)

            for folder_name in folders:
                print("  -", folder_name)

        print()
        print(
            "Keep only one correct dataset folder for each "
            "student ID, then run training again."
        )
        return False, "Duplicate student dataset folders found."

    if not folder_records:
        print("ERROR: No valid student dataset folders found.")
        return False, "No valid student dataset folders found."

    faces = []
    labels = []
    label_dict = {}
    skipped_folders = []

    current_label = 0

    print("=" * 60)
    print("AI ATTENDANCE - TRAIN MODEL")
    print("=" * 60)
    print(
        "Shared preprocessing: resize to 200x200 + CLAHE"
    )
    print(
        "Each dataset image will be processed exactly once."
    )

    for record in folder_records:
        folder_name = record[
            "folder_name"
        ]

        folder_path = record[
            "folder_path"
        ]

        print("\nProcessing:", folder_name)

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
                print("Skipped unreadable image:", filename)
                skipped_images += 1
                continue

            try:
                processed_face = preprocess_for_lbph(
                    image
                )

            except Exception as error:
                print(
                    "Skipped preprocessing error:",
                    filename,
                    "-",
                    error
                )
                skipped_images += 1
                continue

            if processed_face.shape != (200, 200):
                print(
                    "Skipped unexpected image size:",
                    filename,
                    processed_face.shape
                )
                skipped_images += 1
                continue

            usable_count += 1
            student_faces.append(
                processed_face
            )

        print("Usable Images :", usable_count)
        print("Skipped Images:", skipped_images)

        # One incomplete folder must not block every other student.
        # Skip it, keep training the rest, and report it back to the
        # caller so the operator knows who still needs a recapture.
        if usable_count < MIN_IMAGES_PER_STUDENT:
            print(
                "WARNING: Skipping this student - too few usable images."
            )
            print(
                f"Required: {MIN_IMAGES_PER_STUDENT} | "
                f"Found: {usable_count}"
            )
            print(
                "Recapture this student, then train again."
            )

            skipped_folders.append(folder_name)
            continue

        label_dict[current_label] = folder_name

        faces.extend(
            student_faces
        )

        labels.extend(
            [current_label] * len(student_faces)
        )

        current_label += 1

    if not faces:
        print("ERROR: No training images were loaded.")

        if skipped_folders:
            return False, (
                "No student had enough usable images. Recapture: "
                + ", ".join(skipped_folders)
            )

        return False, "No training images were loaded."

    print("\n" + "=" * 60)
    print("DATASET SUMMARY")
    print("=" * 60)
    print("Persons:", len(label_dict))
    print("Images :", len(faces))

    recognizer = cv2.face.LBPHFaceRecognizer_create(
        **LBPH_PARAMS
    )

    print("\nTraining LBPH model...")

    recognizer.train(
        faces,
        np.asarray(
            labels,
            dtype=np.int32
        )
    )

    print("Saving model...")

    try:
        write_model_atomically(
            recognizer,
            label_dict
        )

    except Exception as error:
        print("ERROR: Could not save the trained model:", error)
        print("The previously trained model was left in place.")
        return False, f"Could not save the trained model: {error}"

    trainer_size = os.path.getsize(
        TRAINER_FILE
    )

    print("\nTraining completed successfully.")
    print("Trainer:", TRAINER_FILE)
    print("Labels :", LABELS_FILE)
    print(
        f"Trainer Size: {trainer_size:,} bytes"
    )

    print("\nAssigned Labels")

    for label, folder_name in (
        label_dict.items()
    ):
        print(
            f"{label} -> {folder_name}"
        )

    if skipped_folders:
        print()
        print(
            "WARNING: These students are NOT in the trained model "
            "and will not be recognised:"
        )

        for folder_name in skipped_folders:
            print("  -", folder_name)

        print("Recapture their faces, then train again.")

    print("=" * 60)

    if skipped_folders:
        return True, (
            f"Training completed for {len(label_dict)} student(s). "
            f"Skipped (too few images, not recognisable): "
            + ", ".join(skipped_folders)
        )

    return True, "Training completed successfully."


if __name__ == "__main__":
    success, msg = train_model()
    if success:
        sys.exit(0)
    else:
        sys.exit(1)