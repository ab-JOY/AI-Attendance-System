"""
Schema migrations: one ordered set of SQL files, applied once each (PO-5).

Before this, the schema was defined twice - in `database/schema.sql` and again
in `setup_db.py` - and the two had to be kept in step by hand. Worse, both were
built from `CREATE TABLE IF NOT EXISTS`, which is a no-op on a database that
already has the table. A column added to both files would therefore appear on a
fresh install and silently never appear on an existing one. `setup_db.py` had
already grown an `ensure_column()` shim to work around exactly that for one
column on two tables; migrations are that idea done properly.

**How it works.** `migrations/NNN_name.sql`, applied in filename order. A
`schema_migrations` table records which versions have run, so applying twice is
a no-op and a new file is picked up on the next run.

⚠️ **MySQL cannot roll back DDL.** `CREATE TABLE`, `ALTER TABLE` and friends
commit implicitly, and no amount of transaction handling here changes that. A
migration that fails halfway leaves the database *between* states, and the
version is not recorded, so a re-run will replay the statements that already
succeeded. Two consequences, and they are requirements on the SQL rather than on
this module:

  1. **Keep each file to one coherent change**, so "halfway" is a small place.
  2. **Prefer statements that are safe to replay** - `CREATE TABLE IF NOT
     EXISTS`, and the `information_schema` guards in `setup_db.py` for columns
     and indexes.

⚠️ **This opens its own connection and does not use `infra/db.py`.** Migrations
run before the application, may need to run against a database the pool is not
configured for (a scratch schema in the integration tests), and must not depend
on a pool whose own schema assumptions they are about to change.
`tests/test_db_access.py` excludes this module for that reason, and the
exclusion is deliberate rather than an oversight.
"""

from __future__ import annotations

import contextlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import mysql.connector

from config.settings import BASE_DIR, settings

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = BASE_DIR / "migrations"

# `001_baseline.sql` -> version "001", name "baseline". The numeric prefix is
# what orders them, and it is required: a file that does not match is a
# mistake, not a migration to be applied in some arbitrary position.
_FILENAME = re.compile(r"^(\d{3,})_([A-Za-z0-9_]+)\.sql$")

# MySQL identifiers cannot be bound as query parameters. Same allowlist and same
# reasoning as setup_db.py.
_VALID_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

SCHEMA_MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version VARCHAR(255) NOT NULL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


class MigrationError(RuntimeError):
    """A migration file is malformed, or applying one failed."""


@dataclass(frozen=True)
class Migration:
    """One migration file on disk."""

    version: str
    name: str
    path: Path

    @property
    def label(self) -> str:
        return f"{self.version}_{self.name}"

    def statements(self) -> list[str]:
        """
        The file split into individual statements.

        mysql-connector will execute several statements in one call only with
        `multi=True`, whose result handling is awkward and whose errors are
        reported against the batch rather than the statement. Splitting here
        means a failure names the statement that caused it.

        The split is on `;` at end of line, which is enough for DDL written in
        this repository. It is *not* a SQL parser: a semicolon inside a string
        literal or a stored-procedure body would defeat it. If a migration ever
        needs one of those, give it its own runner rather than making this
        cleverer.
        """
        text = self.path.read_text(encoding="utf-8")

        statements = []

        for chunk in text.split(";\n"):
            # Strip comment-only and blank lines so an all-comment tail does not
            # become an empty statement.
            lines = [
                line
                for line in chunk.splitlines()
                if line.strip() and not line.strip().startswith("--")
            ]

            statement = "\n".join(lines).strip().rstrip(";").strip()

            if statement:
                statements.append(statement)

        return statements


def discover(migrations_dir: Path | None = None) -> list[Migration]:
    """
    Every migration file, in version order.

    Raises MigrationError on a file that does not match `NNN_name.sql`, or on
    two files sharing a version. A silently skipped migration is the failure
    mode this whole module exists to prevent, so an unrecognised `.sql` file in
    the directory is an error rather than something to ignore.
    """
    directory = migrations_dir if migrations_dir is not None else MIGRATIONS_DIR

    if not directory.is_dir():
        raise MigrationError(f"No migrations directory at {directory}")

    migrations: list[Migration] = []
    seen: dict[str, Path] = {}

    for path in sorted(directory.glob("*.sql")):
        match = _FILENAME.match(path.name)

        if match is None:
            raise MigrationError(
                f"{path.name} is not a valid migration filename. "
                "Migrations are named NNN_lower_case_name.sql"
            )

        version, name = match.group(1), match.group(2)

        if version in seen:
            raise MigrationError(
                f"Two migrations share version {version}: "
                f"{seen[version].name} and {path.name}"
            )

        seen[version] = path
        migrations.append(Migration(version=version, name=name, path=path))

    return migrations


def _connect(database: str | None = None):
    """
    A plain connection with the target database selected.

    `database` defaults to the configured one. The integration tests pass a
    scratch schema name, which is the reason this is a parameter at all.
    """
    target = database if database is not None else settings.db_name

    if not _VALID_IDENTIFIER.match(target):
        raise MigrationError(f"Refusing to use {target!r} as a database name")

    kwargs = settings.db_kwargs()
    kwargs["database"] = target

    return mysql.connector.connect(**kwargs)


def applied_versions(cursor) -> set[str]:
    """Versions already recorded as applied. Creates the ledger if absent."""
    cursor.execute(SCHEMA_MIGRATIONS_TABLE)
    cursor.execute("SELECT version FROM schema_migrations")

    rows = cursor.fetchall()

    return {row["version"] if isinstance(row, dict) else row[0] for row in rows}


def pending(cursor, migrations_dir: Path | None = None) -> list[Migration]:
    """Migrations on disk that the ledger has not recorded."""
    already = applied_versions(cursor)

    return [m for m in discover(migrations_dir) if m.version not in already]


