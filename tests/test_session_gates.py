"""
A session is refused on a subject with an empty class list (B3), and the End
control is locked to the running session (R3/FS-16).

Two guards on the same screen, both about a session that cannot produce the
register the operator thinks they are getting.

---

**B3.** FS-3 scopes recognition to `enrolments`: being in the database is not
being in the class, and without that check any recognised face is recorded
against whatever subject happens to be running. The consequence, when a subject
has **nobody** enrolled, is that every face is refused with "Not in this class"
and `/end-attendance` writes a register of nobody.

That is correct behaviour and it must not be relaxed. What was wrong is that
nothing said so - the audit found it by hand (`audit-demo-readiness.md` B3),
`scripts/preflight.py` finds it from the command line, and the operator found
it when a student was refused in front of them.

⚠️ **Built first as a warning that let the session start, then changed to a
refusal on the user's instruction (2026-08-30).** The reasoning for refusing:
the session cannot record a single row, and that is knowable in one query
before a session row, a camera or anybody's time is spent on it. The gate runs
**before** `open_session()`, so a refusal leaves nothing to undo.

---

**R3/FS-16.** The End form carries its own subject dropdown. While a session is
running there is exactly one subject it may legitimately carry, because the
register is written for the *session* - so a free choice there could only ever
produce a 409. It is locked to the running subject instead.

⚠️ **The lock is a convenience; the 409 is the guard.** A disabled control is
absent from a curl, a replayed POST or a browser with scripting off, and what
it stands in front of is a register written for a subject that was never
taught: referentially valid, factually wrong, silent.
`tests/test_end_attendance_subject.py` owns that guard and none of it is
weakened here - `test_the_server_still_refuses_a_mismatch_regardless_of_the_lock`
below states the relationship explicitly.

No database: every repository call is stubbed, and every identifier is fake
(lessons.md L6).
"""

from __future__ import annotations

import contextlib

import mysql.connector
import pytest

import web.sessions as sessions_module
from recognize_face import ActiveSubject
from repositories import enrolments as enrolments_repo
from repositories import subjects as subjects_repo

SUBJECT_ID = 1
SUBJECT_CODE = "TEST-101"
OTHER_SUBJECT_ID = 2


@pytest.fixture(scope="module")
def flask_app():
    import app as app_module

    app_module.app.config["TESTING"] = True
    return app_module.app


@pytest.fixture
def client(flask_app):
    flask_app.config["WTF_CSRF_ENABLED"] = False
    yield flask_app.test_client()
    flask_app.config["WTF_CSRF_ENABLED"] = True


def sign_in(client, role="admin"):
    with client.session_transaction() as session:
        session["user"] = "test-" + role
        session["role"] = role


class FakeSession:
    """Stands in for the recognition session's `subject` property."""

    def __init__(self, subject):
        self.subject = subject


@pytest.fixture
def started(monkeypatch):
    """
    A start that always succeeds once the gate lets it through.

    The camera, the session row and the recognition session are all stubbed;
    what is left is the class-list question and what the route does with it.
    `opened_sessions` is what proves the gate runs *before* the row is created.
    """
    calls = {"cameras_started": 0, "opened_sessions": 0, "closed_sessions": []}

    @contextlib.contextmanager
    def fake_cursor(*_args, **_kwargs):
        yield object()

    def open_session(subject_id, _user):
        calls["opened_sessions"] += 1
        return {"subject_code": SUBJECT_CODE, "id": subject_id}, 99

    def close_session(session_row_id):
        calls["closed_sessions"].append(session_row_id)

    def start_camera(_active):
        calls["cameras_started"] += 1
        return True

    monkeypatch.setattr(sessions_module, "db_cursor", fake_cursor)
    monkeypatch.setattr(
        sessions_module.attendance_service, "open_session", open_session
    )
    monkeypatch.setattr(
        sessions_module.attendance_service, "close_session", close_session
    )
    monkeypatch.setattr(sessions_module, "start_camera", start_camera)

    return calls


def start(client, monkeypatch, enrolled, role="admin"):
    """POST /start-attendance for a subject with `enrolled` students on it."""
    monkeypatch.setattr(
        sessions_module.enrolments_repo,
        "count_for_subject",
        lambda _cursor, _subject_id: enrolled,
    )

    sign_in(client, role)

    return client.post(
        "/start-attendance", data={"subject_id": str(SUBJECT_ID)}
    ).get_json()


# ---------------------------------------------------------------------------
# B3 - the gate
# ---------------------------------------------------------------------------


def test_an_empty_class_list_refuses_the_session(client, monkeypatch, started):
    """⚠️ **The finding.** The session could record nothing at all."""
    body = start(client, monkeypatch, enrolled=0)

    assert body["success"] is False
    assert body["message"].startswith(sessions_module.EMPTY_CLASS_LIST_REFUSAL)


