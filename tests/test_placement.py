"""
Where a student belongs, as a form submits it (migration 009, DM-2).

`web/placement.py` is the one place three routes agree on what a valid
placement is: web registration, the edit screen and the mobile API. No
database - every case here is decided before a query.
"""

from __future__ import annotations

import pytest

from repositories import academics as academics_repo
from web.placement import optional_id, parse_placement

COMPLETE = {"program_id": "5", "year_level": "4", "section": "B"}


def test_a_complete_placement_is_parsed_into_typed_values():
    placement, problem = parse_placement(COMPLETE)

    assert problem is None
    assert placement == {"program_id": 5, "year_level": 4, "section": "B"}


def test_the_section_is_trimmed():
    """Padding is how free text differs, and `B ` must not be a second `B`."""
    placement, _problem = parse_placement(dict(COMPLETE, section="  B  "))

    assert placement["section"] == "B"


@pytest.mark.parametrize(
    ("field", "value", "names"),
    [
        ("program_id", "", "program"),
        ("program_id", "BSCS", "program"),       # a code where an id belongs
        ("program_id", "5 OR 1=1", "program"),
        ("year_level", "", "year level"),
        ("year_level", "0", "year level"),
        ("year_level", "40", "year level"),
        ("year_level", "fourth", "year level"),
        ("section", "", "Section"),
        ("section", "   ", "Section"),
    ],
)
def test_an_unusable_field_is_refused_and_named(field, value, names):
    """
    Refused with a message that says which field, the same standard every
    other form route here holds itself to.
    """
    placement, problem = parse_placement(dict(COMPLETE, **{field: value}))

    assert placement is None
    assert names in problem


def test_a_section_name_longer_than_its_column_is_refused():
    """
    This server has no STRICT_TRANS_TABLES, so the column would truncate an
    over-long name on an INSERT that reports success (lessons.md L11). The
    check is here because the column will not make it.
    """
    at_the_limit = "B" * academics_repo.MAX_SECTION_NAME

    assert parse_placement(dict(COMPLETE, section=at_the_limit))[1] is None

    placement, problem = parse_placement(dict(COMPLETE, section=at_the_limit + "B"))

    assert placement is None
    assert str(academics_repo.MAX_SECTION_NAME) in problem


def test_a_placement_that_came_back_from_the_browser_as_json_is_accepted():
    """
    `/enrol/finish` parses the record that round-tripped through the capture
    page, where the values are JSON numbers rather than form strings.
    """
    placement, problem = parse_placement(
        {"program_id": 5, "year_level": 4, "section": "B", "subject_ids": [3]}
    )

    assert problem is None
    assert placement == {"program_id": 5, "year_level": 4, "section": "B"}


def test_a_recapture_record_has_no_placement():
    """Recapture carries `{}`: it replaces a face, not where somebody belongs."""
    placement, problem = parse_placement({})

    assert placement is None
    assert problem


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("7", 7),
        (" 7 ", 7),
        (7, 7),
        ("", None),
        (None, None),
        ("-1", None),
        ("7; DROP TABLE students", None),
        ("seven", None),
    ],
)
def test_a_filter_is_an_integer_or_nothing(value, expected):
    assert optional_id(value) == expected
