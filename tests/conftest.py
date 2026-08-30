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

What is left is library import cost - cv2 0.60 s, flask 0.51 s, mysql-connector
- and Python caches it per process, so it is paid once per session, not per
test file. (An earlier version of this note quoted pandas at 1.32 s. pandas was
removed in Phase 5 with PE-8; the export is openpyxl now.)

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
import tempfile
from pathlib import Path

# The project uses a flat layout with modules at the repository root, so tests
# need the root on sys.path to import them without an editable install.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Keep the suite out of the deployment's log (R9)
#
# `app.py` calls configure_logging() at module scope - correctly, it owns the
# process - and several test modules import `app` to build a test client. So
# the rotating file handler was attached to logs/app.log before any test ran,
# and the file filled up with test output: fabricated sessions ("Refusing to
# start CS402: a session for CS401 is still running"), a job named `t` raising
# on purpose, repeated camera scans. 2.7 MB and 25,000 lines, most of it
# rehearsal.
#
# That log is the only record of what the deployed system did, and it is the
# artefact you would read after a failed demo. It cannot do that job while a
# real error is buried among invented ones.
#
# ⚠️ **Module scope, not a fixture.** The handler is attached at *import*, and
# pytest imports test modules during collection - before any fixture, including
# a session-scoped autouse one, has run. A fixture here would be tidier and
# would not work.
#
# The console handler is untouched: pytest captures it and shows it on a
# failure, which is where log output is useful during a test run.
# ---------------------------------------------------------------------------
# `settings` is the one shared instance every module imports, so redirecting it
# here redirects it for `app` too, whenever `app` is first imported.
from config.settings import settings as _settings  # noqa: E402

_settings.log_dir = Path(tempfile.gettempdir()) / "attendance-test-logs"


# Every first-party package. Listed rather than discovered, because a glob
# would pick up `.venv/`, `dataset/` and anything else that appears beside
# them - and a source-level ban that silently stops covering a directory is
# the vacuous-test failure lessons.md L12 describes.
SOURCE_PACKAGES = (
    "config",
    "infra",
    "repositories",
    "security",
    "services",
    "vision",
    "web",
)


def project_python_files():
    """
    Every first-party module: the root-level ones and the packages above.

    Used by the source-level bans (the MA-4 gate duplication, the db_cursor
    rules) so that a *new* file is covered the day it is written rather than
    the day somebody remembers to add it to a list. `tests/` and `scripts/`
    are excluded: the first tests the rules and the second is operator tooling
    that does not serve requests.
    """
    files = sorted(PROJECT_ROOT.glob("*.py"))

    for package in SOURCE_PACKAGES:
        files.extend(sorted((PROJECT_ROOT / package).rglob("*.py")))

    return [path for path in files if path.name != "__init__.py"]


# ---------------------------------------------------------------------------
# No test may install a real process-exit hook
#
# `RecognitionSession` registers an `atexit` hook the first time it opens a
# camera, so that a session still running when the process ends releases the
# device instead of leaving it wedged (the `0x8007001F` defect). That is right
# in production and wrong in a test run: dozens of sessions with *fake*
# hardware are created here and many are deliberately left running, so their
# hooks all fire during interpreter shutdown - after pytest has closed the
# streams its logging handlers write to. The result was a wall of
# "--- Logging error --- ValueError: I/O operation on closed file".
#
# Patching `atexit.register` itself, rather than passing a fake into every
# construction, keeps the ban in one place and covers tests written later.
# `vision/session.py` resolves the function at call time precisely so this
# works.
# ---------------------------------------------------------------------------
import atexit as _atexit  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def no_real_exit_hooks(monkeypatch):
    """Collect exit hooks instead of registering them. Returns the list."""
    registered = []

    monkeypatch.setattr(_atexit, "register", registered.append)

    return registered