def test_nothing_is_opened_or_started_when_it_is_refused(
    client, monkeypatch, started
):
    """
    ⚠️ **The gate runs before `open_session()`, and that is the point.**

    The other two refusal paths in this route have to close an
    `attendance_sessions` row they already created; this one never creates it,
    never reaches the camera, and so leaves nothing behind to tidy up or to
    show up later as a session that was never ended.
    """
    start(client, monkeypatch, enrolled=0)

    assert started["opened_sessions"] == 0, "a session row was created anyway"
    assert started["cameras_started"] == 0, "the camera was opened anyway"
    assert started["closed_sessions"] == [], "there was nothing to close"


def test_the_refusal_says_what_to_do_about_it(client, monkeypatch, started):
    """
    A refusal the operator cannot act on is a dead end. This one names the
    screen that fixes it, in the vocabulary the rest of the UI uses for it.
    """
    body = start(client, monkeypatch, enrolled=0)

    assert "Class List" in body["message"]
    assert "Not in this class" in body["message"], (
        "the refusal must connect itself to the message the operator would "
        "otherwise have read off the camera overlay, or the two are unrelated "
        "problems as far as anybody standing there can tell"
    )


def test_a_populated_class_list_starts_normally(client, monkeypatch, started):
    """The gate must be invisible in the case that matters most."""
    body = start(client, monkeypatch, enrolled=12)

    assert body["success"] is True
    assert body["session_id"] == 99
    assert body["subject_id"] == SUBJECT_ID
    assert started["cameras_started"] == 1


def test_a_failed_check_does_not_refuse(client, monkeypatch, started):
    """
    ⚠️ **A gate that closes when it cannot see is worse than no gate.**

    "The database did not respond" is not an answer, and refusing on it would
    turn a momentary blip into a class that cannot take attendance at all -
    which is the outcome this gate exists to be cheaper than. A genuine outage
    still stops the session, one line later, in `open_session()`.
    """

    def explode(_cursor, _subject_id):
        raise mysql.connector.Error("the database is down")

    monkeypatch.setattr(
        sessions_module.enrolments_repo, "count_for_subject", explode
    )

    sign_in(client)

    body = client.post(
        "/start-attendance", data={"subject_id": str(SUBJECT_ID)}
    ).get_json()

    assert body["success"] is True
    assert started["cameras_started"] == 1


# ---------------------------------------------------------------------------
# B3 - the marker on the dropdown
# ---------------------------------------------------------------------------


def stub_subjects(monkeypatch, rows):
    monkeypatch.setattr(
        sessions_module.subjects_repo,
        "for_selection_with_class_list_size",
        lambda _cursor: rows,
    )

    @contextlib.contextmanager
    def fake_cursor(*_args, **_kwargs):
        yield object()

    monkeypatch.setattr(sessions_module, "db_cursor", fake_cursor)


def subject(subject_id, code, enrolled):
    return {
        "id": subject_id,
        "subject_code": code,
        "subject_name": "A Subject",
        "course": "BSIT",
        "section": "A",
        "time_in": None,
        "enrolled": enrolled,
    }


def test_the_dropdown_marks_a_subject_with_no_class_list(client, monkeypatch):
    """
    Seen *before* a subject is chosen. The gate refuses the mistake; this is
    what stops the operator making it, and tells them which of several
    offerings is the one missing its roster.
    """
    stub_subjects(
        monkeypatch,
        [subject(1, "EMPTY-101", 0), subject(2, "FULL-202", 30)],
    )

    sign_in(client)
    page = client.get("/attendance").get_data(as_text=True)

    empty_option = page.split("EMPTY-101")[1].split("</option>")[0]
    full_option = page.split("FULL-202")[1].split("</option>")[0]

    assert "no class list" in empty_option
    assert "no class list" not in full_option


# ---------------------------------------------------------------------------
# R3/FS-16 - the locked End control
# ---------------------------------------------------------------------------


def running(monkeypatch, subject_id):
    monkeypatch.setattr(
        sessions_module,
        "recognition_session",
        FakeSession(
            ActiveSubject(
                subject_id=subject_id, session_id=7, subject_code=SUBJECT_CODE
            )
        ),
    )


def end_form(page):
    return page.split('id="endForm"')[1].split("</form>")[0]


def _option(form, value):
    """Whatever sits between an option's value and its closing `>`."""
    return form.split(f'<option value="{value}"')[1].split(">")[0]


