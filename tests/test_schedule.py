"""
`services.schedule.meets_on()` - whether a subject meets on a given date.

User-reported 2026-10-17: a Monday-and-Wednesday subject could be started on
a Friday. Every date here is fixed, so the tests do not depend on the day
they run.
"""

from __future__ import annotations

from datetime import date

import pytest

from services.schedule import meets_on

MONDAY = date(2026, 10, 12)
WEDNESDAY = date(2026, 10, 14)
FRIDAY = date(2026, 10, 16)
SUNDAY = date(2026, 10, 18)


def test_the_reported_case_friday_is_not_a_monday_wednesday_class():
    assert meets_on("Monday, Wednesday", FRIDAY) is False


@pytest.mark.parametrize("day", [MONDAY, WEDNESDAY])
def test_each_stored_day_is_a_meeting_day(day):
    assert meets_on("Monday, Wednesday", day) is True


def test_a_single_day_subject():
    assert meets_on("Friday", FRIDAY) is True
    assert meets_on("Friday", MONDAY) is False


def test_sunday_is_never_offered_so_never_matches():
    assert meets_on("Monday, Wednesday", SUNDAY) is False


def test_case_and_spacing_do_not_matter():
    """A value typed by hand before the day boxes existed."""
    assert meets_on(" monday ,WEDNESDAY", WEDNESDAY) is True


@pytest.mark.parametrize("stored", [None, "", "  ", "MWF", "TBA"])
def test_a_schedule_that_cannot_be_read_is_not_refused(stored):
    """
    ⚠️ Saying no to a schedule this function cannot interpret would leave the
    class unable to run until someone edits it.
    """
    assert meets_on(stored, FRIDAY) is True
