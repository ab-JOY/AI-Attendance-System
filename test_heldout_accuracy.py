"""
Out-of-Sample (80/20 Train-Test Split) Accuracy Evaluator for AI Attendance System.

Methodology:
1. Split student images into 80% Training set and 20% Held-Out Test set.
2. Train LBPH recognizer exclusively on the 80% Training set, using the
   SAME configuration as train_model.py so this measures the system that
   actually runs in production.
3. Evaluate recognizer ONLY on the 20% Held-Out Test set (images NEVER seen
   during training).

KNOWN LIMITATION - read before quoting these numbers:
    Every image for a student comes from ONE capture session, so the
    "held-out" 20% are near-duplicate video frames of the training 80%.
    This is a same-session split, not a genuine generalisation estimate.
    There is also no impostor set, so the false-acceptance rate - the
    security-critical metric - is NOT measured here.
    A defensible evaluation needs multi-session capture plus non-enrolled
    faces. See tasks/todo.md section 3.
"""

import os
import sys
import random
import cv2
import numpy as np

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATASET_DIR = os.path.join(BASE_DIR, "dataset")

from face_preprocessing import preprocess_for_lbph
from train_model import parse_dataset_folder, LBPH_PARAMS

def run_heldout_evaluation(split_ratio=0.80, seed=42):
    random.seed(seed)
    
    print("=" * 60)
    print(f"STRICT HELD-OUT EVALUATION (Train: {int(split_ratio*100)}% | Test: {int((1-split_ratio)*100)}%)")
    print("=" * 60)
    
    # 1. Discover dataset folders
    folder_records = []
    for folder_name in sorted(os.listdir(DATASET_DIR)):
        folder_path = os.path.join(DATASET_DIR, folder_name)
        if not os.path.isdir(folder_path):
            continue
        parsed = parse_dataset_folder(folder_name)
        if parsed:
            student_id, student_name = parsed
            folder_records.append({
                "folder_name": folder_name,
                "folder_path": folder_path,
                "student_id": student_id,
                "student_name": student_name
            })

    train_faces = []
    train_labels = []
    
    test_set = [] # list of (image_path, expected_label, student_id)
    label_dict = {}
    
    current_label = 0

    for record in folder_records:
        folder_path = record["folder_path"]
        folder_name = record["folder_name"]
        student_id = record["student_id"]
        
        all_images = [
            f for f in sorted(os.listdir(folder_path))
            if f.lower().endswith((".jpg", ".jpeg", ".png"))
        ]
        
        # Shuffle deterministically and split
        random.shuffle(all_images)
        split_idx = int(len(all_images) * split_ratio)
        train_img_names = all_images[:split_idx]
        test_img_names = all_images[split_idx:]
        
        label_dict[current_label] = folder_name
        
        # Process Training Images
        # No augmentation - train_model.py does not augment either, and
        # this evaluator must mirror production or it measures a system
        # that does not exist.
        for fname in train_img_names:
            img_path = os.path.join(folder_path, fname)
            img = cv2.imread(img_path, cv2.IMREAD_GRAYSCALE)
            if img is None:
                continue
            proc = preprocess_for_lbph(img)

            train_faces.append(proc)
            train_labels.append(current_label)

        # Collect Held-Out Test Images
        for fname in test_img_names:
            img_path = os.path.join(folder_path, fname)
            test_set.append((img_path, current_label, student_id, folder_name))

        print(f"Student: {folder_name:40s} | Train: {len(train_img_names)} | Test: {len(test_img_names)}")
        current_label += 1

    # 2. Train Model on 80% Split ONLY
    print("\nTraining LBPH Model on 80% Split...")
    print(f"LBPH configuration (from train_model.py): {LBPH_PARAMS}")
    recognizer = cv2.face.LBPHFaceRecognizer_create(
        **LBPH_PARAMS
    )
    recognizer.train(train_faces, np.asarray(train_labels, dtype=np.int32))
    print("Training complete.")

    # 3. Evaluate ONLY on Held-Out Test Images (20% Split)
    print("\n" + "=" * 60)
    print("EVALUATING ON HELD-OUT UNSEEN TEST IMAGES")
    print("=" * 60)
    
    correct = 0
    incorrect = 0
    unknown = 0
    distances = []
    
    RECOGNITION_THRESHOLD = 58.0
    
    per_student_stats = {lbl: {"correct": 0, "total": 0} for lbl in label_dict}

    for img_path, exp_label, student_id, folder_name in test_set:
        img = cv2.imread(img_path, cv2.IMREAD_GRAYSCALE)
        if img is None:
            continue
            
        proc = preprocess_for_lbph(img)
        pred_label, distance = recognizer.predict(proc)
        
        per_student_stats[exp_label]["total"] += 1
        distances.append(distance)
        
        if distance <= RECOGNITION_THRESHOLD and pred_label in label_dict:
            if pred_label == exp_label:
                correct += 1
                per_student_stats[exp_label]["correct"] += 1
            else:
                incorrect += 1
        else:
            unknown += 1

    total_test = len(test_set)
    
    print("\nPer-Student Held-Out Accuracy:")
    for lbl, folder_name in label_dict.items():
        st = per_student_stats[lbl]
        pct = (st["correct"] / st["total"] * 100) if st["total"] > 0 else 0
        print(f"Student: {folder_name:40s} | Held-Out Accuracy: {pct:6.2f}% ({st['correct']}/{st['total']})")
        
    overall_acc = (correct / total_test * 100) if total_test > 0 else 0
    avg_dist = (sum(distances) / len(distances)) if distances else 0
    
    print("\n" + "=" * 60)
    print("FINAL HELD-OUT EVALUATION SUMMARY")
    print("=" * 60)
    print(f"Total Held-Out Test Images: {total_test}")
    print(f"Correct Identifications   : {correct}")
    print(f"Incorrect / Mismatches    : {incorrect}")
    print(f"Unknown (Above Threshold) : {unknown}")
    print(f"Average LBPH Distance     : {avg_dist:.2f}")
    print(f"Held-Out Generalization   : {overall_acc:.2f}%")
    print("=" * 60)

if __name__ == "__main__":
    run_heldout_evaluation()
