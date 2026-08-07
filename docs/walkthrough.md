# Face Recognition Layer — Walkthrough

> **This document was rewritten on 2026-08-08 after a full audit.**
> The previous version reported results that could not be reproduced from
> the committed code. Section 5 records what was wrong and why, so the
> correction is auditable. Every number below was produced by running the
> scripts named beside it, on this dataset, on this date.

---

## 1. Configuration actually in use

Defined once in [`train_model.py`](../train_model.py) as `LBPH_PARAMS`, and
imported by both evaluators so they can never drift from what ships:

```python
LBPH_PARAMS = {
    "radius": 2,
    "neighbors": 8,     # OpenCV default
    "grid_x": 8,
    "grid_y": 8,
}
```

Training uses **one histogram per captured image, with no synthetic
augmentation**. Preprocessing (eye-alignment → 200×200 → CLAHE) is applied
exactly once, in [`face_preprocessing.py`](../face_preprocessing.py), shared
by capture, training, and recognition.

| Property | Value |
|---|---|
| Identities | 3 |
| Images per student | 100 |
| Training samples | 300 |
| Histogram dimensions | 16,384 (256 bins × 64 cells) |
| Model size | **54,984,159 bytes (55 MB)** |
| Model load time | **9.2 s** |
| Recognition threshold | 58.0 (LBPH distance; lower is better) |

---

## 2. Measured results

### Method A — In-sample verification (`test_accuracy.py`)

```
Student: 23-1-1-0559_Chrizol D. Evangelista       | Accuracy: 100.00% (100/100)
Student: 23-1-1-0918_Jeff Christian P. Alcasid    | Accuracy: 100.00% (100/100)
Student: 23-1-1-0920_Julio B. Falloran Jr         | Accuracy: 100.00% (100/100)

Total Test Images      : 300
Correct Identifications: 300
Average LBPH Distance  : 0.00
Overall Accuracy       : 100.00%
```

**This number must not be quoted as accuracy.** The average distance is
`0.00` because every test image is also a training image, and LBPH stores one
histogram per training sample — so each query matches its own stored
histogram exactly. Method A only confirms the train → save → load → predict
pipeline is wired up correctly. It measures memorisation, not recognition.

### Method B — 80/20 held-out split (`test_heldout_accuracy.py`)

```
Student: 23-1-1-0559_Chrizol D. Evangelista       | Held-Out Accuracy: 100.00% (20/20)
Student: 23-1-1-0918_Jeff Christian P. Alcasid    | Held-Out Accuracy: 100.00% (20/20)
Student: 23-1-1-0920_Julio B. Falloran Jr         | Held-Out Accuracy: 100.00% (20/20)

Total Held-Out Test Images: 60
Correct Identifications   : 60
Incorrect / Mismatches    : 0
Unknown (Above Threshold) : 0
Average LBPH Distance     : 34.95
Held-Out Generalization   : 100.00%
```

**This number is inflated and should be reported with its caveats.** See §4.

---

## 3. Why `neighbors` is 8 and augmentation was removed

The previous configuration (`neighbors=12`, ×4 augmentation) was measured
against two alternatives on an identical 80/20 split (seed 42):

| Configuration | Samples | Dims | ms/predict | Held-out accuracy | Avg distance |
|---|---:|---:|---:|---:|---:|
| `neighbors=12` + ×4 aug *(previous)* | 960 | 262,144 | 1691.8 | **0.00 %** | 94.92 |
| `neighbors=8` + ×4 aug | 960 | 16,384 | 224.5 | 100.00 % | 34.88 |
| **`neighbors=8`, no aug** *(current)* | 300 | 16,384 | **64.2** | **100.00 %** | 34.95 |

The previous configuration failed in **two independent ways**:

1. **The model could not be persisted.** At 262,144 dimensions the model
   reaches 1.835 GB. `recognizer.write()` succeeds; `recognizer.read()` then
   fails inside `cv::FileStorage`:

   ```
   OpenCV(4.10.0) persistence.cpp:1613: error: (-215:Assertion failed)
   ofs == fs_data_blksz[blockIdx] in cv::FileStorage::Impl::normalizeNodeOfs
   ```

   Round-trip testing put the ceiling between 0.572 GB and 1.835 GB.

2. **Even in memory it recognised nobody.** Raising `neighbors` shifts LBPH
   distances upward — into the 81–107 band — while `RECOGNITION_THRESHOLD`
   remained 58.0. Every prediction exceeded the threshold: **0 / 60**.

