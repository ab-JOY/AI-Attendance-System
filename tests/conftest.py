"""
Shared pytest configuration.

**The old rule here - "nothing under tests/ may import `recognize_face` or
`app`" - is gone, because PE-4 removed the reason for it.**

Importing `recognize_face` used to load the 55 MB LBPH model and construct a
MediaPipe FaceMesh at module scope: 9.2 s and a large memory spike, paid by
anything that imported it, `app` included. Phase 3 moved both behind
`RecognitionSession`, so nothing happens at import any more. Measured, median
of three cold subprocess runs:

    import recognize_face   9.15 s -> 1.82 s
    import app            12.16 s -> 3.27 s

What is left is library import cost - cv2 0.60 s, pandas 1.32 s, flask 0.51 s -
and Python caches it per process, so it is paid once per session, not per test
file.

So importing either module is now allowed. Two things are still worth knowing:

* **Prefer testing `vision/` where you can.** That package has no camera, no
  MediaPipe, no database and no model, and its tests run in milliseconds. The
  session, tracker, geometry gate, quality gate and liveness logic all live
  there precisely so they can be tested without any of that.
* **`tests/test_recognition_loop_smoke.py` is `slow` and skips without
  `dataset/`.** Not for the import - it drives 60 real frames through
  MediaPipe and LBPH, and it needs the gitignored biometric data to do it.

`train_model`, `face_preprocessing`, `camera_utils`, `config.*`, `vision.*` and
`security.*` are all cheap and always have been - they define things without
doing anything.
"""

import sys
from pathlib import Path

# The project uses a flat layout with modules at the repository root, so tests
# need the root on sys.path to import them without an editable install.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