def apply_all(
    database: str | None = None,
    migrations_dir: Path | None = None,
    dry_run: bool = False,
) -> list[Migration]:
    """
    Apply every pending migration in order. Returns the ones applied.

    With `dry_run=True` nothing is executed and nothing is recorded; the return
    value is what *would* run.
    """
    conn = _connect(database)

    try:
        cursor = conn.cursor()

        try:
            outstanding = pending(cursor, migrations_dir)

            if not outstanding:
                logger.info("Schema is up to date; no migrations to apply")
                return []

            if dry_run:
                for migration in outstanding:
                    logger.info("Would apply %s", migration.label)
                return outstanding

            for migration in outstanding:
                _apply_one(conn, cursor, migration)

            return outstanding

        finally:
            cursor.close()

    finally:
        conn.close()


def _apply_one(conn, cursor, migration: Migration) -> None:
    """
    Run one migration and record it.

    The record is written *after* the statements, so an interrupted migration
    is not marked done. See the module docstring on why that still leaves work
    for the SQL author: DDL has already committed by then.
    """
    logger.info("Applying migration %s", migration.label)

    for index, statement in enumerate(migration.statements(), start=1):
        try:
            cursor.execute(statement)

            # Some statements return a result set (a SELECT used as a guard).
            # Leaving it unread poisons the connection with "Unread result
            # found" on the next execute.
            if cursor.with_rows:
                cursor.fetchall()

        except mysql.connector.Error as error:
            conn.rollback()
            raise MigrationError(
                f"Migration {migration.label} failed at statement {index}: {error}"
            ) from error

    cursor.execute(
        "INSERT INTO schema_migrations (version, name) VALUES (%s, %s)",
        (migration.version, migration.name),
    )
    conn.commit()

    logger.info("Applied migration %s", migration.label)


# Server error numbers worth telling apart. Everything else is a genuine
# fault and is reported as one.
ER_BAD_DB_ERROR = 1049          # the database does not exist
CR_CONN_HOST_ERROR = 2003       # the server is not accepting connections


class SchemaState:
    """What a start-up schema check found. `ok` is the only thing to branch on."""

    __slots__ = ("ok", "reason", "detail", "applied")

    def __init__(self, ok, reason, detail="", applied=()):
        self.ok = ok
        self.reason = reason
        self.detail = detail
        self.applied = list(applied)

    def __repr__(self):
        return f"SchemaState(ok={self.ok}, reason={self.reason!r})"


def ensure_schema_current(
    database: str | None = None,
    migrations_dir: Path | None = None,
    apply: bool = True,
) -> SchemaState:
    """
    Check the schema at start-up and, by default, bring it up to date.

    This exists because the system is deployed by copying it to another
    machine. Someone pulling a newer version onto a database created by an
    older one previously got no warning at all: `CREATE TABLE IF NOT EXISTS`
    is a no-op on an existing table, so the application started happily and
    then failed on whichever page first touched a column that was never added.
    An attendance system that boots and *then* 500s on the register is worse
    than one that refuses to boot.

    Returns a SchemaState rather than raising, because the caller has to be
    able to print something an operator can act on for each distinct cause -
    "start MySQL", "run setup_db.py", "a migration failed" are three different
    instructions.

    ⚠️ **Deliberately not called at import time.** The test suite imports
    `app`, and CI has no database at all; a connection attempt at import would
    make the whole suite depend on a running server. It is called from the
    `__main__` block, which is the moment a person actually starts the
    application.

    ⚠️ **Single process only.** Two workers starting at once would both try to
    apply the same migration. That is safe today because the deployment is one
    Flask development server (PO-4); it stops being safe the moment it is not,
    and this needs a lock before it runs anywhere else.
    """
    try:
        conn = _connect(database)

    except mysql.connector.Error as error:
        if error.errno == ER_BAD_DB_ERROR:
            return SchemaState(
                False,
                "no-database",
                f"The database {database or settings.db_name!r} does not exist. "
                "Run: python setup_db.py",
            )

        if error.errno == CR_CONN_HOST_ERROR:
            return SchemaState(
                False,
                "no-server",
                f"No database server is answering on {settings.db_host}:"
                f"{settings.db_port}. Start MySQL/MariaDB (in XAMPP, the MySQL "
                "row's Start button) and try again.",
            )

        return SchemaState(False, "connection-failed", str(error))

    except MigrationError as error:
        return SchemaState(False, "configuration", str(error))

    try:
        cursor = conn.cursor()

        try:
            outstanding = pending(cursor, migrations_dir)

        finally:
            cursor.close()

    except mysql.connector.Error as error:
        conn.close()
        return SchemaState(False, "unreadable", str(error))

    finally:
        with contextlib.suppress(Exception):
            conn.close()

    if not outstanding:
        return SchemaState(True, "current")

    if not apply:
        labels = ", ".join(m.label for m in outstanding)

        return SchemaState(
            False,
            "stale",
            f"{len(outstanding)} migration(s) pending: {labels}",
        )

    try:
        applied = apply_all(database=database, migrations_dir=migrations_dir)

    except MigrationError as error:
        return SchemaState(False, "migration-failed", str(error))

    return SchemaState(True, "migrated", applied=applied)


def status(
    database: str | None = None,
    migrations_dir: Path | None = None,
) -> list[tuple[Migration, bool]]:
    """Every migration paired with whether it has been applied."""
    conn = _connect(database)

    try:
        cursor = conn.cursor()

        try:
            already = applied_versions(cursor)
            return [(m, m.version in already) for m in discover(migrations_dir)]
        finally:
            cursor.close()

    finally:
        conn.close()
