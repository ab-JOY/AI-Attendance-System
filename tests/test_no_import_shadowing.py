"""
Guards against a module-level name being imported and then redefined.

This exists because of a real bug introduced in Phase 1. app.py did:

    from config.settings import settings      # line 23
    ...
    @app.route('/settings')
    def settings():                           # line 1376 - shadows the import

Nothing failed at import time, because `app.secret_key = settings.secret_key`
runs before the `def` executes. The application booted, the model loaded, the
unit tests passed and ruff was clean. But `get_db_connection()` runs per
request, and by then `settings` was the view function, so every single
database call would have raised
`AttributeError: 'function' object has no attribute 'db_kwargs'`.

A shadowed import is invisible to every other check in this project, so it
gets its own. The modules are parsed with `ast`, never imported - importing
recognize_face or app costs ~9 s and loads a 55 MB model (PE-4), and
importing capture_dataset would open a camera (MA-2).
"""

import ast

import pytest

from tests.conftest import PROJECT_ROOT

MODULES = [
    "app.py",
    "recognize_face.py",
    "train_model.py",
    "capture_dataset.py",
    "camera_utils.py",
    "setup_db.py",
    "eval_accuracy.py",
    "eval_heldout_accuracy.py",
]


def _module_level_bindings(tree):
    """Return ({imported name: line}, {defined name: first line})."""
    imported = {}
    defined = {}

    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = (alias.asname or alias.name).split(".")[0]
                imported.setdefault(bound, node.lineno)

        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported.setdefault(alias.asname or alias.name, node.lineno)

        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            defined.setdefault(node.name, node.lineno)

        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    defined.setdefault(target.id, node.lineno)

    return imported, defined


@pytest.mark.parametrize("module_name", MODULES)
def test_no_module_level_name_shadows_an_import(module_name):
    path = PROJECT_ROOT / module_name
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    imported, defined = _module_level_bindings(tree)

    collisions = {
        name: (imported[name], defined[name])
        for name in imported
        if name in defined
    }

    assert not collisions, (
        f"{module_name} rebinds imported name(s) at module level: "
        + "; ".join(
            f"{name!r} imported on line {imp}, redefined on line {red}"
            for name, (imp, red) in sorted(collisions.items())
        )
        + ". Alias the import or rename the definition - see this test's "
        "docstring for why this is not merely a style issue."
    )
