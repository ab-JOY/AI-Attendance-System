"""
The single authority on where a student's dataset folder lives (SE-3).

Before this module, `student_id` and `name` came straight off an unvalidated
form, were stored, and were then concatenated into a path handed to
`shutil.rmtree()` and `os.rename()`:

    folder_name = f"{student['student_id']}_{student['name']}"
    shutil.rmtree(os.path.join("dataset", folder_name))

A student named `..\\..\\Windows\\Temp` therefore gave any authenticated user
directory deletion outside `dataset/`. The same construction appeared in four
places in app.py and once in capture_dataset.py, each free to drift.

Three defences, in order, and all three matter:

1. **Allowlist the ID and the name at the boundary.** Reject rather than
   quietly rewrite, so an operator who types something unusable is told,
   instead of finding an empty folder later.
2. **Resolve the path and prove containment.** Even if the allowlist is ever
   widened, a path that leaves `dataset/` is refused. Belt and braces: the
   allowlist is the rule, containment is the proof.
3. **One function, both sides.** app.py and capture_dataset.py call
   `student_dataset_path()`, so enrolment and management can no longer
   disagree about where a folder is.

The folder format is unchanged - `{student_id}_{student_name}` - so
`train_model.parse_dataset_folder()`, `recognize_face.load_model_and_labels()`
and the existing trained model all keep working. Replacing the format with a
surrogate key would also fix the "renaming a student orphans the folder"
coupling, but it needs a dataset migration and a retrain, so it is out of
scope here (see tasks/handover-phase-1.md 1.6).
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

from config.settings import settings

# Characters permitted in a student name in addition to letters and digits.
# Apostrophe is deliberate: `O'Brien` is a real name and rejecting it would be
# a usability bug dressed as a security control (see US-1/US-5).
_NAME_EXTRA_CHARS = frozenset(" .'-")

# Underscore is excluded from student IDs, and this is load-bearing rather
# than cautious. The folder is `{id}_{name}` and both
# train_model.parse_dataset_folder() and
# recognize_face.load_model_and_labels() split on the *first* underscore. An
# ID containing one would silently reassign part of the ID to the name and
# corrupt the label map.
_ID_EXTRA_CHARS = frozenset("-")

MAX_ID_LENGTH = 64
MAX_NAME_LENGTH = 128


class UnsafeStudentPathError(ValueError):
    """Raised when an ID or name cannot be turned into a safe dataset path."""


def validate_student_id(student_id: str | None) -> str:
    """
    Return the student ID unchanged, or raise UnsafeStudentPathError.

    Letters, digits and hyphens only - real IDs look like `23-1-1-0559`. This
    rules out `..`, path separators, drive letters, NUL bytes and every other
    traversal primitive by construction rather than by blocklist.
    """
    if student_id is None:
        raise UnsafeStudentPathError("Student ID is required.")

    value = student_id.strip()

    if not value:
        raise UnsafeStudentPathError("Student ID is required.")

    if len(value) > MAX_ID_LENGTH:
        raise UnsafeStudentPathError(
            f"Student ID must be at most {MAX_ID_LENGTH} characters."
        )

    if not (value[0].isalnum() and value[0].isascii()):
        raise UnsafeStudentPathError("Student ID must start with a letter or a digit.")

    for character in value:
        if character in _ID_EXTRA_CHARS:
            continue
        if character.isalnum() and character.isascii():
            continue
        raise UnsafeStudentPathError(
            "Student ID may contain only letters, digits and hyphens. "
            f"{character!r} is not allowed."
        )

    return value


def validate_student_name(name: str | None) -> str:
    """
    Return the student name with surrounding whitespace stripped, or raise.

    Letters and digits are tested with `str.isalnum()` rather than an ASCII
    pattern, so `Peña` and other non-ASCII names are accepted - this system is
    deployed in the Philippines and an ASCII-only rule would reject real
    students. Path separators, control characters and the Windows-reserved
    set are all non-alphanumeric and therefore refused.
    """
    if name is None:
        raise UnsafeStudentPathError("Student name is required.")

    # Normalise so that visually identical names cannot resolve to two
    # different folders depending on how they were typed.
    value = unicodedata.normalize("NFC", name).strip()

    if not value:
        raise UnsafeStudentPathError("Student name is required.")

    if len(value) > MAX_NAME_LENGTH:
        raise UnsafeStudentPathError(
            f"Student name must be at most {MAX_NAME_LENGTH} characters."
        )

    for character in value:
        if character in _NAME_EXTRA_CHARS:
            continue
        if character.isalnum():
            continue
        raise UnsafeStudentPathError(
            "Student name may contain only letters, digits, spaces, "
            f"periods, hyphens and apostrophes. {character!r} is not allowed."
        )

    # Windows silently strips trailing dots and spaces from path components,
    # so `Jose Jr.` would be created as `Jose Jr` and then never found again
    # by a lookup that rebuilt the name from the database. Refuse instead of
    # producing a folder whose name does not match the record.
    if value[-1] in ". ":
        raise UnsafeStudentPathError(
            "Student name may not end with a period or a space."
        )

    return value


def student_folder_name(student_id: str | None, name: str | None) -> str:
    """Validated `{student_id}_{student_name}` folder name."""
    return f"{validate_student_id(student_id)}_{validate_student_name(name)}"


def student_dataset_path(
    student_id: str | None,
    name: str | None,
    dataset_dir: Path | None = None,
) -> Path:
    """
    Absolute path to a student's dataset folder, proven to be inside
    `dataset/`.

    Raises UnsafeStudentPathError if the ID or name is not acceptable, or if
    the resolved path is anything other than a direct child of the dataset
    directory. Call this before any rmtree, rename or makedirs - it is the
    only sanctioned way to build one of these paths.
    """
    base = (dataset_dir if dataset_dir is not None else settings.dataset_dir).resolve()

    folder_name = student_folder_name(student_id, name)
    candidate = (base / folder_name).resolve()

    # `parent` rather than `is_relative_to`: a dataset folder is always a
    # direct child, so anything nested is as wrong as anything outside.
    if candidate.parent != base:
        raise UnsafeStudentPathError(
            f"Refusing a dataset path outside {base}: {candidate}"
        )

    # The operating system may normalise a component out from under us -
    # trailing dots and spaces on Windows, for instance. If what landed on
    # disk is not what was asked for, the caller's later lookups would miss.
    if candidate.name != folder_name:
        raise UnsafeStudentPathError(
            f"The filesystem rewrote {folder_name!r} as {candidate.name!r}."
        )

    return candidate
