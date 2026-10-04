"""
DM-5 - an instructor sees their own classes and sessions, and nothing else.

Before this, `/reports`, `/export_excel`, `/attendance`, the dashboard and the
correction screen were `@authenticated` with no ownership check: any instructor
saw, exported and could correct every class in the school.

`tests/integration/test_academic_structure.py` proves the scope against a real
database. These are the parts that need none: where the scope comes from, that
it fails closed, and that the routes hand it to the queries whatever the URL
says. No database - every repository call is replaced.
"""

from __future__ import annotations

import contextlib

import mysql.connector
import pytest

from security.access import INSTRUCTOR_KEY, NO_INSTRUCTOR, instructor_scope


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


def sign_in(client, role="instructor", **extra):
    with client.session_transaction() as session:
        session["user"] = "test-" + role
        session["role"] = role
        session.update(extra)


@contextlib.contextmanager
def fake_cursor(*_args, **_kwargs):
    yield object()


# ---------------------------------------------------------------------------
# Where the scope comes from
# ---------------------------------------------------------------------------


def scope_for(flask_app, **session_values):
    from flask import session

    with flask_app.test_request_context("/"):
        session.update(session_values)

        return instructor_scope()


def test_an_administrator_is_unrestricted(flask_app):
    assert scope_for(flask_app, role="admin") is None


def test_an_instructor_is_scoped_to_their_own_key(flask_app):
    assert scope_for(flask_app, role="instructor", **{INSTRUCTOR_KEY: 7}) == 7


@pytest.mark.parametrize(
    "session_values",
    [
        {"role": "instructor"},                          # signed in before the key existed
        {"role": "instructor", INSTRUCTOR_KEY: None},
        {"role": "instructor", INSTRUCTOR_KEY: "7"},     # not the int login stores
        {"role": "student"},
        {},
    ],
)
def test_a_session_that_cannot_say_who_it_is_is_scoped_to_nothing(
    flask_app, session_values
):
    """
    Fails closed. `None` means unrestricted, so the one thing this must never
    do is return it for anyone but an administrator - "I do not know who you
    are" is not "you may see everything".
    """
    scope = scope_for(flask_app, **session_values)

    assert scope == NO_INSTRUCTOR
    assert scope is not None


def test_no_instructor_can_have_the_key_that_means_nobody():
    """AUTO_INCREMENT starts at 1, so 0 matches no `subjects.instructor_id`."""
    assert NO_INSTRUCTOR == 0


def test_login_puts_the_instructors_key_in_the_session(client, monkeypatch):
    """
    The numeric `instructors.id`, not the login identifier. They are different
    columns - `instructor_id` is what the instructor types - and subjects
    reference the first.
    """
    import web.auth as auth_module

    account = {
        "id": 42,
        "instructor_id": "SEC-TEST-INSTR",
        "fullname": "Nobody In Particular",
        "password": "a-stored-hash",
        "must_change_password": 0,
    }

    monkeypatch.setattr(auth_module, "db_cursor", fake_cursor)
    monkeypatch.setattr(
        auth_module.credentials_repo, "instructor_by_id", lambda *_args: account
    )
    monkeypatch.setattr(auth_module, "verify_password", lambda *_args: True)
    monkeypatch.setattr(auth_module, "is_known_default", lambda *_args: False)

    response = client.post("/login", data={
        "role": "instructor",
        "user_id": "SEC-TEST-INSTR",
        "password": "whatever",
    })

    assert response.status_code == 302

    with client.session_transaction() as session:
        assert session[INSTRUCTOR_KEY] == 42
        assert session["instructor_id"] == "SEC-TEST-INSTR"


# ---------------------------------------------------------------------------
# The reports and the export
# ---------------------------------------------------------------------------


