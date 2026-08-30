"""
Regression tests for SE-3 - path traversal into `shutil.rmtree()`.

The vulnerable construction was:

    folder_name = f"{student['student_id']}_{student['name']}"
    shutil.rmtree(os.path.join("dataset", folder_name))

with `student_id` and `name` taken from an unvalidated form. A student named
`..\\..\\Windows\\Temp` gave an authenticated user directory deletion outside
`dataset/`.

Two properties are asserted here and both matter:

1. Traversal input is refused (the allowlist), and
2. no accepted input can produce a path outside `dataset/` (containment).

The second is the one that still holds if somebody widens the first.

**Phase 5 narrowed the input rather than the rule (todo.md §7.5).** The folder
is `dataset/{student_id}`, so a *name* can no longer reach a path at all - one
whole half of the original attack surface is gone by construction. The name is
still validated, because it is still stored and rendered, and those tests stay
here: `validate_student_name` remains the reason a hostile value never reaches
the database.
"""

import ast

import pytest

from security.paths import (
    UnsafeStudentPathError,
    student_dataset_path,
    student_folder_name,
    validate_student_id,
    validate_student_name,
)
from tests.conftest import project_python_files

# The three students actually enrolled on this system, read from dataset/.
# If a change to the allowlist ever rejects one of these, enrolment,
# deletion and recapture all break for a real person.
LIVE_STUDENTS = [
    ("23-1-1-0559", "Chrizol D. Evangelista"),
    ("23-1-1-0918", "Jeff Christian P. Alcasid"),
    ("23-1-1-0920", "Julio B. Falloran Jr"),
]


# ---------------------------------------------------------------------------
# The attack
# ---------------------------------------------------------------------------

TRAVERSAL_INPUTS = [
    "..",
    "../..",
    r"..\..",
    r"..\..\Windows\Temp",
    "../../etc/passwd",
    "dataset/../../..",
    "/etc",
    r"C:\Windows",
    r"\\server\share",
    "a/b",
    "a\\b",
    ".",
]


@pytest.mark.parametrize("hostile", TRAVERSAL_INPUTS)
def test_traversal_is_refused_in_the_student_id(hostile):
    with pytest.raises(UnsafeStudentPathError):
        validate_student_id(hostile)


@pytest.mark.parametrize("hostile", TRAVERSAL_INPUTS)
def test_traversal_is_refused_in_the_student_name(hostile):
    with pytest.raises(UnsafeStudentPathError):
        validate_student_name(hostile)


@pytest.mark.parametrize("hostile", TRAVERSAL_INPUTS)
def test_no_path_is_produced_for_hostile_input(tmp_path, hostile):
    """The end-to-end property: rmtree never receives a path to build on."""
    with pytest.raises(UnsafeStudentPathError):
        student_dataset_path(hostile, dataset_dir=tmp_path)


def test_null_byte_is_refused():
    """A NUL truncates the path at the OS boundary, below Python's checks."""
    with pytest.raises(UnsafeStudentPathError):
        validate_student_name("Real\x00Name")

    with pytest.raises(UnsafeStudentPathError):
        validate_student_id("23-1\x00")


@pytest.mark.parametrize("control", ["\n", "\r", "\t"])
def test_control_characters_are_refused(control):
    with pytest.raises(UnsafeStudentPathError):
        validate_student_name(f"Real{control}Name")


# ---------------------------------------------------------------------------
# Containment holds independently of the allowlist
# ---------------------------------------------------------------------------


def test_every_accepted_path_is_a_direct_child_of_the_dataset_directory(tmp_path):
    base = tmp_path.resolve()

    for student_id, _name in LIVE_STUDENTS:
        path = student_dataset_path(student_id, dataset_dir=tmp_path)

        assert path.parent == base
        assert path.is_relative_to(base)


def test_resolved_name_must_match_what_was_asked_for(tmp_path):
    """
    Windows strips trailing dots and spaces from path components, so a folder
    ending in one would be created under a different name and never found
    again.

    ⚠️ **This applies to the student *ID* and no longer to the name.** The
    folder has been `dataset/{student_id}` since Phase 5; the name is not a
    path component, so the same rule applied to it was rejecting `Jose Jr.` to
    protect a filesystem behaviour nothing can reach. See
    `test_a_name_may_end_in_a_period` below.
    """
    with pytest.raises(UnsafeStudentPathError):
        student_dataset_path("23-1-1-0559.", dataset_dir=tmp_path)


# ---------------------------------------------------------------------------
# Real names must keep working
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("student_id", "name"), LIVE_STUDENTS)
def test_the_three_enrolled_students_are_accepted(student_id, name):
    assert validate_student_id(student_id) == student_id
    assert validate_student_name(name) == name


