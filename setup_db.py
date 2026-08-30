"""
One-click database setup for the AI Attendance System.

Creates the configured database, applies every schema migration, and seeds the
default admin user. Connection details come from config/settings.py (see
.env.example), so this script and the application can no longer disagree about
which server or database they are talking to.

    python setup_db.py

**PO-5 is closed here.** This file used to restate all five CREATE TABLE
blocks, so the schema was defined twice - once here and once in
database/schema.sql - and the two had to be kept in step by hand. Both are gone;
`migrations/` is the single source of schema truth, and this script now does
only the two things a migration cannot: create the database it will run
against, and seed a credential whose hash has to be generated in Python.

`column_exists()` and `ensure_column()` survive because
scripts/migrate_passwords.py imports them, and because they remain the right
tool for a data migration that has to inspect the schema. They are no longer
used to *define* it.
"""

import logging
import re

import mysql.connector

from config.logging_config import configure_logging
from config.settings import settings
from infra.migrations import MigrationError, apply_all
from security.passwords import hash_password

logger = logging.getLogger(__name__)

# MySQL identifiers cannot be passed as query parameters, so the database name
# is interpolated into the DDL. It comes from configuration rather than from a
# request, but interpolating an unvalidated string into SQL is the habit that
# produces injection bugs, so it is checked against a strict allowlist first.
_VALID_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def column_exists(cursor, database, table, column):
    """True if `table` already has `column`. Read-only."""
    cursor.execute(
        """
        SELECT COUNT(*)
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND COLUMN_NAME = %s
        """,
        (database, table, column),
    )

    row = cursor.fetchone()

    # Callers use both plain and dictionary cursors.
    count = row["COUNT(*)"] if isinstance(row, dict) else row[0]

    return bool(count)


def ensure_column(cursor, database, table, column, definition):
    """
    Add `column` to `table` if it is not already there. Returns True if the
    column was added.

    `CREATE TABLE IF NOT EXISTS` is a no-op on an existing table, so it will
    happily leave a database several columns behind the schema in this file
    and report success. Every column added after the first release therefore
    needs an explicit check. MySQL has no `ADD COLUMN IF NOT EXISTS`, so the
    check goes through information_schema.

    Identifiers are validated by the same allowlist as the database name -
    they cannot be passed as query parameters, and interpolating unchecked
    strings into DDL is the habit that produces injection bugs. `definition`
    is a SQL fragment and is deliberately not validated: it must only ever be
    a literal written in this repository, never anything from a request.
    """
    for identifier in (table, column):
        if not _VALID_IDENTIFIER.match(identifier):
            raise ValueError(f"Refusing to use {identifier!r} as a SQL identifier")

    if column_exists(cursor, database, table, column):
        return False

    cursor.execute(f"ALTER TABLE `{table}` ADD COLUMN `{column}` {definition}")
    logger.info("Added column %s.%s", table, column)
    return True


def setup_database(host=None, user=None, password=None, database=None):
    """
    Create the database, bring the schema up to date, seed the admin account.

    ⚠️ `host`, `user` and `password` override only the *bootstrap* connection
    made here, the one that issues CREATE DATABASE. The migration step opens
    its own connection from config/settings.py. Nothing in the repository
    passes these arguments, and pointing them somewhere the configuration does
    not agree with would migrate one server while creating a database on
    another. `database` is safe to pass: it is forwarded to the runner.
    """
    host = host if host is not None else settings.db_host
    user = user if user is not None else settings.db_user
    password = password if password is not None else settings.db_password
    database = database if database is not None else settings.db_name

    if not _VALID_IDENTIFIER.match(database):
        logger.error(
            "Refusing to use %r as a database name: letters, digits and "
            "underscores only, and it may not start with a digit.",
            database,
        )
        return False

    logger.info("AI Attendance System - MySQL database setup")

    # Connect to the MySQL server without selecting a database, since the
    # database may not exist yet.
    try:
        conn = mysql.connector.connect(
            host=host,
            port=settings.db_port,
            user=user,
            password=password,
        )
        cursor = conn.cursor()
        logger.info("Connected to MySQL server at %s:%s", host, settings.db_port)
    except mysql.connector.Error:
        logger.exception(
            "Could not connect to the MySQL server. Check that MySQL "
            "(XAMPP/WAMP/Workbench) is running and that the credentials in "
            ".env are correct."
        )
        return False

    try:
        cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{database}`")
        logger.info("Database %r verified/created", database)

        cursor.execute(f"USE `{database}`")

    except mysql.connector.Error:
        logger.exception("Could not create or select the database")
        cursor.close()
        conn.close()
        return False

    # Schema. Every table and every column comes from migrations/ now - this
    # script no longer restates any of it (PO-5).
    #
    # The migration runner opens its own connection, because it needs the
    # database selected from the start and this one was opened without it.
    try:
        applied = apply_all(database=database)
        logger.info("Schema is current; %d migration(s) applied", len(applied))
    except MigrationError:
        logger.exception("Schema migration failed; the database is not ready")
        cursor.close()
        conn.close()
        return False

    try:
        # Seed Default Admin
        #
        # The credential is still admin/admin and is still public knowledge -
        # but it is now stored as a bcrypt hash (SE-1) and flagged so the
        # account can do nothing except change it (see security/access.py).
        cursor.execute("SELECT * FROM admin WHERE id = 1")
        if cursor.fetchone() is None:
            cursor.execute(
                "INSERT INTO admin (id, username, password, must_change_password) "
                "VALUES (1, 'admin', %s, 1)",
                (hash_password("admin"),),
            )
            conn.commit()
            logger.warning(
                "Seeded the default admin account (username 'admin', "
                "password 'admin'). You will be required to change the "
                "password at first login."
            )
        else:
            logger.info("Admin user already exists")

        logger.info("Database setup completed cleanly. You can now run: python app.py")
        return True

    except mysql.connector.Error:
        logger.exception("SQL execution error during database setup")
        return False
    finally:
        cursor.close()
        conn.close()


if __name__ == "__main__":
    configure_logging()
    setup_database()
