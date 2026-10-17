"""
Which days a subject meets, and whether today is one of them.

`subjects.day` holds the ticked days of the subject form joined in week
order - "Monday, Wednesday" (`web/subjects.py::_days_from()`). It used to be
read only for display, so a Monday-and-Wednesday subject could be started on
a Friday and its whole class list recorded Absent for a class that never met
(user-reported, 2026-10-17). `/start-attendance` now asks `meets_on()` first.

Pure on purpose, like `services/punctuality.py`: no database, no clock of its
own, so a test passes the date and nothing has to import `recognize_face`.
"""

from __future__ import annotations

# The days a class can meet, in week order - the boxes the subject form
# offers. A PRMSU subject meets on two days at the same time, so the form
# offers one box per day and `subjects.day` (VARCHAR(100)) holds them joined.
WEEKDAYS = (
    'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday',
)
DAY_SEPARATOR = ', '

# Indexed by `date.weekday()`, which counts Monday as 0. Spelled out rather
# than taken from `calendar.day_name`, which follows the server's locale and
# would stop matching the stored English names on a Filipino-locale machine.
_DAY_NAMES = WEEKDAYS + ('Sunday',)


def meeting_days(stored):
    """The days in a stored `subjects.day`, as a list. `[]` for NULL."""
    return [day.strip() for day in (stored or '').split(',') if day.strip()]


def meets_on(stored, when):
    """
    Whether a subject whose `subjects.day` is `stored` meets on date `when`.

    ⚠️ **True when no day can be read from `stored`.** A subject saved before
    the day boxes existed may hold NULL, '' or free text such as "MWF". Saying
    no to it would make the class impossible to run until someone edits it,
    for a schedule this function cannot interpret - so it is not refused.
    """
    known = {
        day.title() for day in meeting_days(stored)
        if day.title() in _DAY_NAMES
    }

    if not known:
        return True

    return _DAY_NAMES[when.weekday()] in known