@pytest.mark.parametrize("name", [
    "Juan Dela Cruz Jr.",
    "Jose Rizal Sr.",
    "Ma. Teresa Santos",
    "Jose P. Rizal",
    "Maria Cristina D. Reyes Jr.",
])
def test_a_name_may_end_in_a_period(name):
    """
    Reported by the user 2026-08-29: `Jr.` was refused, and with it `Sr.` -
    rejected by a validator whose reason had expired. The rule was positional,
    on the last character, so `Ma. Teresa` was never affected; the cases that
    broke are exactly the suffixes.

    The reason was real once: under `dataset/{student_id}_{student_name}` the
    name was a path component and Windows strips trailing dots from those.
    Phase 5 made the folder `dataset/{student_id}`, so the name stopped being
    part of any path and the rule went on refusing real students to guard a
    behaviour it could no longer reach.
    """
    assert validate_student_name(name) == name


@pytest.mark.parametrize("name", [".", "..", "...", ". . .", "-", "'"])
def test_a_name_of_punctuation_alone_is_still_refused(name):
    """
    What replaced the trailing-dot rule, and why removing it did not simply
    widen the allowlist: a period is legitimate *inside* a name, so `..` passes
    the character allowlist. It is refused for being no name at all rather than
    for anything about filesystems.
    """
    with pytest.raises(UnsafeStudentPathError):
        validate_student_name(name)


def test_the_student_id_rule_is_unchanged():
    """
    ⚠️ An ID **is** the folder name, so the trailing dot still has to go. The
    two validators are deliberately not consistent with each other; do not
    align them.
    """
    with pytest.raises(UnsafeStudentPathError):
        validate_student_id("23-1-1-0559.")


def test_apostrophes_are_accepted():
    """`O'Brien` is a name, not an injection attempt (see US-5)."""
    assert validate_student_name("Siobhan O'Brien") == "Siobhan O'Brien"


def test_non_ascii_names_are_accepted():
    """Filipino names carry diacritics; an ASCII-only rule excludes people."""
    assert validate_student_name("Maria Peña") == "Maria Peña"


def test_the_folder_is_the_student_id_and_nothing_else(tmp_path):
    """
    The name is not in the path (todo.md §7.5).

    `train_model.parse_dataset_folder()` reads a folder name as a student ID
    and `labels.txt` stores that ID, so this format and those two readers have
    to agree. A folder carrying a name again would strand the trained model.
    """
    assert student_folder_name("23-1-1-0920") == "23-1-1-0920"

    path = student_dataset_path("23-1-1-0920", dataset_dir=tmp_path)
    assert path.name == "23-1-1-0920"


def test_underscores_are_refused_in_the_student_id():
    """
    This rule outlived its original reason and is kept deliberately.

    Under `{id}_{name}` an underscore in the ID silently moved part of it into
    the name and corrupted the label map. That scheme is gone - but `dataset/`
    on an un-migrated machine still holds folders written under it, and an ID
    containing `_` would be ambiguous against them. Real IDs look like
    `23-1-1-0559`.
    """
    with pytest.raises(UnsafeStudentPathError):
        validate_student_id("23_1_1_0559")


def test_surrounding_whitespace_is_stripped_not_rejected(tmp_path):
    """Forms submit stray spaces; that is a typo, not an attack."""
    assert validate_student_id("  23-1-1-0559  ") == "23-1-1-0559"
    assert validate_student_name("  Chrizol D. Evangelista  ") == "Chrizol D. Evangelista"


@pytest.mark.parametrize("empty", [None, "", "   "])
def test_empty_values_are_refused(empty):
    with pytest.raises(UnsafeStudentPathError):
        validate_student_id(empty)

    with pytest.raises(UnsafeStudentPathError):
        validate_student_name(empty)


def test_over_long_values_are_refused():
    with pytest.raises(UnsafeStudentPathError):
        validate_student_id("2" * 65)

    with pytest.raises(UnsafeStudentPathError):
        validate_student_name("N" * 129)


# ---------------------------------------------------------------------------
# The rule holds at the call sites, not just in this module
# ---------------------------------------------------------------------------

# ⚠️ **This list is derived, not typed.**
#
# It was `["app.py", "capture_dataset.py"]`. Phase 5 deleted the second file
# and split the first into eight blueprints, and a hardcoded list would then
# have been checking a module that no longer touches a dataset path while
# ignoring the four that do. The rule is unchanged - whatever builds a dataset
# path imports the validator - so the *set of callers* is found rather than
# restated.
DATASET_PATH_MARKERS = ("dataset_dir", "dataset_staging_dir")