def test_the_end_control_is_locked_while_a_session_runs(client, monkeypatch):
    """
    ⚠️ **The report.** Choosing a different subject to end with was answered
    with a 409 after the fact. There is exactly one subject the End form may
    carry while a session runs, so it carries that one and nothing else.
    """
    stub_subjects(monkeypatch, [subject(1, "RUNNING-101", 5), subject(2, "OTHER-202", 5)])
    running(monkeypatch, 1)

    sign_in(client)
    form = end_form(client.get("/attendance").get_data(as_text=True))

    assert "disabled" in form
    assert 'id="end_subject_id_locked"' in form, (
        "a disabled <select> submits nothing, so the value has to travel in a "
        "hidden input or ending the session posts no subject at all"
    )
    assert "Locked to the session that is running" in form


def test_the_locked_control_carries_the_running_subject(client, monkeypatch):
    """
    Locked to the *running* subject, not merely locked.

    ⚠️ The running subject is the **second** option here on purpose. Locking to
    whatever happens to be first, or to the template's `selected_subject_id`,
    would pass a test that only asserted "something is selected" and would end
    the wrong session's register - which is R3 restored through the control
    that was meant to prevent it.
    """
    stub_subjects(
        monkeypatch, [subject(1, "RUNNING-101", 5), subject(2, "OTHER-202", 5)]
    )
    running(monkeypatch, 2)

    sign_in(client)
    form = end_form(client.get("/attendance").get_data(as_text=True))

    # What actually gets posted: the select is disabled and submits nothing.
    hidden = form.split('id="end_subject_id_locked"')[1].split(">")[0]
    assert 'value="2"' in hidden
    assert 'name="subject_id"' in hidden

    # And what the operator sees selected in the (disabled) list.
    assert _option(form, "2").strip().startswith("selected")
    assert _option(form, "1").strip() == ""


def test_the_end_control_is_free_when_nothing_is_running(client, monkeypatch):
    """
    ⚠️ **The case the dropdown exists for.** With no session running the server
    has no subject to prefer, so the operator must be able to choose one -
    ending a session the process has forgotten (a restart, a crash) is exactly
    when this control does its job.
    """
    stub_subjects(monkeypatch, [subject(1, "RUNNING-101", 5), subject(2, "OTHER-202", 5)])
    running(monkeypatch, None)

    sign_in(client)
    form = end_form(client.get("/attendance").get_data(as_text=True))

    assert "disabled" not in form
    assert 'id="end_subject_id_locked"' not in form
    assert "Locked to the session" not in form


def test_the_server_still_refuses_a_mismatch_regardless_of_the_lock(
    client, monkeypatch
):
    """
    ⚠️ **The lock did not replace the guard, and must never be read as having
    done so.** A disabled control is absent from a curl, a replayed POST and a
    browser with scripting off. What it stands in front of is R3: a register
    written for a subject that was never taught, referentially valid and
    factually wrong, with nothing logged anywhere.

    This drives the route directly, past any control the page rendered.
    `tests/test_end_attendance_subject.py` is the full account.
    """
    running(monkeypatch, SUBJECT_ID)
    sign_in(client)

    response = client.post(
        "/end-attendance", data={"subject_id": str(OTHER_SUBJECT_ID)}
    )

    assert response.status_code == 409


# ---------------------------------------------------------------------------
# The two queries underneath
# ---------------------------------------------------------------------------


class ScriptedCursor:
    def __init__(self, row=None, rows=None):
        self.row = row
        self.rows = rows or []
        self.queries = []

    def execute(self, sql, params=None):
        self.queries.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


def test_counting_a_class_list_reads_only_enrolments():
    cursor = ScriptedCursor(row={"enrolled": 0})

    assert enrolments_repo.count_for_subject(cursor, 7) == 0

    sql, params = cursor.queries[0]
    assert "FROM enrolments" in sql
    assert params == (7,)


def test_counting_a_class_list_survives_a_plain_cursor():
    """
    The same guard `count_for_student()` carries: a non-dictionary cursor
    returns a tuple, and indexing a dict by [0] raises far from here.
    """
    assert enrolments_repo.count_for_subject(ScriptedCursor(row=(4,)), 7) == 4


def test_the_picker_counts_the_joined_column_not_the_rows():
    """
    ⚠️ **`COUNT(e.subject_id)`, never `COUNT(*)`.**

    A LEFT JOIN with no match still produces one row of NULLs, so `COUNT(*)`
    reports **1** for a subject nobody is enrolled in - which would mark every
    empty class list as full and, now that this drives a gate, would let
    exactly the wrong sessions start. Demonstrated live against the dev
    database: `CS401: COUNT(*) = 1, COUNT(e.subject_id) = 0`. The same trap is
    commented in `repositories/students.py` and `scripts/preflight.py`; this is
    the third place it applies and the first that is pinned.
    """
    cursor = ScriptedCursor(rows=[])

    subjects_repo.for_selection_with_class_list_size(cursor)

    sql, _params = cursor.queries[0]

    assert "COUNT(e.subject_id) AS enrolled" in sql
    assert "COUNT(*)" not in sql
    assert "LEFT JOIN enrolments" in sql


