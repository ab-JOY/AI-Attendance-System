"""
The Excel export, against a real register (FS-11, PE-8).

**Why this is an integration test and not a unit test with a fake cursor.**
The two things most likely to break here are both database behaviours, and a
mock would be built from my assumptions about them rather than from what the
server does:

* MySQL returns a `TIME` column as a `datetime.timedelta`. pandas stringified
  it on the way past; openpyxl raises on it. A hand-made row with the string
  `"08:00:00"` in it would pass a test and produce a 500 in production - which
  is the FS-8 mistake one layer along, where a unit test with a hand-made
  `time_in` missed the same conversion.
* The filters have to select the same rows as the screen the operator exported
  from, and "the same rows" is a question about the query.

Every identifier here is an `INT-TEST-*` scratch value in a scratch schema -
see `conftest.py`'s safety gate.
"""

from __future__ import annotations

import io

import pytest
from openpyxl import load_workbook

from tests.integration.conftest import (
    make_attendance,
    make_enrolment,
    make_student,
    make_subject,
    sign_in_as,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def register(scratch_database):
    """
    Two students on one subject, one Present and one Late, plus a second
    subject with one row so the filters have something to exclude.
    """
    from infra.db import db_cursor

    with db_cursor(dictionary=True, commit=True) as cursor:
        first = make_student(cursor, "INT-TEST-0001", name="Ana Reyes")
        second = make_student(cursor, "INT-TEST-0002", name="Ben Cruz")
        other = make_student(cursor, "INT-TEST-0003", name="Cara Diaz")

        subject = make_subject(cursor, subject_code="INT-TEST-101")
        elsewhere = make_subject(cursor, subject_code="INT-TEST-202", section="A")

        for student_id, subject_id in (
            (first, subject),
            (second, subject),
            (other, elsewhere),
        ):
            make_enrolment(cursor, student_id, subject_id)

        make_attendance(cursor, first, subject, status="Present", time_in="08:00:00")
        make_attendance(cursor, second, subject, status="Late", time_in="08:31:00")
        make_attendance(cursor, other, elsewhere, status="Present", time_in="09:05:00")

    return {"subject_id": subject, "other_subject_id": elsewhere}


def workbook_from(response):
    assert response.status_code == 200, response.status_code
    return load_workbook(io.BytesIO(response.data))


def rows_of(response):
    """`[(header...), (row...)]` from the exported sheet."""
    sheet = workbook_from(response).active
    return [tuple(cell.value for cell in row) for row in sheet.iter_rows()]


def test_the_export_is_a_real_workbook_with_a_header(client, register):
    sign_in_as(client)

    rows = rows_of(client.get("/export_excel"))

    assert rows[0] == (
        "Date", "Student ID", "Student", "Subject", "Section", "Time In", "Status",
    )


def test_every_register_row_is_exported(client, register):
    sign_in_as(client)

    rows = rows_of(client.get("/export_excel"))

    assert len(rows) == 4, "header plus three attendance rows"

    exported_ids = {row[1] for row in rows[1:]}
    assert exported_ids == {"INT-TEST-0001", "INT-TEST-0002", "INT-TEST-0003"}


def test_the_subject_filter_is_honoured(client, register):
    """
    FS-11's original defect: every filter was ignored and the whole table was
    dumped, so the file never matched the screen it was exported from.
    """
    sign_in_as(client)

    rows = rows_of(
        client.get(f"/export_excel?subject={register['subject_id']}")
    )

    exported_ids = {row[1] for row in rows[1:]}

    assert exported_ids == {"INT-TEST-0001", "INT-TEST-0002"}
    assert "INT-TEST-0003" not in exported_ids, (
        "A row from another subject reached a filtered export."
    )


def test_a_filter_matching_nothing_exports_only_the_header(client, register):
    sign_in_as(client)

    rows = rows_of(client.get("/export_excel?date=1999-01-01"))

    assert len(rows) == 1


def test_the_time_column_survives_the_round_trip(client, register):
    """
    ⚠️ **MySQL hands back a TIME column as a `timedelta`.**

    openpyxl refuses to write one, so without an explicit conversion this route
    is a 500 for any register that has a time in it - which is every register.
    A unit test holding the string "08:00:00" would never see it.
    """
    sign_in_as(client)

    rows = rows_of(client.get("/export_excel"))

    times = {row[5] for row in rows[1:]}

    assert times == {"08:00:00", "08:31:00", "09:05:00"}, times


def test_the_status_column_carries_the_three_real_statuses(client, register):
    sign_in_as(client)

    rows = rows_of(client.get("/export_excel"))
    statuses = sorted({row[6] for row in rows[1:]})

    assert statuses == ["Late", "Present", "Present"] or set(statuses) == {
        "Late", "Present"
    }


def test_the_export_is_offered_as_a_download(client, register):
    sign_in_as(client)

    response = client.get("/export_excel")

    assert response.mimetype == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert "attachment" in response.headers["Content-Disposition"]
    assert ".xlsx" in response.headers["Content-Disposition"]


def test_nothing_is_written_to_disk(client, register):
    """
    ⚠️ **This test found a real leak and is the reason the export is a buffer.**

    The export used to keep a timestamped copy of the register under `reports/`
    for ever - an accumulating pile of attendance data that
    `docs/data_privacy.md` has no retention answer for. The first Phase 5
    version replaced that with a temporary file deleted by a `call_on_close`
    hook; **the hook does not fire under the test client**, so every export left
    a full register in the system temp directory. The workbook is built in
    memory now and there is no cleanup path to get wrong.

    Watching the temp directory rather than `reports/` is deliberate: the
    finding was not "reports/ is the wrong folder", it was "an export must not
    outlive the response".
    """
    import tempfile
    from pathlib import Path

    temp_root = Path(tempfile.gettempdir())

    before = set(temp_root.glob("attendance_report_*"))
    before |= set(Path.cwd().glob("attendance_report_*"))

    sign_in_as(client)

    response = client.get("/export_excel")
    assert response.data
    response.close()

    after = set(temp_root.glob("attendance_report_*"))
    after |= set(Path.cwd().glob("attendance_report_*"))

    assert after == before, (
        f"The export left {sorted(after - before)} on disk. An attendance "
        "register is sensitive personal information under RA 10173; it is "
        "sent to the browser, not filed on the server."
    )