# Modules that name a dataset location without ever addressing *a particular
# student's* folder, which is the thing SE-3 is about. Listed with the reason,
# because an allowlist without one becomes a place to hide.
NOT_PER_STUDENT = {
    # Defines the setting. Nothing to import it from.
    "settings.py",
    # Enumerate `dataset/` with os.listdir and read back what training wrote.
    # No request, no identifier, no path built from either.
    "eval_accuracy.py",
    "eval_heldout_accuracy.py",
    # Reads every folder to train on. `parse_dataset_folder()` validates each
    # name through security.paths, which its own tests cover.
    "train_model.py",
    # The migration script - operator tooling, run by hand against a directory
    # named on the command line.
    "rename_dataset_folders.py",
}


def _modules_that_build_dataset_paths():
    callers = []

    for path in project_python_files():
        # security/paths.py is the validator itself.
        if path.name == "paths.py" or path.name in NOT_PER_STUDENT:
            continue

        source = path.read_text(encoding="utf-8")

        if any(marker in source for marker in DATASET_PATH_MARKERS):
            callers.append(path)

    return callers


@pytest.mark.parametrize(
    "path",
    _modules_that_build_dataset_paths(),
    ids=lambda path: path.name,
)
def test_callers_use_the_shared_helper(path):
    """
    A validator nothing calls fixes nothing. Every module that reaches for a
    dataset location must get its path from security.paths.
    """
    source = path.read_text(encoding="utf-8")

    assert "security.paths" in source, (
        f"{path.name} names a dataset location but does not import "
        "security.paths - SE-3 would be open again."
    )


@pytest.mark.parametrize(
    "path", project_python_files(), ids=lambda path: path.name
)
def test_no_module_joins_a_dataset_path_by_hand(path):
    """
    Guards the *class* of bug rather than the five instances that existed,
    in the spirit of test_no_import_shadowing.py.

    The vulnerable pattern was a literal directory name joined onto a
    user-derived folder name:

        os.path.join("dataset", f"{student_id}_{name}")

    Anything of that shape bypasses the containment check, so it is banned
    outright at source level. Build the path with student_dataset_path().

    Swept over every first-party module rather than a named list: a ban that
    covers the files somebody remembered is a ban with a hole the shape of the
    next file.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    offenders = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        rendered = ast.unparse(node.func)

        if rendered not in ("os.path.join", "Path"):
            continue

        for argument in node.args:
            if isinstance(argument, ast.Constant) and argument.value == "dataset":
                offenders.append(f"line {node.lineno}: {ast.unparse(node)}")

    assert not offenders, (
        f"{path.name} joins a dataset path by hand, which skips the "
        "containment check (SE-3):\n  " + "\n  ".join(offenders)
    )


# `pathlib` made the original `os.path.join("dataset", ...)` shape unfashionable
# rather than impossible, and the modern spelling is the one a newly written
# blueprint would actually reach for.
DATASET_ROOTS = ("dataset_dir", "dataset_staging_dir")

# Where dividing the dataset root by something is the *correct* thing to do.
MAY_DIVIDE_THE_ROOT = {
    "paths.py",  # the containment check itself
    "dataset_store.py",  # sweeps the staging root
    "rename_dataset_folders.py",  # operator tooling over whole directories
}


@pytest.mark.parametrize(
    "path", project_python_files(), ids=lambda path: path.name
)
def test_no_module_divides_the_dataset_root_by_hand(path):
    """
    `settings.dataset_dir / student_id` is the same bug in modern spelling.

    ⚠️ **Added in Phase 5 because the blueprint split created the opportunity.**
    Building a per-student path is now four lines from the route that used to
    contain it, and the obvious wrong way to do it in a new `web/` module is a
    one-character operator. The original ban catches
    `os.path.join("dataset", ...)`, which nobody would write today.

    Mutation-tested: adding `settings.dataset_dir / student_id` to
    web/students.py fails this.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    if path.name in MAY_DIVIDE_THE_ROOT:
        pytest.skip(f"{path.name} owns the dataset root")

    offenders = [
        f"line {node.lineno}: {ast.unparse(node)}"
        for node in ast.walk(tree)
        if isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Div)
        and isinstance(node.left, ast.Attribute)
        and node.left.attr in DATASET_ROOTS
    ]

    assert not offenders, (
        f"{path.name} builds a path under the dataset root directly, which "
        "skips the containment check (SE-3). Use "
        "security.paths.student_dataset_path():\n  " + "\n  ".join(offenders)
    )
