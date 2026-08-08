"""
Shared pytest configuration.

IMPORTANT - do not import `recognize_face` or `app` from anything under
tests/, with the single exception noted below. Both execute real work at
import time: recognize_face.py loads the 55 MB LBPH model at module scope and
constructs a MediaPipe FaceMesh, costing around 9 seconds and a large memory
spike per import, and app.py imports recognize_face. That is PE-4, deferred
to Phase 3.

**The exception: tests/test_route_security.py.** Phase 2 made every route
authenticated and role-checked, and the only honest way to test that is to
drive real requests through `app.test_client()` - lessons.md L5 is exactly
the story of a check one layer away from where the failure lived. It pays the
9 s import once, in a session-scoped fixture, and is marked `slow` so
`pytest -m "not slow"` stays fast. Nothing else under tests/ may import app.

`train_model`, `face_preprocessing`, `camera_utils`, `config.*` and
`security.*` are all safe to import - they define things without doing
anything.
"""

import sys
from pathlib import Path

# The project uses a flat layout with modules at the repository root, so tests
# need the root on sys.path to import them without an editable install.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
