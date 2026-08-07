"""
Shared pytest configuration.

IMPORTANT - do not import `recognize_face` or `app` from anything under
tests/. Both execute real work at import time: recognize_face.py loads the
55 MB LBPH model at module scope and constructs a MediaPipe FaceMesh, costing
around 9 seconds and a large memory spike per import, and app.py imports
recognize_face. That is PE-4, deferred to Phase 3.

`train_model`, `face_preprocessing`, `camera_utils` and `config.*` are all
safe to import - they define things without doing anything.
"""

import sys
from pathlib import Path

# The project uses a flat layout with modules at the repository root, so tests
# need the root on sys.path to import them without an editable install.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
