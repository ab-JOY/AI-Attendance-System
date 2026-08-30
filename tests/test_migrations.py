"""
The migration runner's file handling, with no database (PO-5).

Everything here is about *discovery and parsing* - which files count as
migrations, what order they run in, and how a file is split into statements.
None of it connects to anything, so it belongs in the fast suite.

The half that needs a real server - does 001 actually apply to an empty
database, and is a second run a no-op - is in tests/integration/, gated on a
scratch database, because a migration test that runs against the configured
database would rewrite the deployment's schema. That is lessons.md L6 exactly:
a negative test is an execution of the code you are proving works.
"""

from __future__ import annotations

import pytest

from infra.migrations import (
    MIGRATIONS_DIR,
    Migration,
    MigrationError,
    discover,
)

# ==============================
# The real migrations directory
# ==============================


def test_the_repository_migrations_are_discoverable():
    """The shipped migrations parse, so a typo in a filename fails the build."""
    migrations = discover()

    assert migrations, "No migrations found in migrations/"
    assert migrations[0].version == "001"
    assert migrations[0].name == "baseline"


def test_migrations_are_returned_in_version_order():
    versions = [m.version for m in discover()]

    assert versions == sorted(versions)


def test_every_shipped_migration_has_at_least_one_statement():
    """
    A migration whose statements() comes back empty would be recorded as
    applied while doing nothing - the silent no-op this module exists to stop.
    An all-comment file is the realistic way to get there.
    """
    for migration in discover():
        assert migration.statements(), f"{migration.label} parses to no statements"


def test_the_baseline_creates_every_table_the_application_reads():
    """
    001 is the deployed schema. If a table the app queries is missing from it,
    a fresh install boots and then 500s on the first page that touches it.
    """
    baseline = discover()[0]
    sql = " ".join(baseline.statements()).lower()

    for table in ("admin", "instructors", "students", "subjects", "attendance"):
        assert f"create table if not exists {table}" in sql, (
            f"001_baseline.sql does not create {table}"
        )


def test_the_baseline_is_replayable():
    """
    DDL cannot be rolled back, so a migration that fails halfway is re-run from
    the top. The baseline in particular must survive that, because it is the
    one that runs against a database which may already have the tables.
    """
    baseline = discover()[0]

    for statement in baseline.statements():
        assert "if not exists" in statement.lower(), (
            f"Not replayable, so a partial failure cannot be re-run: {statement[:60]}"
        )


# ==============================
# Discovery rules
# ==============================


def write(directory, name, body="CREATE TABLE IF NOT EXISTS t (id INT);"):
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return path


def test_a_badly_named_file_is_an_error_not_a_skip(tmp_path):
    """
    Ignoring an unrecognised .sql file would let a real migration sit in the
    directory, never run, and never be mentioned. Failing loudly is the whole
    value of having a ledger.
    """
    write(tmp_path, "001_baseline.sql")
    write(tmp_path, "add_enrolments.sql")

    with pytest.raises(MigrationError, match="not a valid migration filename"):
        discover(tmp_path)


def test_two_migrations_cannot_share_a_version(tmp_path):
    """Two branches each adding 003 is the realistic way this happens."""
    write(tmp_path, "003_enrolments.sql")
    write(tmp_path, "003_attendance_sessions.sql")

    with pytest.raises(MigrationError, match="share version 003"):
        discover(tmp_path)


def test_a_missing_directory_is_an_error(tmp_path):
    with pytest.raises(MigrationError, match="No migrations directory"):
        discover(tmp_path / "nope")


def test_only_sql_files_are_considered(tmp_path):
    write(tmp_path, "001_baseline.sql")
    (tmp_path / "README.md").write_text("not a migration", encoding="utf-8")
    (tmp_path / "001_baseline.sql.bak").write_text("stale", encoding="utf-8")

    assert [m.label for m in discover(tmp_path)] == ["001_baseline"]


def test_versions_sort_numerically_not_lexicographically(tmp_path):
    """
    Zero padding is what makes this work, and the filename pattern requires at
    least three digits so it keeps working past 009.
    """
    for name in ("010_ten.sql", "002_two.sql", "001_one.sql"):
        write(tmp_path, name)

    assert [m.version for m in discover(tmp_path)] == ["001", "002", "010"]


# ==============================
# Statement splitting
# ==============================


def statements_of(tmp_path, body):
    path = write(tmp_path, "001_test.sql", body)
    return Migration(version="001", name="test", path=path).statements()


def test_statements_are_split_on_terminating_semicolons(tmp_path):
    body = "CREATE TABLE a (id INT);\nCREATE TABLE b (id INT);\n"

    assert statements_of(tmp_path, body) == [
        "CREATE TABLE a (id INT)",
        "CREATE TABLE b (id INT)",
    ]


def test_a_trailing_statement_without_a_semicolon_is_kept(tmp_path):
    """
    The last statement in a file often has no trailing newline after its
    semicolon, and some have no semicolon at all. Dropping it would apply a
    migration minus its final statement and record it as done.
    """
    assert statements_of(tmp_path, "CREATE TABLE a (id INT)") == [
        "CREATE TABLE a (id INT)"
    ]


