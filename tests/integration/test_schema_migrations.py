"""
The migration runner against a real server (PO-5).

`tests/test_migrations.py` covers discovery, ordering and statement splitting
with no database. It says twice, in its docstrings, that the half needing a
server belongs here. This is that half: do the shipped migrations actually
build a database from nothing, and is applying them twice a no-op.

That first question is not rhetorical. Migration 004 was written three times,
and two of those drafts *reported success* while leaving corrupt referential
state, because they relied on the server failing in ways this server does not
(lessons.md L11). A migration set that has only ever been applied to the one
database that already has the tables has not been tested.
"""

from __future__ import annotations

import pytest

from infra.db import db_cursor
from infra.migrations import apply_all, discover

pytestmark = pytest.mark.integration


def test_every_migration_applied_to_the_scratch_database(scratch_database):
    """
    The `scratch_database` fixture built this schema from nothing by running
    `apply_all`. If that had failed the fixture would have raised, so reaching
    here at all is the result - what this asserts is that the ledger agrees.
    """
    with db_cursor() as cursor:
        cursor.execute("SELECT version FROM schema_migrations")
        applied = {row[0] for row in cursor.fetchall()}

    on_disk = {migration.version for migration in discover()}

    assert applied == on_disk, (
        "The ledger and the migrations directory disagree after a clean build: "
        f"missing {sorted(on_disk - applied)}, unexpected {sorted(applied - on_disk)}"
    )


def test_applying_a_second_time_is_a_no_op(scratch_database):
    """
    The ledger's whole purpose. Without it, `python app.py` on a current
    database would replay 002..006 on every start.
    """
    assert apply_all(database=scratch_database) == []


def test_the_tables_the_application_reads_all_exist(scratch_database):
    """
    A fresh install that boots and then 500s on the first page touching a
    missing table is the failure PO-5 exists to prevent.
    """
    expected = {
        "admin",
        "attendance",
        "attendance_audit",
        "attendance_sessions",
        "enrolments",
        "instructors",
        "schema_migrations",
        "students",
        "subjects",
    }

    with db_cursor() as cursor:
        cursor.execute("SHOW TABLES")
        present = {row[0] for row in cursor.fetchall()}

    assert expected <= present, f"Missing after a clean build: {sorted(expected - present)}"


def test_the_server_runs_the_sql_mode_the_deployment_runs():
    """
    ⚠️ The most dangerous environmental fact in this project.

    The deployed server's `sql_mode` has **no STRICT_TRANS_TABLES**, so a
    failed conversion is a silent wrong value rather than an error: NULL
    becomes 0, an unparseable time becomes midnight, an over-long string is
    truncated. That cost two wrong drafts of migration 004, the second of which
    reported *success* while leaving an attendance row pointing at a subject
    that does not exist (lessons.md L11).

    A server running in strict mode would catch those coercions - and would
    therefore prove nothing about the machine this is deployed on. So this is
    asserted rather than merely noted: a run whose engine is stricter than the
    deployment is measuring a system nobody ships.

    **Two engines are in play, deliberately.** Locally this is MariaDB 10.4.32
    under XAMPP; CI runs MySQL 8.0 (the user's choice), which ships strict mode
    *on* by default and has it turned off in the workflow. The migrations are
    written to syntax both accept - no RENAME COLUMN, no functional indexes,
    no utf8mb4_0900_* collations - so both are meaningful runs. What neither
    catches alone is a behaviour that differs between them, and the local run
    against the real MariaDB is the authority.
    """
    with db_cursor() as cursor:
        cursor.execute("SELECT VERSION()")
        version = cursor.fetchone()[0]

        cursor.execute("SELECT @@sql_mode")
        sql_mode = cursor.fetchone()[0]

    assert "STRICT_TRANS_TABLES" not in sql_mode, (
        f"This server runs in strict mode ({sql_mode!r}), but the deployment "
        "does not. Migrations and inserts verified here would behave "
        "differently in production - which is exactly lessons.md L11. Match "
        "the deployment's sql_mode or re-verify every conversion."
    )

    # A readable failure if the engine is ever something the migrations were
    # not written against. MariaDB 10.4 locally, MySQL 8 in CI.
    assert "mariadb" in version.lower() or version.startswith("8."), (
        f"Unexpected engine {version!r}: the migrations are written for "
        "MariaDB 10.4 and MySQL 8."
    )
