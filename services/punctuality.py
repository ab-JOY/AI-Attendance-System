"""
Present or Late: the grace window a student has after a class starts (FS-8).

PRMSU's rule, as the user stated it on 2026-10-16:

- a **one-hour** class gives the student **15 minutes** after the scheduled
  start to be recorded Present;
- a **one-and-a-half-hour** class gives **30 minutes**;
- after the window, the student is Late.

⚠️ **There used to be no window at all.** `derive_status()` answered
`arrival > scheduled_start`, so a student recognised at 11:45:20 for an 11:45
class was recorded Late - twenty seconds in - and so was everyone who walked
in with the bell. Measured on the deployed register before this fix: THS2,
scheduled 11:45:00, a Late at 11:45:20.

Pure on purpose - no database, no camera. `recognize_face` costs ~9 seconds
to import, and `scripts/preflight.py` has to ask the same question.
"""

from __future__ import annotations

from datetime import timedelta

SHORT_CLASS_GRACE = timedelta(minutes=15)
LONG_CLASS_GRACE = timedelta(minutes=30)

# A class this long or longer gets the long window. The user named two
# lengths, 60 and 90 minutes; the cut sits at the second, so a 2- or 3-hour
# laboratory is treated as a long class (`todo.md` §7 - an open decision).
LONG_CLASS_FROM = timedelta(minutes=90)


def _since_midnight(value):
    """
    A TIME as a timedelta from midnight, or None.

    MySQL hands a TIME back as a timedelta already; a `datetime.time` comes
    from callers that read the clock. Working in timedeltas rather than
    `time` means a window that runs past midnight is still a comparison, not
    an exception.
    """
    if value is None or isinstance(value, timedelta):
        return value

    return timedelta(
        hours=value.hour, minutes=value.minute, seconds=value.second,
        microseconds=value.microsecond,
    )


def grace_period(scheduled_start, scheduled_end):
    """
    How long after the start a student is still Present.

    The short window when the end is missing or not after the start: a class
    whose length cannot be known gets the stricter of the two rules rather
    than the more generous one.
    """
    start = _since_midnight(scheduled_start)
    end = _since_midnight(scheduled_end)

    if start is None or end is None or end <= start:
        return SHORT_CLASS_GRACE

    if end - start >= LONG_CLASS_FROM:
        return LONG_CLASS_GRACE

    return SHORT_CLASS_GRACE


def late_after(scheduled_start, scheduled_end):
    """The last moment, from midnight, at which an arrival is Present."""
    return _since_midnight(scheduled_start) + grace_period(
        scheduled_start, scheduled_end
    )


def derive_status(arrival, scheduled_start, scheduled_end=None):
    """
    "Present" or "Late" for a student recognised at `arrival`.

    Present up to and including the end of the grace window - recognised at
    7:45:00 for a one-hour 7:30 class is Present, at 7:45:01 Late. Arriving
    before the start is Present.

    A subject with no scheduled start cannot make anyone late, so the answer
    is Present - that is a statement about missing data, not punctuality.
    """
    if scheduled_start is None:
        return "Present"

    if _since_midnight(arrival) > late_after(scheduled_start, scheduled_end):
        return "Late"

    return "Present"
