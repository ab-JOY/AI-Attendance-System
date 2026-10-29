"""
Face capture history on the Activity page (migration 014).

Capturing a face collects biometric data, and instructors can do it for their
own classes, so the administrator must be able to see who captured whose
face - including a capture that is running right now. Every way a capture
ends is recorded, and **recording never changes how it ends.**
No database here; tests/integration covers the SQL.
"""

from __future__ import annotations

import contextlib
from datetime import datetime
from types import SimpleNamespace

import pytest

from services import capture_history
from tests.test_enrolment_class_list import (  # noqa: F401 - fixtures
    NAME,
    STUDENT,
    StubCapture,
    client,
    finish,
    flask_app,
    sign_in,
    wired,
)
from tests.test_route_security import sign_in_as


def outcomes(calls):
    return [outcome for _session, outcome in calls]


# ---------------------------------------------------------------------------
# 1. Every way a capture ends is recorded
# ---------------------------------------------------------------------------


def test_a_completed_capture_is_recorded(client, monkeypatch, wired, capture_history_calls):
    sign_in(client)

    assert finish(client, monkeypatch, {"subject_ids": []}).status_code == 200
    assert outcomes(capture_history_calls) == ["completed"]


def test_a_capture_whose_images_cannot_be_saved_is_recorded_as_failed(
    client, monkeypatch, wired, capture_history_calls
):
    import web.enrolment as enrolment_module

    def broken_promote(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(enrolment_module.dataset_store, "promote", broken_promote)
    monkeypatch.setattr(enrolment_module.enrolment_slot, "abandon", lambda: None)
    sign_in(client)

    assert finish(client, monkeypatch, {"subject_ids": []}).status_code == 500
    assert outcomes(capture_history_calls) == ["failed"]


def test_a_cancelled_capture_is_recorded(client, monkeypatch, capture_history_calls):
    import web.enrolment as enrolment_module

    capture = StubCapture({})
    monkeypatch.setattr(enrolment_module.enrolment_slot, "current", lambda: capture)
    monkeypatch.setattr(enrolment_module.enrolment_slot, "abandon", lambda: None)
    monkeypatch.setattr(enrolment_module, "_owns_capture", lambda _c: True)
    sign_in(client)

    assert client.post("/enrol/cancel").get_json()["success"]
    assert capture_history_calls == [(capture, "cancelled")]


def test_an_abandoned_capture_is_recorded_when_it_is_swept(capture_history_calls):
    from services.enrolment import EnrolmentSlot

    capture = SimpleNamespace(student_id=STUDENT, age_seconds=10_000)
    registry = SimpleNamespace(current=capture)
    slot = EnrolmentSlot(registry=registry, idle_timeout_seconds=60)
    slot.abandon = lambda: setattr(registry, "current", None)

    assert slot.current() is None
    assert capture_history_calls == [(capture, "abandoned")]


# ---------------------------------------------------------------------------
# 2. What is recorded, and that recording cannot hurt a capture
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("record, added, kind", [
    ({"program_id": 1}, None, "New student"),
    ({}, None, "Recapture"),
    ({}, SimpleNamespace(number=3, condition="room"), "Added session 3 (room)"),
])
def test_the_kind_of_capture_is_described(record, added, kind):
    capture = SimpleNamespace(record=record, added_session=added)

    assert capture_history.describe(capture) == kind


def test_the_recorder_writes_who_whose_and_how_many(capture_history_calls, monkeypatch):
    written = {}

    class Cursor:
        def execute(self, _sql, params):
            written["params"] = params

    @contextlib.contextmanager
    def fake_cursor(*_args, commit=False, **_kwargs):
        written["commit"] = commit
        yield Cursor()

    monkeypatch.setattr(capture_history, "db_cursor", fake_cursor)
    capture = SimpleNamespace(
        student_id=STUDENT, student_name=NAME, started_by="Ana Cruz",
        record={}, added_session=None, captured=100,
    )

    capture_history_calls.real(capture, "completed")

    assert written["commit"] is True, "without commit the row is rolled back"
    assert written["params"] == (STUDENT, NAME, "Ana Cruz", "Recapture", "completed", 100)


def test_a_failing_history_write_is_swallowed_and_logged(capture_history_calls, monkeypatch, caplog):
    @contextlib.contextmanager
    def broken_cursor(*_args, **_kwargs):
        raise RuntimeError("Table 'capture_events' doesn't exist")
        yield

    monkeypatch.setattr(capture_history, "db_cursor", broken_cursor)

    capture_history_calls.real(
        SimpleNamespace(student_id=STUDENT, student_name=NAME, started_by="x",
                        record={}, added_session=None, captured=4),
        "cancelled",
    )

    assert f"Could not record a cancelled capture of '{STUDENT}'" in caplog.text


def test_a_failing_history_write_does_not_fail_a_completed_capture(
    client, monkeypatch, wired, capture_history_calls
):
    monkeypatch.setattr(capture_history, "record", capture_history_calls.real)

    @contextlib.contextmanager
    def broken_cursor(*_args, **_kwargs):
        raise RuntimeError("database is down")
        yield

    monkeypatch.setattr(capture_history, "db_cursor", broken_cursor)
    sign_in(client)

    assert finish(client, monkeypatch, {"subject_ids": []}).status_code == 200


# ---------------------------------------------------------------------------
# 3. The page
# ---------------------------------------------------------------------------


@pytest.fixture
def activity_data(monkeypatch):
    import web.activity as activity_module

    @contextlib.contextmanager
    def fake_cursor(*_args, **_kwargs):
        yield object()

    captures = [
        {"student_id": "23-1-1-0918", "student_name": "Jeff", "performed_by": "Ana Cruz",
         "kind": "Added session 3 (room)", "outcome": "completed", "images": 100,
         "occurred_at": datetime(2026, 10, 28, 21, 27)},
        {"student_id": "23-1-1-0559", "student_name": "Chrizol", "performed_by": "admin",
         "kind": "Recapture", "outcome": "cancelled", "images": 12,
         "occurred_at": datetime(2026, 10, 28, 21, 0)},
    ]
    monkeypatch.setattr(activity_module, "db_cursor", fake_cursor)
    monkeypatch.setattr(activity_module.activity_repo, "recent_logins", lambda _c: [])
    monkeypatch.setattr(activity_module.activity_repo, "recent_sessions", lambda _c: [])
    monkeypatch.setattr(activity_module.activity_repo, "recent_captures", lambda _c: captures)
    return captures


def test_the_admin_sees_who_captured_whose_face(client, activity_data):
    sign_in_as(client, "admin")

    page = client.get("/activity").get_data(as_text=True)

    assert "Jeff (23-1-1-0918)" in page and "Ana Cruz" in page
    assert "Added session 3 (room)" in page and "Completed" in page
    assert "Cancelled" in page
    assert "No face capture is running right now." in page


def test_a_capture_running_now_is_shown_live(client, activity_data, monkeypatch):
    import web.enrolment as enrolment_module

    running = SimpleNamespace(
        student_id="25-1-1-0526", student_name="Aleatrice", started_by="Ana Cruz",
        record={"program_id": 1}, added_session=None, captured=37,
        plan=SimpleNamespace(total=100), running_seconds=150,
    )
    monkeypatch.setattr(enrolment_module.enrolment_slot, "current", lambda: running)
    sign_in_as(client, "admin")

    page = client.get("/activity").get_data(as_text=True)

    assert "Capturing now" in page
    assert "Aleatrice" in page and "25-1-1-0526" in page
    assert "37 of 100 images" in page
    assert "started 2 min ago" in page
