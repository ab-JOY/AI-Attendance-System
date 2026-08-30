"""
FS-10 against a real server: the correction, and the record of it.

`tests/test_attendance_correction.py` covers the validation and the structural
properties with no database. What needs a server is the part FS-10 is actually
about: does the status change and its audit row commit together, and - the
question that decides whether the audit trail means anything - does the status
change roll back when the audit row cannot be written.

Migration 006 states the requirement it was created for: an attendance system
where a human can silently change a biometric determination is worse than one
where they cannot change it at all. A correction without its audit row is that
silent change. So the interesting test here is the failure path, not the happy
one.
"""

from __future__ import annotations

import mysql.connector
import pytest

from infra.db import db_cursor

from .conftest import (
    make_attendance,
    make_enrolment,
    make_student,
    make_subject,
    sign_in_as,
)

pytestmark = pytest.mark.integration

STUDENT = "INT-TEST-0001"


@pytest.fixture
def absent_record():
    """
    One student marked Absent - the false negative FS-10 exists to correct.
    Returns the attendance row id.
    """
    with db_cursor(commit=True) as cursor:
        subject_id = make_subject(cursor)
        make_student(cursor, STUDENT)
        make_enrolment(cursor, STUDENT, subject_id)
        return make_attendance(cursor, STUDENT, subject_id, status="Absent")


def status_of(attendance_id):
    with db_cursor() as cursor:
        cursor.execute("SELECT status FROM attendance WHERE id = %s", (attendance_id,))
        row = cursor.fetchone()
        return row[0] if row else None


def audit_rows(attendance_id):
    with db_cursor(dictionary=True) as cursor:
        cursor.execute(
            "SELECT * FROM attendance_audit WHERE attendance_id = %s ORDER BY id",
            (attendance_id,),
        )
        return cursor.fetchall()


# ===========================================================================
# The correction itself
# ===========================================================================


def test_correcting_an_absent_to_present_updates_the_register(client, absent_record):
    sign_in_as(client, "instructor")

    response = client.post(
        f"/attendance/{absent_record}/correct",
        data={"status": "Present", "reason": "Was in class; camera did not see them"},
    )

    assert response.status_code == 302, "A successful correction should redirect (PRG)"
    assert status_of(absent_record) == "Present"


def test_the_correction_is_recorded_with_who_when_and_why(client, absent_record):
    """
    Every field migration 006 created, populated. `changed_by` is a username
    string rather than a foreign key on purpose: an audit trail that
    disappears when the account is deleted is not an audit trail.
    """
    sign_in_as(client, "instructor")

    client.post(
        f"/attendance/{absent_record}/correct",
        data={"status": "Late", "reason": "Arrived at 08:20, signed the paper register"},
    )

    entries = audit_rows(absent_record)

    assert len(entries) == 1

    entry = entries[0]
    assert entry["old_status"] == "Absent"
    assert entry["new_status"] == "Late"
    assert entry["reason"] == "Arrived at 08:20, signed the paper register"
    assert entry["changed_by"] == "int-test-instructor"
    assert entry["changed_at"] is not None


def test_the_reason_is_stored_whole(client, absent_record):
    """
    255 characters exactly. With no STRICT_TRANS_TABLES a longer value would
    be truncated silently, so this proves the boundary the Python check
    defends is the real column width and not an approximation of it.
    """
    from web.sessions import MAX_CORRECTION_REASON

    reason = "R" * MAX_CORRECTION_REASON

    sign_in_as(client)
    client.post(
        f"/attendance/{absent_record}/correct",
        data={"status": "Present", "reason": reason},
    )

    stored = audit_rows(absent_record)[0]["reason"]

    assert stored == reason
    assert len(stored) == MAX_CORRECTION_REASON


def test_successive_corrections_accumulate_rather_than_overwrite(client, absent_record):
    """
    The history is the point. A record corrected twice must show both, in
    order, each naming what it changed from.
    """
    sign_in_as(client)

    client.post(
        f"/attendance/{absent_record}/correct",
        data={"status": "Present", "reason": "First correction"},
    )
    client.post(
        f"/attendance/{absent_record}/correct",
        data={"status": "Late", "reason": "Second correction, register showed 08:20"},
    )

    entries = audit_rows(absent_record)

    assert [(e["old_status"], e["new_status"]) for e in entries] == [
        ("Absent", "Present"),
        ("Present", "Late"),
    ]
    assert status_of(absent_record) == "Late"


