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
from tests.conftest import PROJECT_ROOT

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
        student_dataset_path(hostile, "Real Name", dataset_dir=tmp_path)

    with pytest.raises(UnsafeStudentPathError):
        student_dataset_path("23-1-1-0559", hostile, dataset_dir=tmp_path)


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

    for student_id, name in LIVE_STUDENTS:
        path = student_dataset_path(student_id, name, dataset_dir=tmp_path)

        assert path.parent == base
        assert path.is_relative_to(base)


def test_resolved_name_must_match_what_was_asked_for(tmp_path):
    """
    Windows strips trailing dots and spaces from path components, so a name
    ending in one would be created under a different name and never found
    again. Refused rather than silently renamed.
    """
    with pytest.raises(UnsafeStudentPathError):
        student_dataset_path("23-1-1-0559", "Jose Jr.", dataset_dir=tmp_path)


# ---------------------------------------------------------------------------
# Real names must keep working
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("student_id", "name"), LIVE_STUDENTS)
def test_the_three_enrolled_students_are_accepted(student_id, name):
    assert validate_student_id(student_id) == student_id
    assert validate_student_name(name) == name


def test_apostrophes_are_accepted():
    """`O'Brien` is a name, not an injection attempt (see US-5)."""
    assert validate_student_name("Siobhan O'Brien") == "Siobhan O'Brien"


def test_non_ascii_names_are_accepted():
    """Filipino names carry diacritics; an ASCII-only rule excludes people."""
    assert validate_student_name("Maria Peña") == "Maria Peña"


def test_folder_format_is_unchanged(tmp_path):
    """
    train_model.parse_dataset_folder() and
    recognize_face.load_model_and_labels() both split this on the first
    underscore. Changing the format would invalidate the trained model.
    """
    assert student_folder_name("23-1-1-0920", "Julio B. Falloran Jr") == (
        "23-1-1-0920_Julio B. Falloran Jr"
    )

    path = student_dataset_path("23-1-1-0920", "Julio B. Falloran Jr", dataset_dir=tmp_path)
    assert path.name == "23-1-1-0920_Julio B. Falloran Jr"


def test_underscores_are_refused_in_the_student_id():
    """
    Not cosmetic: the folder is `{id}_{name}` and both readers split on the
    *first* underscore, so an ID containing one silently moves part of the ID
    into the name and corrupts the label map.
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

CALLERS = ["app.py", "capture_dataset.py"]


@pytest.mark.parametrize("module_name", CALLERS)
def test_callers_use_the_shared_helper(module_name):
    """
    A validator nothing calls fixes nothing. Both modules that build a
    dataset path must import it from security.paths.
    """
    source = (PROJECT_ROOT / module_name).read_text(encoding="utf-8")

    assert "security.paths" in source, (
        f"{module_name} builds dataset paths but does not import "
        "security.paths - SE-3 would be open again."
    )


@pytest.mark.parametrize("module_name", CALLERS)
def test_no_caller_joins_a_dataset_path_by_hand(module_name):
    """
    Guards the *class* of bug rather than the five instances that existed,
    in the spirit of test_no_import_shadowing.py.

    The vulnerable pattern was a literal directory name joined onto a
    user-derived folder name:

        os.path.join("dataset", f"{student_id}_{name}")

    Anything of that shape bypasses the containment check, so it is banned
    outright at source level. Build the path with student_dataset_path().
    """
    path = PROJECT_ROOT / module_name
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
        f"{module_name} joins a dataset path by hand, which skips the "
        "containment check (SE-3):\n  " + "\n  ".join(offenders)
    )
