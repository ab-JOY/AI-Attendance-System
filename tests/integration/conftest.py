"""
Integration tests: the schema behaviour Phase 4 changed, against a real server.

**Why this directory exists.** Phase 4 closed FS-3, FS-4, FS-5, FS-7, FS-8,
RE-3 and RE-4 - the register scoped to enrolments, Absent actually persisted,
Late actually derived, a unique constraint instead of a read-then-write race,
foreign keys instead of a hand-rolled cascade. Every one of those was verified
by end-to-end scripts run against the deployed database and then discarded.
The suite had no database, so none of it was regression-protected, and this
project's whole history is defects that looked like working code.

`tests/test_migrations.py` already names this directory in two docstrings as
where the half that needs a server belongs. This is that directory.

===========================================================================
THE SAFETY GATE - read this before adding a test
===========================================================================

lessons.md L6 is the governing lesson here, and it was written from an incident
in this repository: a route test naming a real student ran against unguarded
code and **deleted the row**, and a second test took out a subject an hour
later without anyone noticing. Two properties of that incident matter now.

  1. **A worktree is not isolation.** It isolates files. The database, the
     `.env` and every other external resource are shared, and the checkout runs
     with the same credentials as everything else.
  2. **These tests are destructive by design.** They truncate tables between
     tests. Pointed at the configured database they would empty the
     deployment - and unlike the L6 incident there would be no `labels.txt` to
     reconstruct from.

So the target is named explicitly, never defaulted, and checked three ways:

  1. `INTEGRATION_DB_NAME` unset -> **skip**. Opt in by naming a scratch
     schema, not by setting a boolean. What has to be deliberate is the
     *target*, not the intent.
  2. The name equals the configured database -> **fail**, loudly, never skip.
     A skip here reads as "not configured yet" and is precisely how someone
     ends up running this against the deployment and believing it was fine.
  3. The name does not match `test_*` -> **fail**. An allowlist, for the same
     reason `security/paths.py` allowlists rather than blocklists: a typo of
     the real database name is refused outright rather than nearly-matched.

Run them with:

    INTEGRATION_DB_NAME=test_attendance_scratch pytest tests/integration/ -q

===========================================================================
WHAT THE FIXTURES DO
===========================================================================

`scratch_database` creates the schema, points `settings` and the connection
pool at it, applies every migration, and drops it at the end.
`infra.migrations.apply_all()` already takes a `database` parameter, and its
docstring says the integration tests are why - this is the caller it was
written for.

`clean_tables` truncates between tests so they are independent without paying
to rebuild the schema each time.

**Every identifier seeded here is fake** - `INT-TEST-...`. L6 again: had the
first draft of the route tests used a fake ID, the negative run would have
deleted a row matching nothing.
"""

from __future__ import annotations

import os
import re

import mysql.connector
import pytest

from config.settings import settings

ENV_VAR = "INTEGRATION_DB_NAME"

# A scratch database is named `test_something`. Anything else is refused rather
# than used, so `attendancesystem_db` typed into the wrong shell cannot become
# the target by accident.
_SCRATCH_NAME = re.compile(r"^test_[A-Za-z0-9_]+$")

# Truncated between tests, children before parents. `schema_migrations` is
# deliberately absent: emptying the ledger would make the schema look unapplied
# to anything that checked it mid-run.
DATA_TABLES = (
    "attendance_audit",
    "attendance",
    "enrolments",
    "attendance_sessions",
    "subjects",
    "students",
    "instructors",
    "admin",
)


def _resolve_scratch_name() -> str:
    """The validated scratch database name. Skips or fails; never returns junk."""
    name = os.environ.get(ENV_VAR, "").strip()

    if not name:
        pytest.skip(
            f"{ENV_VAR} is not set. These tests need a real MySQL/MariaDB "
            "server and a scratch database to build, e.g. "
            f"{ENV_VAR}=test_attendance_scratch",
            allow_module_level=True,
        )

    # Not a skip. A skip reads as "not configured" and would let a run that
    # was about to truncate the deployment look like a clean pass.
    if name == settings.db_name:
        pytest.fail(
            f"{ENV_VAR} is set to {name!r}, which is the *configured* database. "
            "These tests truncate every table they touch and drop the schema "
            "afterwards. Point them at a scratch database.",
            pytrace=False,
        )

    if not _SCRATCH_NAME.match(name):
        pytest.fail(
            f"{ENV_VAR}={name!r} does not look like a scratch database. "
            "It must match `test_[A-Za-z0-9_]+`, so a mistyped real database "
            "name is refused rather than created and dropped.",
            pytrace=False,
        )

    return name


def _connect_serverwide():
    """A connection with no database selected, for CREATE/DROP DATABASE."""
    kwargs = settings.db_kwargs()
    kwargs.pop("database", None)

    return mysql.connector.connect(**kwargs)