@pytest.fixture
def reports(monkeypatch):
    """`/reports` and `/export_excel` with every query recorded, none run."""
    import web.reports as reports_module

    asked = {}

    def record(name, result):
        def call(_cursor, *args, **kwargs):
            asked[name] = (args, kwargs)
            return result

        return call

    monkeypatch.setattr(reports_module, "db_cursor", fake_cursor)
    monkeypatch.setattr(
        reports_module.subjects_repo, "for_report_filter", record("subjects", [])
    )
    monkeypatch.setattr(
        reports_module.instructors_repo,
        "all_instructors",
        record("instructors", []),
    )
    monkeypatch.setattr(
        reports_module.academics_repo, "placements", record("placements", [])
    )
    monkeypatch.setattr(
        reports_module.attendance_repo, "filtered", record("register", [])
    )
    monkeypatch.setattr(
        reports_module.attendance_repo,
        "sessions_filtered",
        record("sessions", []),
    )

    def register_workbook(*args, **kwargs):
        import io

        asked["export"] = (args, kwargs)
        return io.BytesIO(b"not a real workbook"), 0

    monkeypatch.setattr(
        reports_module.reporting, "register_workbook", register_workbook
    )

    return asked


def test_an_instructors_report_is_limited_to_them(client, reports):
    sign_in(client, **{INSTRUCTOR_KEY: 7})

    assert client.get("/reports").status_code == 200

    assert reports["register"][0] == (None, None, 7)
    assert reports["sessions"][0] == (None, None, 7)
    assert reports["subjects"][1] == {"instructor_id": 7}
    assert "instructors" not in reports, (
        "an instructor was offered the list of other instructors to choose from"
    )


def test_the_query_string_cannot_name_a_different_instructor(client, reports):
    """
    `?instructor=` is the administrator's filter. Read for an instructor, it
    would make the scope a default that the URL overrides.
    """
    sign_in(client, **{INSTRUCTOR_KEY: 7})

    client.get("/reports?instructor=8")
    client.get("/export_excel?instructor=8")

    assert reports["register"][0] == (None, None, 7)
    assert reports["sessions"][0] == (None, None, 7)
    assert reports["export"][0] == (None, None, 7)


def test_the_export_carries_the_same_scope_as_the_screen(client, reports):
    """
    Both go through one `_filters()`. A scope applied to the page and not the
    download is the whole register in a spreadsheet.
    """
    sign_in(client, **{INSTRUCTOR_KEY: 7})

    client.get("/reports?date=2026-08-16&subject=3")
    client.get("/export_excel?date=2026-08-16&subject=3")

    assert reports["register"][0] == reports["export"][0] == ("2026-08-16", 3, 7)


def test_an_administrator_sees_everything_by_default(client, reports):
    sign_in(client, role="admin", admin_id=1)

    client.get("/reports")

    assert reports["register"][0] == (None, None, None)
    assert reports["subjects"][1] == {"instructor_id": None}
    assert "instructors" in reports, "the administrator has no instructor filter"


def test_an_administrator_can_choose_one_instructor(client, reports):
    sign_in(client, role="admin", admin_id=1)

    client.get("/reports?instructor=8")
    client.get("/export_excel?instructor=8")

    assert reports["register"][0] == (None, None, 8)
    assert reports["sessions"][0] == (None, None, 8)
    assert reports["export"][0] == (None, None, 8)


def test_the_export_link_carries_the_chosen_filters(client, reports):
    """
    The link was a bare `/export_excel`, so the route that honours the filters
    (FS-11) was never sent any and the download was always the whole register.

    Parsed out of the rendered page, not searched for in the template, whose
    own comment describes the bug (lessons.md L12).
    """
    from html.parser import HTMLParser
    from urllib.parse import parse_qs, urlparse

    class ExportLink(HTMLParser):
        href = None

        def handle_starttag(self, tag, attrs):
            attributes = dict(attrs)

            if tag == "a" and "export-btn" in (attributes.get("class") or ""):
                self.href = attributes.get("href")

    sign_in(client, role="admin", admin_id=1)

    page = client.get("/reports?date=2026-08-16&subject=3&instructor=8")

    parser = ExportLink()
    parser.feed(page.get_data(as_text=True))

    assert parser.href is not None, "the export link is not on the page"

    assert parse_qs(urlparse(parser.href).query) == {
        "date": ["2026-08-16"],
        "subject": ["3"],
        "instructor": ["8"],
        "sort": ["date"],
    }


