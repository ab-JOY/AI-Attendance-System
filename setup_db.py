"""
One-click MySQL database setup for the AI Attendance System.

Creates the configured database, all required tables, and the default admin
user. Connection details come from config/settings.py (see .env.example), so
this script and the application can no longer disagree about which server or
database they are talking to.

    python setup_db.py

Known limitation, unchanged here: this file and database/schema.sql both
define the schema and must be kept in sync by hand (PO-5). Replacing both
with migrations is Phase 4 work.
"""

import logging
import re

import mysql.connector

from config.logging_config import configure_logging
from config.settings import settings
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

        # 1. Admin Table
        #
        # `password` holds a bcrypt hash, never a plaintext password (SE-1),
        # and is never compared in SQL (SE-15). See security/passwords.py.
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS admin (
            id INT AUTO_INCREMENT PRIMARY KEY,
            username VARCHAR(100) NOT NULL UNIQUE,
            password VARCHAR(255) NOT NULL,
            must_change_password TINYINT(1) NOT NULL DEFAULT 0
        )
        """)
        logger.info("Table 'admin' verified")

        # 2. Instructors Table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS instructors (
            id INT AUTO_INCREMENT PRIMARY KEY,
            instructor_id VARCHAR(100) NOT NULL UNIQUE,
            fullname VARCHAR(255) NOT NULL,
            password VARCHAR(255) NOT NULL,
            must_change_password TINYINT(1) NOT NULL DEFAULT 0
        )
        """)
        logger.info("Table 'instructors' verified")

        # 3. Students Table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS students (
            student_id VARCHAR(100) PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            college_department VARCHAR(255),
            program VARCHAR(255),
            year_level INT,
            section VARCHAR(100)
        )
        """)
        logger.info("Table 'students' verified")

        # 4. Subjects Table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS subjects (
            id INT AUTO_INCREMENT PRIMARY KEY,
            subject_code VARCHAR(100) NOT NULL,
            subject_name VARCHAR(255) NOT NULL,
            instructor VARCHAR(255),
            day VARCHAR(100),
            course VARCHAR(100),
            section VARCHAR(100),
            time_in VARCHAR(50),
            time_out VARCHAR(50)
        )
        """)
        logger.info("Table 'subjects' verified")

        # 5. Attendance Table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS attendance (
            id INT AUTO_INCREMENT PRIMARY KEY,
            student_id VARCHAR(100) NOT NULL,
            student_name VARCHAR(255) NOT NULL,
            subject_code VARCHAR(100) NOT NULL,
            attendance_date DATE NOT NULL,
            time_in TIME NOT NULL,
            status VARCHAR(50) NOT NULL,
            INDEX idx_student (student_id),
            INDEX idx_date_subject (attendance_date, subject_code)
        )
        """)
        logger.info("Table 'attendance' verified")

        # Columns added after the first release. The CREATE TABLE statements
        # above do nothing on a database that already exists, so these have
        # to be applied separately.
        for table in ("admin", "instructors"):
            ensure_column(
                cursor,
                database,
                table,
                "must_change_password",
                "TINYINT(1) NOT NULL DEFAULT 0",
            )

        conn.commit()

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