@pytest.fixture(scope="session", autouse=True)
def scratch_database():
    """
    Build a scratch schema, point the application at it, drop it afterwards.

    Autouse, so a test cannot forget to request it and end up running against
    whatever `settings` happened to be pointing at.
    """
    from infra.db import reset_pool
    from infra.migrations import apply_all

    name = _resolve_scratch_name()

    try:
        connection = _connect_serverwide()

    except mysql.connector.Error as error:
        pytest.skip(
            f"No database server answering on {settings.db_host}:"
            f"{settings.db_port} ({error}). Start MariaDB and re-run.",
            allow_module_level=True,
        )

    try:
        cursor = connection.cursor()
        # The name is allowlisted above; MySQL cannot bind an identifier as a
        # parameter, which is why the allowlist is the control and not a
        # convenience.
        cursor.execute(f"DROP DATABASE IF EXISTS `{name}`")
        cursor.execute(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4")
        cursor.close()
    finally:
        connection.close()

    original_db_name = settings.db_name

    settings.db_name = name
    reset_pool()

    # Migrations against the scratch schema. This is also the PO-5 evidence:
    # if 001..006 cannot build a database from nothing, every test below is
    # measuring something other than the shipped schema.
    apply_all(database=name)

    try:
        yield name

    finally:
        settings.db_name = original_db_name
        reset_pool()

        connection = _connect_serverwide()
        try:
            cursor = connection.cursor()
            cursor.execute(f"DROP DATABASE IF EXISTS `{name}`")
            cursor.close()
        finally:
            connection.close()


@pytest.fixture(autouse=True)
def clean_tables(scratch_database):
    """
    Empty the data tables before each test.

    `FOREIGN_KEY_CHECKS=0` around the truncates because RE-4 added eight
    foreign keys and TRUNCATE will not run on a table another references.
    Disabling the checks is safe here precisely because everything is being
    emptied - there is no partial state left to be inconsistent.
    """
    from infra.db import db_cursor

    with db_cursor(commit=True) as cursor:
        cursor.execute("SET FOREIGN_KEY_CHECKS = 0")

        for table in DATA_TABLES:
            cursor.execute(f"TRUNCATE TABLE `{table}`")

        cursor.execute("SET FOREIGN_KEY_CHECKS = 1")

    return None


# ===========================================================================
# The application under test
# ===========================================================================


@pytest.fixture(scope="session")
def flask_app(scratch_database):
    """
    The real application, pointed at the scratch schema.

    Depends on `scratch_database` so `settings.db_name` is already redirected
    before anything builds a connection pool.
    """
    import app as app_module

    app_module.app.config["TESTING"] = True

    # CSRF off. It is not what these tests are about, and it is already
    # covered properly in tests/test_route_security.py, which asserts that a
    # POST without a token is refused. Leaving it on here would mean every
    # test fetching a token to exercise a SQL statement.
    app_module.app.config["WTF_CSRF_ENABLED"] = False

    return app_module.app


@pytest.fixture
def client(flask_app):
    return flask_app.test_client()


def sign_in_as(client, role="admin", username=None):
    """
    Fabricate a session, exactly as tests/test_route_security.py does.

    Deliberately does not create a credential row: authentication is tested in
    the fast suite, and seeding a bcrypt hash per test would cost more than
    every query under test combined.
    """
    with client.session_transaction() as session:
        session["user"] = username or f"int-test-{role}"
        session["role"] = role


# ===========================================================================
# Seed helpers
#
# Every identifier is `INT-TEST-...` on purpose (L6). If one of these ever
# escapes into the configured database through a misconfiguration, it matches
# nothing real and is obvious in a row dump.
# ===========================================================================


def make_student(cursor, student_id, name="Integration Test Subject", **overrides):
    values = {
        "student_id": student_id,
        "name": name,
        "college_department": "College of Computing",
        "program": "BSCS",
        "year_level": 4,
        "section": "B",
    }
    values.update(overrides)

    cursor.execute(
        """
        INSERT INTO students
            (student_id, name, college_department, program, year_level, section)
        VALUES (%(student_id)s, %(name)s, %(college_department)s,
                %(program)s, %(year_level)s, %(section)s)
        """,
        values,
    )

    return student_id


def make_subject(cursor, subject_code="INT-TEST-101", time_in="08:00:00", **overrides):
    """
    A subject offering. `time_in` is what FS-8 derives Late from, so it is a
    named parameter rather than buried in the defaults.
    """
    values = {
        "subject_code": subject_code,
        "subject_name": "Integration Testing",
        "instructor": None,
        "instructor_id": None,
        "day": "Monday",
        "course": "BSCS",
        "section": "B",
        "time_in": time_in,
        "time_out": "10:00:00",
    }
    values.update(overrides)

    cursor.execute(
        """
        INSERT INTO subjects
            (subject_code, subject_name, instructor, instructor_id, day,
             course, section, time_in, time_out)
        VALUES (%(subject_code)s, %(subject_name)s, %(instructor)s,
                %(instructor_id)s, %(day)s, %(course)s, %(section)s,
                %(time_in)s, %(time_out)s)
        """,
        values,
    )

    return cursor.lastrowid


def make_instructor(cursor, instructor_id="INT-TEST-INSTR", fullname="Integration Instructor"):
    """
    An instructor account. The password is a placeholder string, not a bcrypt
    hash: nothing here logs in through it (see `sign_in_as`), and generating a
    real hash costs ~250 ms per call for no coverage.
    """
    cursor.execute(
        """
        INSERT INTO instructors
            (instructor_id, fullname, password, must_change_password)
        VALUES (%s, %s, %s, 0)
        """,
        (instructor_id, fullname, "not-a-real-hash"),
    )

    return cursor.lastrowid


def make_enrolment(cursor, student_id, subject_id):
    """The FS-3 relation: this student is on this offering's class list."""
    cursor.execute(
        "INSERT INTO enrolments (student_id, subject_id) VALUES (%s, %s)",
        (student_id, subject_id),
    )


def make_attendance(
    cursor,
    student_id,
    subject_id,
    status="Present",
    time_in="08:00:00",
    attendance_date=None,
):
    cursor.execute(
        """
        INSERT INTO attendance
            (student_id, subject_id, attendance_date, time_in, status)
        VALUES (%s, %s, COALESCE(%s, CURDATE()), %s, %s)
        """,
        (student_id, subject_id, attendance_date, time_in, status),
    )

    return cursor.lastrowid