def test_comments_and_blank_lines_do_not_become_statements(tmp_path):
    body = (
        "-- a leading comment\n"
        "\n"
        "CREATE TABLE a (id INT);\n"
        "\n"
        "-- a trailing comment, alone after the last semicolon\n"
    )

    assert statements_of(tmp_path, body) == ["CREATE TABLE a (id INT)"]


def test_comments_inside_a_statement_are_stripped(tmp_path):
    body = "CREATE TABLE a (\n    -- why this column exists\n    id INT\n);\n"

    assert statements_of(tmp_path, body) == ["CREATE TABLE a (\n    id INT\n)"]


def test_an_all_comment_file_yields_nothing(tmp_path):
    """
    Documented behaviour, and the reason
    test_every_shipped_migration_has_at_least_one_statement exists above.
    """
    assert statements_of(tmp_path, "-- nothing to do yet\n-- really\n") == []


def test_the_migrations_directory_constant_points_into_the_repository():
    assert MIGRATIONS_DIR.name == "migrations"
    assert MIGRATIONS_DIR.is_dir()


# ==============================
# The start-up schema check
# ==============================
#
# The application brings a stale schema up to date when it starts, so a copy
# of a newer version cannot silently run against a database created by an
# older one. These tests cover the decision, not the SQL - the SQL half needs
# a server and belongs in tests/integration/.


def test_the_startup_check_is_never_called_at_import_time():
    """
    ⚠️ The load-bearing property of the whole feature.

    `tests/` imports `app`, and CI has no database at all. If the schema check
    ran at import, every test in this suite would depend on a running MySQL
    server and CI would fail on a green codebase.

    Checked at the source level with `ast` rather than by importing and
    watching for a connection, because by the time any test runs `app` has
    usually been imported already by another one and the observation would be
    vacuous.
    """
    import ast

    from tests.conftest import PROJECT_ROOT

    tree = ast.parse((PROJECT_ROOT / "app.py").read_text(encoding="utf-8"))

    # The property is "not reachable when the module is executed", which means
    # two kinds of call are fine: one inside `if __name__ == '__main__'`, and
    # one inside a function body, which does not run until something calls it.
    # Only a call at module scope opens a connection on import.
    guarded = set()

    def shelter(node):
        for inner in ast.walk(node):
            guarded.add(id(inner))

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            shelter(node)

        elif isinstance(node, ast.If):
            test = node.test
            if (
                isinstance(test, ast.Compare)
                and isinstance(test.left, ast.Name)
                and test.left.id == "__name__"
            ):
                shelter(node)

    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in ("ensure_schema_current", "check_database_on_startup")
        and id(node) not in guarded
    ]

    assert not offenders, (
        "app.py calls the start-up schema check outside `if __name__ == "
        f"'__main__'` at line(s) {offenders}. That makes importing app - which "
        "the whole test suite does - open a database connection."
    )


def test_a_failed_startup_check_stops_the_application():
    """
    An attendance system that boots on a half-migrated schema and then 500s on
    the register is worse than one that refuses to boot. The __main__ block
    must exit non-zero rather than call app.run() anyway.
    """
    from tests.conftest import PROJECT_ROOT

    source = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")

    assert "if not check_database_on_startup():" in source
    assert "sys.exit(1)" in source


@pytest.mark.parametrize(
    ("reason", "must_mention"),
    [
        ("no-server", "Start MySQL"),
        ("no-database", "setup_db.py"),
    ],
)
def test_each_failure_names_the_thing_that_fixes_it(reason, must_mention, monkeypatch):
    """
    "Start MySQL", "run setup_db.py" and "a migration failed" are three
    different problems. A single "database error" tells an operator apart from
    none of them, and this system is about to be handed to a tester on another
    machine.
    """
    import mysql.connector

    import infra.migrations as migrations

    errno = {
        "no-server": migrations.CR_CONN_HOST_ERROR,
        "no-database": migrations.ER_BAD_DB_ERROR,
    }[reason]

    def refuse(*args, **kwargs):
        raise mysql.connector.Error(msg="nope", errno=errno)

    monkeypatch.setattr(migrations, "_connect", refuse)

    state = migrations.ensure_schema_current()

    assert not state.ok
    assert state.reason == reason
    assert must_mention.lower() in state.detail.lower()


def test_a_stale_schema_is_reported_rather_than_applied_when_disabled(monkeypatch):
    """
    AUTO_MIGRATE=false is for anywhere running more than one process, where
    two workers would race to apply the same migration. It must report, never
    apply.
    """
    import infra.migrations as migrations

    applied = []

    class FakeCursor:
        def close(self):
            pass

    class FakeConn:
        def cursor(self):
            return FakeCursor()

        def close(self):
            pass

    monkeypatch.setattr(migrations, "_connect", lambda database=None: FakeConn())
    monkeypatch.setattr(
        migrations, "pending", lambda cursor, d=None: discover()
    )
    monkeypatch.setattr(
        migrations, "apply_all",
        lambda **kwargs: applied.append(kwargs) or [],
    )

    state = migrations.ensure_schema_current(apply=False)

    assert not state.ok
    assert state.reason == "stale"
    assert applied == [], "apply_all was called with auto-migration disabled"
    assert "scripts/migrate.py" not in state.detail  # the caller says that
    assert "pending" in state.detail
