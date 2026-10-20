"""
Where a student stands against one offering, for adding them to its class list.

A subject offering is taught to one section. Its own students are regular; a
student of the same program in another year level or section is irregular, and
added only when the operator says so; another program's student is refused.
The SQL itself is covered against a real database in
`tests/integration/test_academic_structure.py`; this pins the decision.

No database. Every identifier is fake (lessons.md L6).
"""

from __future__ import annotations

import pytest

from repositories import enrolments as enrolments_repo

SECTION_1A, SECTION_1B, SECTION_3A = 11, 12, 31
BSCS, BSN = 1, 2


class FakeCursor:
    def __init__(self, row):
        self.row = row

    def execute(self, sql, values):
        self.values = values

    def fetchone(self):
        return self.row


def placement(student_section, student_program, offering_section,
              offering_program, dictionary=True):
    row = (student_section, student_program, offering_section, offering_program)

    if dictionary:
        row = dict(zip(
            ("student_section", "student_program",
             "offering_section", "offering_program"),
            row,
            strict=True,
        ))

    return enrolments_repo.placement(FakeCursor(row), "TEST-0001", 99)


@pytest.mark.parametrize("dictionary", [True, False])
@pytest.mark.parametrize(
    "student, offering, expected",
    [
        ((SECTION_1A, BSCS), (SECTION_1A, BSCS), enrolments_repo.REGULAR),
        # Section B student, Section A subject.
        ((SECTION_1B, BSCS), (SECTION_1A, BSCS), enrolments_repo.IRREGULAR),
        # Third-year taking a first-year subject.
        ((SECTION_3A, BSCS), (SECTION_1A, BSCS), enrolments_repo.IRREGULAR),
        ((SECTION_1A, BSN), (SECTION_1B, BSCS), enrolments_repo.OTHER_PROGRAM),
        # No placement at all is no program, not every program.
        ((None, None), (SECTION_1A, BSCS), enrolments_repo.OTHER_PROGRAM),
        # An offering with no section has no block: everybody may take it.
        ((SECTION_1A, BSN), (None, None), enrolments_repo.UNLINKED),
        ((None, None), (None, None), enrolments_repo.UNLINKED),
    ],
)
def test_a_student_is_placed_against_the_offerings_section(
    student, offering, expected, dictionary,
):
    assert placement(*student, *offering, dictionary=dictionary) == expected


def test_a_missing_student_or_subject_is_none():
    assert enrolments_repo.placement(FakeCursor(None), "TEST-0001", 99) is None


def test_the_subject_and_student_reach_the_query_in_order():
    cursor = FakeCursor(None)

    enrolments_repo.placement(cursor, "TEST-0001", 99)

    assert cursor.values == (99, "TEST-0001")
