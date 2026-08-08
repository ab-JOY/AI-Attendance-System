"""
Regression tests for FS-12 — enrolment must not report failure as success.

`capture_dataset.py` used bare `sys.exit()` on every failure path. That exits
with status **0**, and `app.py` reads 0 as a completed enrolment and launches
an automatic retrain. A missing dependency, a bad argument or an unopenable
camera therefore produced a "successful" enrolment for a student with no
dataset, which then fed FS-2 and FS-9.

Three layers of check here, cheapest first:

1. the contract itself is sane;
2. no bare `sys.exit()` survives in the script (AST, no import, no camera);
3. the real script really does exit non-zero — run as a subprocess.

Layer 3 is possible without a camera because argument validation happens
before `open_best_camera()` and before the dataset folder is created, so
these runs touch no hardware and leave no directories behind.
"""

import ast
import subprocess
import sys

import pytest

from config.exit_codes import EXIT_CANCELLED, EXIT_FAILURE, EXIT_SUCCESS
from tests.conftest import PROJECT_ROOT

CAPTURE_SCRIPT = PROJECT_ROOT / "capture_dataset.py"


# ---------------------------------------------------------------------------
# 1. The contract
# ---------------------------------------------------------------------------

def test_exit_codes_are_distinct_and_success_is_zero():
    """
    Success must be 0 because that is what the OS and subprocess.run mean by
    success, and cancelled must differ from failure because app.py redirects
    quietly for one and shows an error page for the other.
    """
    assert EXIT_SUCCESS == 0
    assert EXIT_FAILURE != EXIT_SUCCESS
    assert EXIT_CANCELLED != EXIT_SUCCESS
    assert EXIT_CANCELLED != EXIT_FAILURE


# ---------------------------------------------------------------------------
# 2. No bare sys.exit() left in the capture script
# ---------------------------------------------------------------------------

def test_capture_dataset_has_no_bare_sys_exit():
    """
    `sys.exit()` with no argument exits 0. In a script whose exit code is the
    only signal app.py receives, an argument-less exit is the bug itself, so
    it is banned outright rather than reviewed case by case.

    Parsed with ast: importing capture_dataset runs a camera session (MA-2).
    """
    tree = ast.parse(CAPTURE_SCRIPT.read_text(encoding="utf-8"))

    bare = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "exit"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "sys"
        and not node.args
    ]

    assert not bare, (
        f"capture_dataset.py has bare sys.exit() at line(s) {bare}. "
        "That exits 0, which app.py reads as a completed enrolment. "
        "Use EXIT_FAILURE / EXIT_CANCELLED / EXIT_SUCCESS from "
        "config.exit_codes."
    )


# ---------------------------------------------------------------------------
# 3. The real script, run for real
# ---------------------------------------------------------------------------

def _run_capture(*args):
    """
    Run capture_dataset.py as app.py does. Only used with arguments that fail
    validation, which happens before any camera is opened or any folder is
    created.
    """
    return subprocess.run(
        [sys.executable, str(CAPTURE_SCRIPT), *args],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.mark.slow
@pytest.mark.parametrize(
    "args, description",
    [
        ((), "no arguments at all"),
        (("23-1-1-0559",), "student id but no name"),
        (("", ""), "empty student id and name"),
        (("   ", "   "), "whitespace-only student id and name"),
    ],
)
def test_invalid_arguments_exit_non_zero(args, description):
    """
    This is the exact failure FS-12 describes: before the fix every one of
    these returned 0 and app.py went on to retrain the model.
    """
    result = _run_capture(*args)

    assert result.returncode == EXIT_FAILURE, (
        f"capture_dataset.py with {description} exited "
        f"{result.returncode}, expected {EXIT_FAILURE}. "
        f"An exit code of {EXIT_SUCCESS} would be reported to the operator "
        f"as a completed enrolment.\nstderr:\n{result.stderr}"
    )


@pytest.mark.slow
def test_failure_exit_is_not_mistaken_for_cancellation():
    """
    app.py redirects silently on EXIT_CANCELLED. A genuine failure must not
    borrow that code, or the operator sees no error at all.
    """
    result = _run_capture()

    assert result.returncode != EXIT_CANCELLED
    assert result.returncode != EXIT_SUCCESS


@pytest.mark.slow
def test_invalid_arguments_create_no_dataset_folder(tmp_path, monkeypatch):
    """
    Argument validation must happen before the dataset folder is created.
    An empty `dataset/{id}_{name}` directory left behind by a failed run is
    what produces the dangling folders behind FS-2 and FS-9.
    """
    dataset_dir = PROJECT_ROOT / "dataset"
    before = set(dataset_dir.iterdir()) if dataset_dir.exists() else set()

    _run_capture("", "")

    after = set(dataset_dir.iterdir()) if dataset_dir.exists() else set()

    assert after == before, f"failed run created: {sorted(after - before)}"