def test_the_picker_selects_the_same_columns_as_every_other_picker():
    """
    Derived from SELECTION_COLUMNS rather than written out again, so a column
    added there cannot give the attendance dropdown a different shape from the
    report filter for reasons nobody recorded.
    """
    cursor = ScriptedCursor(rows=[])

    subjects_repo.for_selection_with_class_list_size(cursor)

    sql, _params = cursor.queries[0]

    for column in subjects_repo.SELECTION_COLUMNS.split(","):
        assert f"s.{column.strip()}" in sql


# ---------------------------------------------------------------------------
# The refusal dialog and its way out
#
# The refusal used to be written only into the notice strip at the top of the
# page. On a phone the Start button is below the fold, so pressing it scrolled
# nothing and changed nothing visible - reported as the button being broken
# rather than as the system refusing. It is a <dialog> now, carrying a button
# to the screen that fixes it.
#
# ⚠️ **The notice is still written too.** US-1/US-2 removed `alert()` from this
# page because it erased itself and had to be *remembered*; a modal that did
# the same would be that regression in nicer clothes. The Python side of that
# is only that `message` is still returned, which every test above relies on.
# ---------------------------------------------------------------------------


def test_an_admin_is_given_the_way_out(client, monkeypatch, started):
    """The button, built by `url_for` rather than assembled in JavaScript."""
    body = start(client, monkeypatch, enrolled=0, role="admin")

    assert body["action"]["url"] == f"/subject_enrolments/{SUBJECT_ID}"
    assert body["action"]["label"] == "Open Class List"


def test_the_way_out_points_at_the_subject_that_was_refused(
    client, monkeypatch, started
):
    """
    ⚠️ A button to *a* class list is not a way out; it has to be the class list
    of the offering that was just refused, or the operator fixes the wrong one.
    """
    monkeypatch.setattr(
        sessions_module.enrolments_repo,
        "count_for_subject",
        lambda _cursor, _subject_id: 0,
    )
    sign_in(client, "admin")

    body = client.post(
        "/start-attendance", data={"subject_id": "42"}
    ).get_json()

    assert body["action"]["url"].endswith("/42")


def test_a_non_admin_is_not_offered_a_button_they_cannot_use(
    client, monkeypatch, started
):
    """
    ⚠️ **`subjects.subject_enrolments` is `@role_required('admin')`.**

    Offering the button to an instructor sends them to a 403 for a screen they
    will never be allowed to open, which turns "somebody needs to fill in this
    roster" into "the system is broken" for the one person who cannot fix
    either. They get the refusal and no button.
    """
    body = start(client, monkeypatch, enrolled=0, role="instructor")

    assert body["success"] is False
    assert body["action"] is None


def test_a_non_admin_is_told_who_can_fix_it(client, monkeypatch, started):
    """
    The remedy is a different sentence for somebody who cannot perform it.
    Telling an instructor to "add students under Subjects → Class List" is
    directions to a locked door.
    """
    body = start(client, monkeypatch, enrolled=0, role="instructor")

    assert "Ask an administrator" in body["message"]
    assert "Add students under" not in body["message"]


def test_the_page_carries_the_dialog(client, monkeypatch):
    """
    The markup the refusal is shown in. Rendered once and reused, so it has to
    be on the page before any refusal happens.
    """
    stub_subjects(monkeypatch, [subject(1, "ANY-101", 5)])
    running(monkeypatch, None)

    sign_in(client)
    page = client.get("/attendance").get_data(as_text=True)

    assert '<dialog id="session-dialog"' in page
    assert 'id="session-dialog-message"' in page
    assert 'id="session-dialog-action"' in page

    # ⚠️ Labelled, which is the whole difference from the alert() US-1/US-2
    # removed: a modal with no accessible name is announced as nothing at all.
    assert 'aria-labelledby="session-dialog-title"' in page


def test_the_notice_strip_survives_alongside_the_dialog(client, monkeypatch):
    """
    ⚠️ **Both, not either.** The dialog is dismissible; the notice is what the
    message persists in afterwards. Deleting the notice would restore exactly
    the complaint US-1/US-2 recorded against `alert()` - that the message had
    to be remembered rather than read.
    """
    stub_subjects(monkeypatch, [subject(1, "ANY-101", 5)])
    running(monkeypatch, None)

    sign_in(client)
    page = client.get("/attendance").get_data(as_text=True)

    assert 'id="session-notice"' in page
    assert 'aria-live="polite"' in page
