"""
One-off migration: plaintext passwords -> bcrypt hashes (SE-1).

    python scripts/migrate_passwords.py --dry-run   # show what would change
    python scripts/migrate_passwords.py             # apply

What it does, in order:

1. Adds `must_change_password` to `admin` and `instructors` if the columns
   are missing. `CREATE TABLE IF NOT EXISTS` in setup_db.py cannot do this on
   a database that already exists.
2. Replaces any password that is not already a bcrypt hash with one.
3. Sets `must_change_password = 1` on every account still using a credential
   this system ships with, so it can do nothing but change it.

Idempotent: a second run reports zero changes. Safe to run before or after
`setup_db.py`.

**This is one-way.** Hashing is not reversible, so an account whose plaintext
password was lost or mistyped in the database can only be recovered by
setting a new one. Take a dump first:

    mysqldump attendancesystem_db > backup.sql

Note on scope: this migrates *credentials*. It does not touch `students`,
`subjects` or `attendance`.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import mysql.connector

# The project uses a flat layout with modules at the repository root, and this
# script lives one level down in scripts/.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.logging_config import configure_logging  # noqa: E402
from config.settings import settings  # noqa: E402
from security.passwords import hash_password, is_hashed, is_known_default  # noqa: E402
from setup_db import column_exists, ensure_column  # noqa: E402

logger = logging.getLogger(__name__)

# (table, the human-readable identifier column used in log lines)
CREDENTIAL_TABLES = (
    ("admin", "username"),
    ("instructors", "instructor_id"),
)


def migrate(dry_run: bool = False) -> bool:
    """Returns True on success. Logs a summary of everything it changed."""
    action = "Would apply" if dry_run else "Applying"
    logger.info("%s the password migration to %s", action, settings.db_name)

    try:
        conn = mysql.connector.connect(**settings.db_kwargs())
    except mysql.connector.Error:
        logger.exception(
            "Could not connect to the database. Check that MySQL is running "
            "and that the credentials in .env are correct."
        )
        return False

    hashed_count = 0
    flagged_count = 0
    already_done = 0

    try:
        cursor = conn.cursor(dictionary=True)

        for table, identifier_column in CREDENTIAL_TABLES:
            has_flag_column = column_exists(
                cursor, settings.db_name, table, "must_change_password"
            )

            if not has_flag_column:
                logger.info("%s: adding %s.must_change_password", action, table)

                if not dry_run:
                    ensure_column(
                        cursor,
                        settings.db_name,
                        table,
                        "must_change_password",
                        "TINYINT(1) NOT NULL DEFAULT 0",
                    )
                    conn.commit()
                    has_flag_column = True

            # A dry run leaves the column absent, so it cannot be selected.
            flag_expression = "must_change_password" if has_flag_column else "0"

            cursor.execute(
                f"SELECT id, `{identifier_column}` AS identifier, password, "
                f"{flag_expression} AS must_change_password FROM `{table}`"
            )
            rows = cursor.fetchall()

            for row in rows:
                identifier = row["identifier"]
                stored = row["password"]

                needs_hash = not is_hashed(stored)
                # is_known_default handles both forms, so an already-hashed
                # row that is still the shipped credential is caught too.
                should_flag = is_known_default(stored) and not row["must_change_password"]

                if not needs_hash and not should_flag:
                    already_done += 1
                    continue

                if needs_hash:
                    hashed_count += 1
                    logger.info(
                        "%s: hashing the password for %s.%s",
                        action,
                        table,
                        identifier,
                    )

                if should_flag:
                    flagged_count += 1
                    logger.warning(
                        "%s: %s.%s is still using a default credential and "
                        "will be required to change it at next login",
                        action,
                        table,
                        identifier,
                    )

                if dry_run:
                    continue

                new_password = hash_password(stored) if needs_hash else stored
                cursor.execute(
                    f"UPDATE `{table}` SET password = %s, must_change_password = %s "
                    f"WHERE id = %s",
                    (
                        new_password,
                        1 if (should_flag or row["must_change_password"]) else 0,
                        row["id"],
                    ),
                )

        if not dry_run:
            conn.commit()

        logger.info(
            "Migration summary: %d password(s) hashed, %d account(s) flagged "
            "for a forced change, %d row(s) already correct.",
            hashed_count,
            flagged_count,
            already_done,
        )

        if dry_run:
            logger.info("Dry run - nothing was written.")

        return True

    except mysql.connector.Error:
        conn.rollback()
        logger.exception("The migration failed and was rolled back")
        return False

    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing anything.",
    )
    arguments = parser.parse_args()

    configure_logging()

    return 0 if migrate(dry_run=arguments.dry_run) else 1


if __name__ == "__main__":
    sys.exit(main())
