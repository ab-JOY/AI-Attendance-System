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

1. **Allowlist the ID at the boundary.** Reject rather than quietly rewrite,
   so an operator who types something unusable is told, instead of finding an
   empty folder later.
2. **Resolve the path and prove containment.** Even if the allowlist is ever
   widened, a path that leaves `dataset/` is refused. Belt and braces: the
   allowlist is the rule, containment is the proof.
3. **One function, every side.** Enrolment, management and the store all call
   `student_dataset_path()`, so nothing can disagree about where a folder is.

**The folder is `dataset/{student_id}`, and the name is not part of it
(Phase 5, todo.md §7.5).** It used to be `{student_id}_{student_name}`, which
made a student's *display name* load-bearing for their data: renaming
`Jose Cruz` to `Jose C. Cruz` in the database left the folder behind, and
delete, edit and recapture then silently operated on a path with nothing at
it. The record and the storage location are now coupled only through the key
that cannot change.

Two consequences worth knowing before touching this:

* `trainer/labels.txt` stores `label,student_id`, so the display name on the
  recognition overlay comes from the `students` table, not from the filesystem
  (`recognize_face.load_model_and_labels()`).
* `validate_student_name()` is still here and still used. Names are stored and
  rendered even though they no longer become paths, and the allowlist is what
  keeps a hostile one out of the database in the first place.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

from config.settings import settings

# Characters permitted in a student name in addition to letters and digits.
# Apostrophe is deliberate: `O'Brien` is a real name and rejecting it would be
# a usability bug dressed as a security control (see US-1/US-5).
_NAME_EXTRA_CHARS = frozenset(" .'-")

# Underscore is excluded from student IDs. It was load-bearing under the old
# `{id}_{name}` folder scheme, where an underscore in the ID would silently
# reassign part of it to the name and corrupt the label map. That scheme is
# gone, but the restriction stays: `dataset/` still holds folders written under
# the old convention on machines that have not run the rename, and an ID
# containing `_` would be ambiguous against them. Real IDs look like
# `23-1-1-0559`; nothing legitimate needs it.
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

    ⚠️ **A name may end in a period, and that was a real rejection.** Reported
    by the user 2026-08-29: `Juan Dela Cruz Jr.` was refused, and with it every
    `Jr.` and `Sr.` - the two commonest suffixes in Filipino names.

    Measured rather than assumed: the rule was on the **last** character, so
    `Ma. Teresa Santos` and `Jose P. Rizal` were always accepted. A name is
    refused only when the period ends it, which is exactly the suffix case.

    The rule that refused them was correct **when it was written and is not
    now.** Under the old `dataset/{student_id}_{student_name}` scheme the name
    was a path component, and Windows silently strips trailing dots from those:
    `Jose Jr.` would have been created as `Jose Jr` and never found again by a
    lookup that rebuilt the path from the database. Phase 5 made the folder
    `dataset/{student_id}` - see this module's header - so **the name has not
    been part of any path since**, and the rule went on rejecting real students
    to protect a filesystem behaviour nothing here can reach any more.

    What replaces it is a rule about names rather than about paths: a name has
    to contain at least one letter or digit. That refuses `.`, `..` and `...`,
    which are the only inputs the old check was still usefully catching, and it
    refuses them because they are not names.

    ⚠️ **`validate_student_id()` still refuses a trailing dot, and must.** An
    ID *is* the folder name. Do not "make the two consistent".
    """
    if name is None:
        raise UnsafeStudentPathError("Student name is required.")

    # Normalise so that two visually identical names are one value however
    # they were typed - `Peña` composed and decomposed are different strings
    # that render the same, and a search or a duplicate check would disagree
    # with the operator's eyes.
    #
    # ⚠️ The reason used to be stated as "cannot resolve to two different
    # folders", which stopped being true with the §7.5 migration. The rule is
    # still right; only its justification had expired. That is exactly how the
    # trailing-period rule above survived long enough to reject `Jr.` - see
    # lessons.md L30 - so it is restated here rather than left to rot.
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

    # A string of punctuation is not a name. This is what stops `.`, `..` and
    # `...` - which the allowlist above permits, since a period is a legitimate
    # character inside a name - without saying anything about filesystems.
    if not any(character.isalnum() for character in value):
        raise UnsafeStudentPathError(
            "Student name must contain at least one letter or digit."
        )

    return value


def student_folder_name(student_id: str | None) -> str:
    """
    Validated `{student_id}` folder name.

    ⚠️ This took a `name` argument until Phase 5 and returned
    `{student_id}_{name}`. If you are reading a caller that still passes two
    arguments, it is out of date - the name never belonged in a path.
    """
    return validate_student_id(student_id)


def student_dataset_path(
    student_id: str | None,
    dataset_dir: Path | None = None,
) -> Path:
    """
    Absolute path to a student's dataset folder, proven to be inside
    `dataset/`.

    Raises UnsafeStudentPathError if the ID is not acceptable, or if the
    resolved path is anything other than a direct child of the dataset
    directory. Call this before any rmtree, rename or makedirs - it is the
    only sanctioned way to build one of these paths.
    """
    base = (dataset_dir if dataset_dir is not None else settings.dataset_dir).resolve()

    folder_name = student_folder_name(student_id)
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


def student_staging_path(
    student_id: str | None,
    staging_dir: Path | None = None,
) -> Path:
    """
    Absolute path to a student's *in-progress* capture folder.

    Browser enrolment writes here and the folder is promoted into `dataset/`
    only when it is complete (FS-9). Same containment proof as
    `student_dataset_path()`, against the staging root instead - the images
    arriving here come from an HTTP request, so this is if anything the more
    exposed of the two.
    """
    base = (
        staging_dir if staging_dir is not None else settings.dataset_staging_dir
    ).resolve()

    return student_dataset_path(student_id, dataset_dir=base)


def student_image_path(folder: Path, index: int, total: int) -> Path:
    """
    Absolute path to image `index` inside an already-validated `folder`.

    ⚠️ **The index is never taken from a request.** Browser enrolment posts a
    frame and the server decides which image number it is, from a counter it
    owns. Accepting a client-supplied filename - or a client-supplied index -
    would hand an authenticated caller a write primitive pointed at a path
    this module exists to constrain.

    `total` is passed rather than assumed so the bound is the plan's, not a
    constant that could drift from it.
    """
    if not isinstance(index, int) or isinstance(index, bool):
        raise UnsafeStudentPathError(f"Image index must be an integer, got {index!r}")

    if not 1 <= index <= total:
        raise UnsafeStudentPathError(
            f"Image index {index} is outside 1..{total}"
        )

    return folder / f"{index}.jpg"