# ---------------------------------------------------------------------------
# Narrowing and ordering by where the student belongs
# ---------------------------------------------------------------------------

EVERYTHING = {
    "department_id": None,
    "program_id": None,
    "year_level": None,
    "section_id": None,
}


def test_the_placement_filters_reach_the_register_the_sessions_and_the_export(
    client, reports
):
    """One set of filters, three consumers. A fourth that forgot them would
    show one section on screen and export the school."""
    sign_in(client, role="admin", admin_id=1)

    query = "department_id=5&program_id=5&year_level=4&section_id=2&sort=placement"
    chosen = {"department_id": 5, "program_id": 5, "year_level": 4, "section_id": 2}

    client.get(f"/reports?{query}")
    client.get(f"/export_excel?{query}")

    assert reports["register"][1] == dict(chosen, sort="placement")
    assert reports["export"][1] == dict(chosen, sort="placement")
    assert reports["sessions"][1] == chosen


def test_an_instructor_narrows_within_their_own_classes(client, reports):
    """
    The placement filters do not replace the scope, they sit inside it: the
    instructor key is still the session's whatever else the URL asks for.
    """
    sign_in(client, **{INSTRUCTOR_KEY: 7})

    client.get("/reports?year_level=4&section_id=2")

    assert reports["register"][0] == (None, None, 7)
    assert reports["register"][1] == dict(
        EVERYTHING, year_level=4, section_id=2, sort="date"
    )


def test_an_instructor_is_offered_only_the_sections_they_teach(client, reports):
    """An administrator is offered every section; an instructor, their own."""
    sign_in(client, **{INSTRUCTOR_KEY: 7})
    client.get("/reports")

    assert reports["placements"][1] == {"instructor_id": 7}

    sign_in(client, role="admin", admin_id=1)
    client.get("/reports")

    assert reports["placements"][1] == {"instructor_id": None}


@pytest.mark.parametrize(
    "sort", ["", "nonsense", "a.status; DROP TABLE attendance", "PLACEMENT"]
)
def test_an_unknown_sort_is_the_default_one(client, reports, sort):
    """
    `sort` is a key into an allowlist, never SQL: an ORDER BY cannot be a
    bound parameter, so the only safe thing a request can do is choose.
    """
    from urllib.parse import quote

    sign_in(client, role="admin", admin_id=1)

    assert client.get(f"/reports?sort={quote(sort)}").status_code == 200
    assert reports["register"][1]["sort"] == "date"


def test_every_sort_the_form_offers_is_one_the_query_knows():
    from repositories import attendance as attendance_repo

    assert set(attendance_repo.SORT_ORDERS) == {"date", "placement"}
    assert attendance_repo.DEFAULT_SORT in attendance_repo.SORT_ORDERS


def test_a_filter_the_query_does_not_know_is_an_error_not_a_no_op():
    """
    Ignored silently, a mistyped filter returns the whole register under a
    heading that says it is one section's.
    """
    from repositories import attendance as attendance_repo

    with pytest.raises(KeyError):
        attendance_repo._filtered_query(campus_id=3)


def test_the_placement_filters_are_bound_not_spliced():
    from repositories import attendance as attendance_repo

    query, values = attendance_repo._filtered_query(
        None, None, 7, "placement", year_level=4, section_id=2
    )

    assert values == [7, 4, 2]
    assert "sec.year_level = %s" in query and "s.section_id = %s" in query
    assert query.rstrip().endswith(attendance_repo.SORT_ORDERS["placement"])


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


