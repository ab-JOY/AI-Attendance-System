"""
The Activity page's SQL, against a real scratch database (migration 013).

The test that matters most is the first: a sign-in recorded by the real
recorder must be *there* when read back on another connection. `db_cursor`
rolls back unless told `commit=True`, and the first version of the recorder
did not say so - every sign-in would have vanished with no error, and the
unit tests, which stub the database, passed.
"""

from __future__ import annotations

import pytest

from infra.db import db_cursor
from repositories import activity as activity_repo

from .conftest import make_student, make_subject, sign_in_as

pytestmark = pytest.mark.integration


def test_a_recorded_sign_in_is_committed(login_history_calls):
    login_history_calls.real("INS-001", "instructor", "success", "web")
    login_history_calls.real("nobody", None, "failed", "web")

    with db_cursor(dictionary=True) as cursor:
        rows = activity_repo.recent_logins(cursor)

    assert [(r["username"], r["role"], r["outcome"], r["channel"]) for r in rows] == [
        ("nobody", None, "failed", "web"),
        ("INS-001", "instructor", "success", "web"),
    ]


def test_sessions_carry_who_started_them_and_the_registers_counts():
    with db_cursor(commit=True) as cursor:
        subject_id = make_subject(cursor)
        for n in range(4):
            make_student(cursor, f"INT-TEST-000{n}")

        cursor.execute(
            """
            INSERT INTO attendance_sessions
                (subject_id, session_date, started_at, ended_at, started_by)
            VALUES (%s, CURDATE(), NOW(), NULL, 'Ana Cruz')
            """,
            (subject_id,),
        )
        session_id = cursor.lastrowid

        for n, status in enumerate(("Present", "Present", "Late", "Absent")):
            cursor.execute(
                """
                INSERT INTO attendance
                    (student_id, subject_id, session_id, attendance_date, time_in, status)
                VALUES (%s, %s, %s, CURDATE(), NULL, %s)
                """,
                (f"INT-TEST-000{n}", subject_id, session_id, status),
            )

    with db_cursor(dictionary=True) as cursor:
        (row,) = activity_repo.recent_sessions(cursor)

    assert row["started_by"] == "Ana Cruz"
    assert row["ended_at"] is None
    assert (row["present"], row["late"], row["absent"]) == (2, 1, 1)


def test_the_page_renders_from_the_real_tables(client, login_history_calls):
    login_history_calls.real("INS-001", "instructor", "success", "web")
    sign_in_as(client, "admin")

    response = client.get("/activity")

    assert response.status_code == 200
    assert "INS-001" in response.get_data(as_text=True)
