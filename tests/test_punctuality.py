"""
The grace window between a class's start and Late (FS-8).

Reported by the user 2026-10-16: students recognised minutes - in one
measured case twenty seconds - after the scheduled start were recorded Late.
PRMSU's rule is a 15-minute window for a one-hour class and 30 minutes for a
one-and-a-half-hour class.

Times are passed as timedeltas where they come from the database, because
that is what MySQL hands back for a TIME, and as `datetime.time` where they
come from the clock - the two shapes `save_attendance()` combines.
"""

from datetime import time, timedelta

import pytest

from services.punctuality import derive_status, grace_period, late_after


def hm(hours, minutes=0):
    """A TIME as MySQL returns it."""
    return timedelta(hours=hours, minutes=minutes)


ONE_HOUR = (hm(7, 30), hm(8, 30))
ONE_AND_A_HALF = (hm(7, 30), hm(9, 0))


@pytest.mark.parametrize(
    ("arrival", "expected"),
    [
        (time(7, 1), "Present"),        # before the start - the user's example
        (time(7, 30), "Present"),       # with the bell
        (time(7, 30, 20), "Present"),   # the measured 20-seconds-in Late
        (time(7, 45, 0), "Present"),    # the last second of the window
        (time(7, 45, 1), "Late"),       # past it
        (time(8, 10), "Late"),
    ],
)
def test_a_one_hour_class_gives_fifteen_minutes(arrival, expected):
    assert derive_status(arrival, *ONE_HOUR) == expected


@pytest.mark.parametrize(
    ("arrival", "expected"),
    [
        (time(7, 1), "Present"),
        (time(7, 46), "Present"),       # Late under the one-hour rule
        (time(8, 0, 0), "Present"),     # the last second of the window
        (time(8, 0, 1), "Late"),
    ],
)
def test_a_one_and_a_half_hour_class_gives_thirty_minutes(arrival, expected):
    assert derive_status(arrival, *ONE_AND_A_HALF) == expected


@pytest.mark.parametrize(
    ("length", "grace"),
    [
        (hm(0, 50), 15),
        (hm(1, 0), 15),
        (hm(1, 29), 15),
        (hm(1, 30), 30),
        (hm(3, 0), 30),
    ],
)
def test_the_window_follows_the_length_of_the_class(length, grace):
    start = hm(7, 30)

    assert grace_period(start, start + length) == timedelta(minutes=grace)


@pytest.mark.parametrize(
    "end",
    [None, hm(7, 30), hm(7, 0)],
    ids=["no end", "ends as it starts", "ends before it starts"],
)
def test_a_class_of_unknown_length_gets_the_short_window(end):
    assert grace_period(hm(7, 30), end) == timedelta(minutes=15)


def test_a_subject_with_no_scheduled_start_makes_nobody_late():
    assert derive_status(time(23, 59), None, None) == "Present"


def test_the_clock_and_the_database_shapes_agree():
    """`time` from the form or the clock, `timedelta` from MySQL."""
    assert late_after(time(7, 30), time(8, 30)) == late_after(*ONE_HOUR)


def test_a_window_running_past_midnight_does_not_raise():
    """A `time` cannot hold 24:05; the comparison has to survive it."""
    assert derive_status(time(23, 59), hm(23, 50), hm(23, 59)) == "Present"