def test_correcting_to_the_status_it_already_has_records_nothing(client, absent_record):
    """
    An audit row reading "changed Absent to Absent" is noise in the one
    register that has to stay readable. The operator is told, rather than
    given a success for a change that did not happen.
    """
    sign_in_as(client)

    response = client.post(
        f"/attendance/{absent_record}/correct",
        data={"status": "Absent", "reason": "No change intended"},
    )

    assert response.status_code == 400
    assert audit_rows(absent_record) == []
    assert status_of(absent_record) == "Absent"


# ===========================================================================
# ⚠️ The failure path - the test this file exists for
# ===========================================================================


def test_the_status_change_rolls_back_if_the_audit_row_cannot_be_written(
    client, absent_record, monkeypatch
):
    """
    **A correction that is not recorded must not happen at all.**

    Both writes are in one `db_cursor(commit=True)` block, which is one
    transaction (handover-phase-4 §1.7), so a failing audit insert must take
    the status change with it. If someone later splits them into two blocks
    both statements still "work" and this property disappears silently - which
    is why it is asserted rather than assumed.

    The failure is injected by making the audit table unwritable for the
    duration, rather than by patching application code, so what is under test
    is the real transaction boundary.
    """
    # The blueprint's own binding, not infra.db's. `from infra.db import
    # db_cursor` copies the name into the importing module, so patching the
    # source would leave the route holding the original - the correction route
    # moved to web/sessions.py in Phase 5.
    import web.sessions as sessions_module

    original_execute = sessions_module.db_cursor

    class FailingAuditCursor:
        """Passes everything through except the audit insert, which raises."""

        def __init__(self, inner):
            self._inner = inner

        def execute(self, statement, params=None):
            if "attendance_audit" in statement.lower():
                raise mysql.connector.Error(msg="injected audit failure", errno=1146)
            return self._inner.execute(statement, params)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    import contextlib

    @contextlib.contextmanager
    def failing_db_cursor(*args, **kwargs):
        with original_execute(*args, **kwargs) as cursor:
            yield FailingAuditCursor(cursor)

    monkeypatch.setattr(sessions_module, "db_cursor", failing_db_cursor)

    sign_in_as(client)

    response = client.post(
        f"/attendance/{absent_record}/correct",
        data={"status": "Present", "reason": "This correction must not survive"},
    )

    assert response.status_code == 500

    # The whole point. Without a shared transaction the register would now
    # read Present with nothing recording who changed it or why.
    assert status_of(absent_record) == "Absent", (
        "The status change survived an audit-insert failure. The correction "
        "and its audit row are no longer one transaction, so this system can "
        "now silently alter a biometric determination."
    )
    assert audit_rows(absent_record) == []


# ===========================================================================
# The screen
# ===========================================================================


def test_the_correction_screen_shows_the_record_and_its_history(client, absent_record):
    sign_in_as(client)

    client.post(
        f"/attendance/{absent_record}/correct",
        data={"status": "Present", "reason": "Recognised late, marked by hand"},
    )

    body = client.get(f"/attendance/{absent_record}/correct").get_data(as_text=True)

    assert "Integration Test Subject" in body
    assert STUDENT in body
    assert "Recognised late, marked by hand" in body
    assert "int-test-admin" in body


def test_a_record_that_does_not_exist_is_a_404(client):
    sign_in_as(client)

    assert client.get("/attendance/999999999/correct").status_code == 404
    assert client.post(
        "/attendance/999999999/correct",
        data={"status": "Present", "reason": "Nothing to correct"},
    ).status_code == 404


def test_the_reports_page_links_to_the_correction_screen(client, absent_record):
    """
    FS-10 is a screen the operator can reach, not just a route that exists.
    The register is where they are looking when they notice the miss.
    """
    sign_in_as(client)

    body = client.get("/reports").get_data(as_text=True)

    assert f"/attendance/{absent_record}/correct" in body
