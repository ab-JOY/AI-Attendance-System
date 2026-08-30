"""
Tests for train_model.parse_dataset_folder() and its migration guard.

This function decides which directories under `dataset/` become trained
identities, and its output feeds labels.txt.

**It changed shape in Phase 5 (todo.md §7.5).** A dataset folder used to be
`{student_id}_{student_name}` and this returned the pair; it is now
`{student_id}` and this returns the ID. The old scheme made a *display name*
load-bearing for storage - renaming a student in the database orphaned their
images, and delete, edit and recapture then operated on a path with nothing at
it.

Two behaviours are worth testing rather than one:

* what parses, which is now exactly `security.paths.validate_student_id`, so
  the module that decides what may become a path is the module that decides
  what a folder may be called; and
* what a *pre-migration* folder does, because "unusable" and "you have not run
  the rename yet" need different answers. The second is a documented one-line
  fix and train_model.py refuses the whole run over it rather than quietly
  training a model that is missing people.
"""

import pytest

from train_model import looks_like_a_pre_migration_folder, parse_dataset_folder


@pytest.mark.parametrize(
    "folder_name",
    [
        # The three identities actually enrolled in this system.
        "23-1-1-0559",
        "23-1-1-0918",
        "23-1-1-0920",
        # Plain numeric and plain alphanumeric IDs are both fine.
        "12345",
        "STU0001",
    ],
)
def test_a_folder_named_for_a_student_id_parses(folder_name):
    assert parse_dataset_folder(folder_name) == folder_name


def test_surrounding_whitespace_is_stripped():
    assert parse_dataset_folder("  23-1-1-0002  ") == "23-1-1-0002"


@pytest.mark.parametrize(
    "folder_name",
    [
        "23-1-1-0559_Chrizol D. Evangelista",  # the old scheme
        "not a student id",  # spaces
        "../escape",  # traversal
        "23-1-1-0559.previous",  # an interrupted promotion's leftover
        "",  # empty
        "   ",  # whitespace only
    ],
)
def test_anything_else_returns_none(folder_name):
    """
    Returning None is what makes train_model() skip the folder rather than
    crash the whole run - or, for the pre-migration case below, refuse it by
    name.
    """
    assert parse_dataset_folder(folder_name) is None


# ---------------------------------------------------------------------------
# Telling "not migrated yet" apart from "not a dataset folder"
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "folder_name",
    [
        "23-1-1-0559_Chrizol D. Evangelista",
        "23-1-1-0001_Mary_Jane Cruz",  # a name may itself contain underscores
        "12345_test student",
    ],
)
def test_old_scheme_folders_are_recognised_as_pre_migration(folder_name):
    assert looks_like_a_pre_migration_folder(folder_name) is True


@pytest.mark.parametrize(
    "folder_name",
    [
        "23-1-1-0559",  # already migrated
        "_Chrizol D. Evangelista",  # no ID before the underscore
        "23-1-1-0559_",  # nothing after it
        "23-1-1-0559_   ",  # only whitespace after it
        "../escape_Someone",  # unusable ID; not our old scheme
        "",
    ],
)
def test_everything_else_is_not_pre_migration(folder_name):
    """
    A false positive here would tell an operator to run the rename script over
    a folder it will not touch, which is a confusing dead end. A false negative
    downgrades a refusal into a silently smaller model, which is worse - hence
    the asymmetry in what this accepts.
    """
    assert looks_like_a_pre_migration_folder(folder_name) is False