Dropping augmentation cost nothing measurable (34.88 → 34.95 average
distance; both 60/60) while cutting stored samples 4×, model size 4×, and
per-prediction cost 3.5×. Augmentation helps models that *learn* parameters;
LBPH simply stores one histogram per sample, so augmentation mainly inflates
the model and slows every prediction.

---

## 4. Limits of these results — read before citing them

1. **Same-session leakage.** All 100 images per student come from a single
   capture session. The 80/20 split therefore divides near-duplicate
   consecutive video frames, so the "held-out" images closely resemble the
   training images. This is **not** an out-of-sample generalisation estimate.
2. **Closed set — no impostors.** Every test image belongs to an enrolled
   student, so the **false-acceptance rate was not measured**. For an
   attendance system that is the security-critical metric, and `58.0` remains
   an uncalibrated threshold.
3. **N = 3.** Three identities is a 3-way problem. Difficulty grows sharply
   with enrolment size; these results do not extrapolate.
4. **Threshold circularity.** The same 58.0 used in production scores the
   evaluation, with no ROC/DET curve and no justified operating point.
5. **Scaling ceiling remains.** The model holds one histogram per training
   image, so it grows linearly: 55 MB for 3 students ≈ **18.3 MB/student**.
   Round-trip testing showed `FileStorage` reads 0.572 GB successfully and
   fails at 1.835 GB, so the ceiling returns somewhere between **≈31 and
   ≈100 students** — the exact limit was not bisected. `neighbors=8` buys
   headroom for a class-sized pilot; it does not remove the limit. A
   constant-size embedding backend would.

**A defensible evaluation requires:** capture across ≥2 separate sessions per
student, a set of non-enrolled faces, and reporting **FAR / FRR / EER** with a
DET curve. Tracked as Phase 6 in [`tasks/todo.md`](../tasks/todo.md).

---

## 5. Correction log — what the previous version got wrong

| Previous claim | Status | Finding |
|---|---|---|
| "Method A: 100.00% (300/300), avg distance 16.57" | **Unreproducible** | `test_accuracy.py` had a variable-aliasing bug: `label_map` stored student IDs, but the evaluation loop joined those onto `DATASET_DIR` as folder names. No folder ever matched, so it scored **0 images** and reported 0.00%. Fixed; now scores 300/300 at distance 0.00. |
| "Method B: 100.00% (60/60), avg distance 24.36" | **Unreproducible as stated** | Ran at `neighbors=12`, which yields distances of 81–107 — all above the 58.0 threshold — so the true result was 0/60. The 24.36 figure does not correspond to this configuration. |
| "LBPH tuning to `neighbors=12` captures larger spatial facial structures" | **Incorrect** | This change is what broke the system: it made the model unloadable *and* pushed every distance above the recognition threshold. |
| "Augmentation expands the dataset to 1,200 training images" | **Accurate but counterproductive** | It did produce 1,200 samples. For LBPH that is 4× the storage and 4× the per-prediction cost for no measured accuracy gain. |
| "Reduced identification latency from ~2.5 s to ~1.0 s" | **Unverified** | No timing harness exists. Not reproduced; treat as unsubstantiated. |
| "+5-10 FPS gain from threaded camera acquisition" | **Unverified** | No FPS measurement exists in the codebase. Not reproduced. |

Two further defects were found and fixed while restoring the system:

- **Non-atomic model save.** `train_model()` deleted `trainer.yml` and
  `labels.txt` *before* writing replacements, so any failure mid-write
  destroyed the working model with no backup. This is what took the system
  down: a truncated 878 MB `trainer.yml` with no `labels.txt`. Saving is now
  atomic (temp write → `.bak` rotation → `os.replace`, with rollback).
- **One bad folder blocked all training.** A student folder with too few
  images aborted the entire run, so a single cancelled enrolment made the
  system unrecoverable. Such folders are now skipped and named in the result.

---

## 6. Reproducing these numbers

```bash
python train_model.py             # builds trainer/trainer.yml + labels.txt
python test_accuracy.py           # Method A (in-sample sanity check)
python test_heldout_accuracy.py   # Method B (80/20 held-out)
```

Environment: Python 3.11, `opencv-contrib-python==4.10.0.84`,
`mediapipe==0.10.14`. See [`requirements.txt`](../requirements.txt).
