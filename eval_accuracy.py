"""
In-sample accuracy harness for the AI Attendance System.

Renamed from test_accuracy.py in Phase 1: this is a human-facing measurement
script, not a unit test, and the old name made pytest try to import it at
collection time. Run it directly:

    python eval_accuracy.py

WHAT THIS NUMBER MEANS - read before quoting it.
Every test image here is also a training image, and LBPH stores one histogram
per training sample, so each query matches its own stored histogram exactly.
A result of 100% at average distance 0.00 demonstrates that the
train -> save -> load -> predict pipeline is wired up correctly. It is not an
accuracy measurement. See docs/walkthrough.md sections 4 and 5, and
tasks/todo.md section 3.

Uses SQLite so it runs offline without a MySQL connection.
"""

import os
import sqlite3

import cv2

from config.settings import settings
from face_preprocessing import preprocess_for_lbph
from train_model import LBPH_PARAMS, train_model

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATASET_DIR = str(settings.dataset_dir)
TRAINER_FILE = str(settings.trainer_file)
LABELS_FILE = str(settings.labels_file)
DB_FILE = os.path.join(BASE_DIR, "test_attendance.db")

def setup_sqlite_db():
    """Initializes a lightweight SQLite test database mirroring the attendance schema."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS students (
        student_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        college_department TEXT,
        program TEXT,
        year_level INTEGER,
        section TEXT
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS attendance (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id TEXT NOT NULL,
        student_name TEXT NOT NULL,
        subject_code TEXT NOT NULL,
        attendance_date TEXT NOT NULL,
        time_in TEXT NOT NULL,
        status TEXT NOT NULL,
        FOREIGN KEY (student_id) REFERENCES students(student_id)
    );
    """)

    conn.commit()
    return conn

def test_model_training():
    """Runs model training and verifies output files."""
    print("=" * 60)
    print("1. TESTING MODEL TRAINING PIPELINE")
    print("=" * 60)

    success, message = train_model()
    print(f"Training Success: {success}")
    print(f"Message         : {message}")

    if not success:
        print("[ERROR] Training failed!")
        return False

    if not os.path.exists(TRAINER_FILE) or not os.path.exists(LABELS_FILE):
        print("[ERROR] Trainer file or labels file missing after training!")
        return False

    print("[OK] Model training verified successfully.")
    return True

def evaluate_recognition_accuracy():
    """Evaluates LBPH model accuracy against dataset images."""
    print("\n" + "=" * 60)
    print("2. EVALUATING LBPH RECOGNITION ACCURACY")
    print("=" * 60)

    if not os.path.exists(TRAINER_FILE) or not os.path.exists(LABELS_FILE):
        print("[ERROR] Cannot evaluate: trainer.yml or labels.txt not found.")
        return

    # Parameters come from train_model.py so this can never drift from
    # the shipped configuration. read() also restores them from the file.
    recognizer = cv2.face.LBPHFaceRecognizer_create(
        **LBPH_PARAMS
    )
    recognizer.read(TRAINER_FILE)

    # Load labels
    # Two separate maps on purpose: label_map holds student IDs (used to
    # score a prediction) and label_folders holds dataset folder names
    # (used to locate the images). Collapsing them into one dict made this
    # evaluator silently score zero images, because the student ID was
    # being joined onto DATASET_DIR as if it were a folder name.
    label_map = {}
    label_folders = {}
    with open(LABELS_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split(",", 1)
            label = int(parts[0])
            folder_name = parts[1]
            student_id = folder_name.split("_", 1)[0]
            label_map[label] = student_id
            label_folders[label] = folder_name

    print(f"Loaded {len(label_map)} identities from labels.txt.")

    total_images = 0
    correct_matches = 0
    incorrect_matches = 0
    unknowns = 0
    confidence_scores = []

    # Imported from configuration rather than restated, so the evaluator can
    # never measure a threshold that production does not use (lessons.md L3).
    RECOGNITION_THRESHOLD = settings.recognition_threshold

    for label, folder_name in sorted(label_folders.items()):
        folder_path = os.path.join(DATASET_DIR, folder_name)
        if not os.path.isdir(folder_path):
            print(f"[WARN] dataset folder missing, skipping: {folder_name}")
            continue

        expected_student_id = label_map[label]
        student_correct = 0
        student_total = 0

        for filename in sorted(os.listdir(folder_path)):
            if not filename.lower().endswith((".jpg", ".jpeg", ".png")):
                continue

            img_path = os.path.join(folder_path, filename)
            img = cv2.imread(img_path, cv2.IMREAD_GRAYSCALE)
            if img is None:
                continue

            processed = preprocess_for_lbph(img)
            pred_label, distance = recognizer.predict(processed)

            total_images += 1
            student_total += 1
            confidence_scores.append(distance)

            if distance <= RECOGNITION_THRESHOLD and pred_label in label_map:
                pred_student_id = label_map[pred_label]
                if pred_student_id == expected_student_id:
                    correct_matches += 1
                    student_correct += 1
                else:
                    incorrect_matches += 1
            else:
                unknowns += 1

        accuracy_pct = (student_correct / student_total * 100) if student_total > 0 else 0
        print(
            f"Student: {folder_name:40s} | "
            f"Accuracy: {accuracy_pct:6.2f}% ({student_correct}/{student_total})"
        )

    # Scoring zero images is a harness failure, not a 0% result. Without
    # this guard the summary below prints a tidy table of zeros and looks
    # like a finished evaluation - which is exactly how a folder-name bug
    # went unnoticed here.
    if total_images == 0:
        print("\n[ERROR] Evaluated 0 images - the harness is broken, not the model.")
        print("Check that labels.txt folder names match the dataset/ directories.")
        return False

    overall_accuracy = (correct_matches / total_images * 100) if total_images > 0 else 0
    avg_confidence = (sum(confidence_scores) / len(confidence_scores)) if confidence_scores else 0

    print("\n" + "=" * 60)
    print("ACCURACY EVALUATION SUMMARY")
    print("=" * 60)
    print(f"Total Test Images      : {total_images}")
    print(f"Correct Identifications: {correct_matches}")
    print(f"Incorrect / Mismatches : {incorrect_matches}")
    print(f"Unknown (Above Thresh) : {unknowns}")
    print(f"Average LBPH Distance  : {avg_confidence:.2f}")
    print(f"Overall Accuracy       : {overall_accuracy:.2f}%")
    print("=" * 60)

def main():
    conn = setup_sqlite_db()
    print("[OK] SQLite test DB initialized at test_attendance.db")

    if test_model_training():
        evaluate_recognition_accuracy()

    conn.close()

if __name__ == "__main__":
    # This harness calls train_model(), which reports its progress through
    # logging. Without configuring handlers here that output is discarded and
    # a retrain looks like a silent hang - so this script is an entry point
    # and configures logging like one. Its own results stay on print(),
    # because they are a report rather than a log.
    from config.logging_config import configure_logging

    configure_logging()

    main()
