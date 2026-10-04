"""
The student and instructor lists, grouped by where each person belongs
(migration 009, DM-2 and DM-3).

⚠️ **Every list here is rendered with rows in it, and that is the point.** The
first build drew the section heading from an included template that read
`loop.changed()`. An included template cannot see the caller's `loop`, so the
page raised `'loop' is undefined` - but only when there was a student to draw a
heading for. Every other test that reached these pages stubbed an empty list,
1,457 of them passed, and it was found by loading the page (lessons.md L5).

No database: the repositories are replaced. Headings are read with a parser,
not searched for in the template, whose comments describe them (L12).
"""

from __future__ import annotations

import contextlib
from html.parser import HTMLParser

import pytest


@pytest.fixture(scope="module")
def flask_app():
    import app as app_module

    app_module.app.config["TESTING"] = True
    return app_module.app


@pytest.fixture
def client(flask_app):
    client = flask_app.test_client()

    with client.session_transaction() as session:
        session["user"] = "test-admin"
        session["role"] = "admin"
        session["admin_id"] = 1

    return client


@contextlib.contextmanager
def fake_cursor(*_args, **_kwargs):
    yield object()


class Headings(HTMLParser):
    """The text of every `<tr class="group-row">`, in order."""

    def __init__(self):
        super().__init__()
        self.headings = []
        self._inside = False

    def handle_starttag(self, tag, attrs):
        if tag == "tr" and "group-row" in (dict(attrs).get("class") or ""):
            self._inside = True
            self.headings.append("")

    def handle_endtag(self, tag):
        if tag == "tr":
            self._inside = False

    def handle_data(self, data):
        if self._inside:
            self.headings[-1] += data


def headings_of(response):
    assert response.status_code == 200, response.get_data(as_text=True)[:400]

    parser = Headings()
    parser.feed(response.get_data(as_text=True))

    return [" ".join(heading.split()) for heading in parser.headings]


def student(student_id, section_id, department=None, program=None, year=None,
            section=None):
    return {
        "student_id": student_id,
        "name": "Nobody In Particular",
        "section_id": section_id,
        "program_id": 5 if section_id else None,
        "department_id": 5 if section_id else None,
        "college_department": department,
        "program": program,
        "year_level": year,
        "section": section,
        "classes": 1,
    }


# In the order `students_repo.roster()` returns them: by department, program,
# year and section, with Unassigned last.
STUDENTS = [
    student("SEC-TEST-0001", 1, "College of Computing", "BSCS", 1, "B"),
    student("SEC-TEST-0002", 1, "College of Computing", "BSCS", 1, "B"),
    student("SEC-TEST-0003", 2, "College of Computing", "BSCS", 4, "B"),
    student("SEC-TEST-0004", None),
]

EXPECTED_STUDENT_HEADINGS = [
    "College of Computing › BSCS › Year 1 › Section B",
    "College of Computing › BSCS › Year 4 › Section B",
    "Unassigned — no program, year level or section",
]


@pytest.fixture
def student_lists(monkeypatch):
    import web.students as students_module

    asked = {}

    def roster(_cursor, **filters):
        asked["filters"] = filters
        return STUDENTS

    monkeypatch.setattr(students_module, "db_cursor", fake_cursor)
    monkeypatch.setattr(students_module.students_repo, "roster", roster)
    monkeypatch.setattr(
        students_module.students_repo, "all_students", lambda _cursor: STUDENTS
    )
    monkeypatch.setattr(
        students_module.subjects_repo, "for_selection", lambda _cursor: []
    )

    for name in ("departments", "programs", "sections"):
        monkeypatch.setattr(
            students_module.academics_repo, name, lambda _cursor: []
        )

    return asked


def test_manage_students_draws_one_heading_per_section(client, student_lists):
    """Two students in one section share a heading; it is not repeated."""
    assert headings_of(client.get("/manage_students")) == EXPECTED_STUDENT_HEADINGS


def test_the_registration_page_groups_its_list_the_same_way(client, student_lists):
    assert headings_of(client.get("/students")) == EXPECTED_STUDENT_HEADINGS


def test_the_filters_reach_the_query_as_integers(client, student_lists):
    client.get(
        "/manage_students?department_id=5&program_id=5&year_level=4&section_id=2"
    )

    assert student_lists["filters"] == {
        "department_id": 5,
        "program_id": 5,
        "year_level": 4,
        "section_id": 2,
    }


def test_a_filter_that_is_not_a_number_is_no_filter(client, student_lists):
    """Typed into the URL by hand, it cannot reach the query as anything else."""
    client.get("/manage_students?department_id=5%20OR%201=1&year_level=fourth")

    assert student_lists["filters"] == {
        "department_id": None,
        "program_id": None,
        "year_level": None,
        "section_id": None,
    }


def test_the_instructor_list_draws_one_heading_per_department(client, monkeypatch):
    import web.instructors as instructors_module

    def instructor(key, department_id, department_name):
        return {
            "id": key,
            "instructor_id": f"SEC-TEST-INSTR-{key}",
            "fullname": "Nobody In Particular",
            "must_change_password": 0,
            "department_id": department_id,
            "department_code": "CCIT" if department_id else None,
            "department_name": department_name,
        }

    rows = [
        instructor(1, 5, "College of Computing"),
        instructor(2, 5, "College of Computing"),
        instructor(3, None, None),
    ]

    monkeypatch.setattr(instructors_module, "db_cursor", fake_cursor)
    monkeypatch.setattr(
        instructors_module.instructors_repo, "roster", lambda _cursor, **_kw: rows
    )
    monkeypatch.setattr(
        instructors_module.instructors_repo, "all_instructors", lambda _cursor: rows
    )
    monkeypatch.setattr(
        instructors_module.academics_repo, "departments", lambda _cursor: []
    )

    expected = ["College of Computing", "Unassigned — no department"]

    assert headings_of(client.get("/manage_instructors")) == expected
    assert headings_of(client.get("/instructors")) == expected