@pytest.fixture
def sessions(monkeypatch):
    """`/start-attendance` and `/end-attendance`, with nothing behind them."""
    import web.sessions as sessions_module

    calls = {"opened": 0, "stopped": 0}

    def open_session(*_args):
        calls["opened"] += 1
        return None, None

    def stop_camera():
        calls["stopped"] += 1

    monkeypatch.setattr(sessions_module, "db_cursor", fake_cursor)
    monkeypatch.setattr(
        sessions_module.attendance_service, "open_session", open_session
    )
    monkeypatch.setattr(sessions_module, "stop_camera", stop_camera)

    return sessions_module, calls


def test_an_instructor_cannot_start_a_session_on_a_subject_that_is_not_theirs(
    client, sessions, monkeypatch
):
    """
    The filtered dropdown never offers it; this is the request that names it
    anyway. Refused before a session row exists.
    """
    sessions_module, calls = sessions

    asked = []

    def is_taught_by(_cursor, subject_id, instructor_id):
        asked.append((subject_id, instructor_id))
        return False

    monkeypatch.setattr(sessions_module.subjects_repo, "is_taught_by", is_taught_by)

    sign_in(client, **{INSTRUCTOR_KEY: 7})

    body = client.post("/start-attendance", data={"subject_id": "3"}).get_json()

    assert body == {"success": False, "message": sessions_module.NOT_YOUR_SUBJECT}
    assert asked == [(3, 7)]
    assert calls["opened"] == 0, "a session row was opened before the refusal"


def test_a_failed_ownership_check_refuses(client, sessions, monkeypatch):
    """
    The opposite of the class-list gate beside it, deliberately. That one
    protects the operator's time and stays open on a blip; this one decides
    whose register is written, and "could not check" is not "yes".
    """
    sessions_module, calls = sessions

    def is_taught_by(*_args):
        raise mysql.connector.Error("the database went away")

    monkeypatch.setattr(sessions_module.subjects_repo, "is_taught_by", is_taught_by)

    sign_in(client, **{INSTRUCTOR_KEY: 7})

    body = client.post("/start-attendance", data={"subject_id": "3"}).get_json()

    assert body["success"] is False
    assert calls["opened"] == 0


def test_an_instructor_cannot_end_a_class_that_is_not_theirs(
    client, sessions, monkeypatch
):
    """Refused before the camera is stopped, so whatever is running still is."""
    sessions_module, calls = sessions

    class NothingRunning:
        subject = None

    monkeypatch.setattr(sessions_module, "recognition_session", NothingRunning())
    monkeypatch.setattr(
        sessions_module.subjects_repo, "is_taught_by", lambda *_args: False
    )

    sign_in(client, **{INSTRUCTOR_KEY: 7})

    response = client.post("/end-attendance", data={"subject_id": "3"})

    assert response.status_code == 403
    assert calls["stopped"] == 0, "the camera was stopped by a refused request"


def test_an_administrator_is_not_asked_who_teaches_the_subject(
    client, sessions, monkeypatch
):
    """Unrestricted means the check does not run, not that it happens to pass."""
    sessions_module, calls = sessions

    def is_taught_by(*_args):
        raise AssertionError("an administrator's request was ownership-checked")

    monkeypatch.setattr(sessions_module.subjects_repo, "is_taught_by", is_taught_by)
    monkeypatch.setattr(
        sessions_module.enrolments_repo, "count_for_subject", lambda *_args: 1
    )

    sign_in(client, role="admin", admin_id=1)

    client.post("/start-attendance", data={"subject_id": "3"})

    assert calls["opened"] == 1


# ---------------------------------------------------------------------------
# The mobile API
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ({"role": "admin"}, None),
        ({"role": "instructor", "instructor_pk": 7}, 7),
        # A token issued before the claim existed: nothing, until it expires.
        ({"role": "instructor"}, NO_INSTRUCTOR),
        ({"role": "instructor", "instructor_pk": "7"}, NO_INSTRUCTOR),
        # Students are confined by the routes themselves, not by this.
        ({"role": "student"}, None),
    ],
)
def test_a_token_carries_the_same_scope_as_a_session(token, expected):
    from web.api import _instructor_scope

    assert _instructor_scope(token) == expected
