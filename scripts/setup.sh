#!/usr/bin/env bash
#
# One-command development setup: interpreter check, virtualenv, pinned
# dependencies, .env with a generated SECRET_KEY, and a smoke test of the three
# imports most likely to be broken.
#
#   bash scripts/setup.sh              # set up, or top up an existing setup
#   bash scripts/setup.sh --recreate   # throw the venv away and rebuild it
#
# Safe to re-run: every step is skipped if it is already done, and an existing
# .env is never overwritten.
#
# It does NOT touch the database, and it does not read or write dataset/ or
# trainer/. `python setup_db.py` is left to you because it creates a schema and
# seeds an account, which is not something a setup script should do behind your
# back.
#
# Runs on Git Bash (Windows), macOS and Linux. The venv layout differs -
# .venv/Scripts on Windows, .venv/bin elsewhere - so nothing below hardcodes
# either; PYTHON is resolved once, after the venv exists.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

RECREATE=0
if [ "${1:-}" = "--recreate" ]; then
    RECREATE=1
elif [ -n "${1:-}" ]; then
    echo "usage: bash scripts/setup.sh [--recreate]" >&2
    exit 2
fi

step() { printf '\n=== %s\n' "$1"; }
fail() { printf '\nFAILED: %s\n' "$1" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 1. Interpreter
#
# 3.11 exactly, and the upper bound is the load-bearing half: mediapipe
# 0.10.14 publishes no wheels for 3.12+, so a 3.12 venv installs cleanly and
# then dies at `import mediapipe`. Catching it here costs a second; catching it
# at the first camera page costs an afternoon.
# ---------------------------------------------------------------------------
step "Looking for Python 3.11"

find_python() {
    local candidate version
    for candidate in "python3.11" "python3" "python" "py -3.11"; do
        version="$($candidate -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
        if [ "$version" = "3.11" ]; then
            echo "$candidate"
            return 0
        fi
    done
    return 1
}

HOST_PYTHON="$(find_python || true)"
[ -n "$HOST_PYTHON" ] || fail "no Python 3.11 on PATH.
    mediapipe==0.10.14 has no wheels for 3.12 or later, so this project pins
    >=3.11,<3.12. Install 3.11 and re-run:
      https://www.python.org/downloads/release/python-3119/"

echo "    $HOST_PYTHON -> $($HOST_PYTHON -c 'import sys; print(sys.version.split()[0])')"

# ---------------------------------------------------------------------------
# 2. Virtualenv
#
# An existing .venv is reused, but only after its interpreter is checked. A
# 3.12 venv left over from an earlier attempt would otherwise be topped up
# happily and fail later, which is the failure mode step 1 exists to prevent.
# ---------------------------------------------------------------------------
step "Virtual environment"

if [ "$RECREATE" = "1" ] && [ -d .venv ]; then
    echo "    --recreate: removing the existing .venv"
    rm -rf .venv
fi

if [ -d .venv ]; then
    echo "    .venv exists, reusing it"
else
    echo "    creating .venv"
    $HOST_PYTHON -m venv .venv
fi

if [ -x .venv/Scripts/python.exe ]; then
    PYTHON=".venv/Scripts/python.exe"     # Windows
elif [ -x .venv/bin/python ]; then
    PYTHON=".venv/bin/python"             # POSIX
else
    fail ".venv exists but has no interpreter in Scripts/ or bin/.
    Re-run with --recreate to rebuild it."
fi

VENV_VERSION="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
[ "$VENV_VERSION" = "3.11" ] || fail ".venv runs Python $VENV_VERSION, not 3.11.
    Re-run with --recreate to rebuild it against $HOST_PYTHON."

# ---------------------------------------------------------------------------
# 3. Dependencies
#
# pip is upgraded first, and not out of tidiness: pip 23.x cannot parse the
# `Metadata-Version: 2.4` wheels several of these pins now ship, and the error
# it gives is about metadata rather than about the pin. A venv created by an
# older 3.11 ships exactly that pip.
#
# Versions come from pyproject.toml, where every one of them is pinned to what
# this system was measured against (PO-1). Nothing is pinned here, so there is
# only ever one place to change one.
# ---------------------------------------------------------------------------
step "Dependencies (this pulls ~250 MB the first time)"

"$PYTHON" -m pip install --upgrade pip --quiet
"$PYTHON" -m pip install -e ".[dev]" --quiet
echo "    installed from pyproject.toml [dev]"

# ---------------------------------------------------------------------------
# 4. Configuration
#
# SECRET_KEY has no default and the app refuses to start without one, so a
# setup that stopped at a copied .env would still not run. The key is generated
# here and written straight in.
#
# An existing .env is never touched - it holds your database password, and
# rewriting it would also invalidate every live session cookie.
# ---------------------------------------------------------------------------
step "Configuration"

if [ -f .env ]; then
    echo "    .env exists, leaving it alone"
else
    "$PYTHON" - <<'PY'
import re
import secrets
from pathlib import Path

text = Path(".env.example").read_text(encoding="utf-8")

text, count = re.subn(
    r"(?m)^SECRET_KEY=.*$",
    "SECRET_KEY=" + secrets.token_urlsafe(48),
    text,
    count=1,
)

if count != 1:
    raise SystemExit("    no SECRET_KEY line in .env.example - refusing to guess")

Path(".env").write_text(text, encoding="utf-8")
print("    wrote .env from .env.example, with a fresh 48-byte SECRET_KEY")
PY
fi

# ---------------------------------------------------------------------------
# 5. Smoke test
#
# Three specific things, each of which has actually broken here:
#
#   cv2.face      - only opencv-CONTRIB ships the LBPH recogniser. Plain
#                   opencv-python installs fine and recognition then cannot
#                   start at all.
#   mediapipe     - imports TensorFlow if it finds one, and catches only
#                   ImportError. A stale TF built against NumPy 1.x raises
#                   SystemError instead, straight through the fallback.
#   settings      - validates .env at import, so a short SECRET_KEY or an
#                   unparseable value is a boot failure. Better here than at
#                   the first request.
# ---------------------------------------------------------------------------
step "Verifying the install"

"$PYTHON" - <<'PY'
import sys

try:
    import cv2
except Exception as error:
    sys.exit(f"    cv2 will not import: {error}")

if not hasattr(cv2, "face"):
    sys.exit(
        "    cv2.face is missing - that is opencv-python, not "
        "opencv-contrib-python.\n"
        "    Fix: pip uninstall -y opencv-python && pip install -e '.[dev]'"
    )

try:
    import mediapipe  # noqa: F401
except Exception as error:
    sys.exit(f"    mediapipe will not import: {error}")

try:
    from config.settings import settings
except Exception as error:
    sys.exit(f"    config rejected .env: {error}")

print(f"    cv2 {cv2.__version__} with cv2.face, mediapipe, and .env all OK")
print(
    "    database target: "
    f"{settings.db_user}@{settings.db_host}:{settings.db_port}/{settings.db_name}"
)
PY

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
if [ -x .venv/Scripts/python.exe ]; then
    ACTIVATE="source .venv/Scripts/activate"
else
    ACTIVATE="source .venv/bin/activate"
fi

cat <<DONE

=== Setup complete

Next, in order:

  $ACTIVATE
  # edit .env first if MySQL is not root@127.0.0.1 with an empty password
  python setup_db.py      # creates the schema, seeds admin/admin
  python app.py           # http://127.0.0.1:5000

The seeded admin/admin is published in the README, so it is not a password.
That account can reach nothing but the change-password screen until you
replace it.

Optional:
  pytest -m "not slow and not integration"   # the fast suite, ~80s
  python scripts/preflight.py                # read-only readiness check
DONE
