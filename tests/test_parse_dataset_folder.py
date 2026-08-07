"""
Tests for train_model.parse_dataset_folder().

This function decides which directories under dataset/ become trained
identities, and its output feeds labels.txt. `app.py` derives the same
`{student_id}_{name}` path shape from database values, so the split rule
here and the path built there have to agree - a disagreement silently breaks
delete, edit and recapture for that student.

The underscore-in-name case is the one that matters most: real names in this
dataset contain spaces and punctuation, and student IDs contain hyphens, so
the split must be on the FIRST underscore only.
"""

import pytest

from train_model import parse_dataset_folder


@pytest.mark.parametrize(
    "folder_name, expected",
    [
        # The three identities actually enrolled in this system.
        (
            "23-1-1-0559_Chrizol D. Evangelista",
            ("23-1-1-0559", "Chrizol D. Evangelista"),
        ),
        (
            "23-1-1-0918_Jeff Christian P. Alcasid",
            ("23-1-1-0918", "Jeff Christian P. Alcasid"),
        ),
        (
            "23-1-1-0920_Julio B. Falloran Jr",
            ("23-1-1-0920", "Julio B. Falloran Jr"),
        ),
        # Split on the first underscore only - a name may contain more.
        ("23-1-1-0001_Mary_Jane Cruz", ("23-1-1-0001", "Mary_Jane Cruz")),
        # Surrounding whitespace is stripped from both halves.
        ("  23-1-1-0002_  Ana Reyes  ", ("23-1-1-0002", "Ana Reyes")),
    ],
)
def test_valid_folder_names_are_parsed(folder_name, expected):
    assert parse_dataset_folder(folder_name) == expected


@pytest.mark.parametrize(
    "folder_name",
    [
        "no-underscore-at-all",  # missing separator entirely
        "_Chrizol D. Evangelista",  # empty student ID
        "23-1-1-0559_",  # empty name
        "23-1-1-0559_   ",  # name is whitespace only
        "",  # empty string
    ],
)
def test_invalid_folder_names_return_none(folder_name):
    """
    Returning None is what makes train_model() skip the folder with a warning
    instead of crashing the whole run.
    """
    assert parse_dataset_folder(folder_name) is None
