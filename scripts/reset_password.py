"""
Recover an account whose password is lost: give it a temporary one.

    python scripts/reset_password.py --list                    # who can sign in
    python scripts/reset_password.py admin admin               # an administrator
    python scripts/reset_password.py instructor INS-001        # an instructor

For the case the website cannot handle: **the administrator** has forgotten
their password, or has left and nobody knows it. An instructor who forgets
theirs does not need this - an administrator sets a new one on the website
(Manage Instructors -> Edit). This is for when there is no administrator who
can sign in.

It runs on the server, against the database in `.env`, so only someone who
already controls the machine can use it - the school's IT custodian, not a
visitor to the website. That is the same trust boundary as `setup_db.py`.

What it does:

1. Generates a random temporary password and prints it once. It is never
   taken from the command line, where it would stay in the shell history.
2. Stores it as a bcrypt hash and sets `must_change_password = 1`, so the
   temporary password opens nothing but the change-password page.

The identifier is matched exactly, as at login (SE-15). The login lockout
(SE-13) is held in the running server's memory: restart `serve.py` if the
account is currently locked.
"""

from __future__ import annotations

import argparse
import logging
import secrets
import sys
from pathlib import Path

import mysql.connector

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.logging_config import configure_logging  # noqa: E402
from config.settings import settings  # noqa: E402
from security.passwords import hash_password  # noqa: E402

logger = logging.getLogger(__name__)

# (table, the column the person types at login). Fixed literals: both are
# interpolated into the statements below and no argument can reach them.
ACCOUNT_TABLES = {
    "admin": ("admin", "username"),
    "instructor": ("instructors", "instructor_id"),
}

# 12 characters from token_urlsafe: about 72 bits, readable over the phone,
# and well inside bcrypt's 72-byte limit.
TEMPORARY_PASSWORD_BYTES = 9


def temporary_password() -> str:
    return secrets.token_urlsafe(TEMPORARY_PASSWORD_BYTES)


def list_accounts(cursor) -> dict[str, list[str]]:
    """Every identifier that can sign in, by role."""
    found = {}

    for role, (table, column) in ACCOUNT_TABLES.items():
        cursor.execute(f"SELECT `{column}` FROM `{table}` ORDER BY `{column}`")
        found[role] = [row[0] for row in cursor.fetchall()]

    return found


def reset_password(cursor, role: str, identifier: str, new_hash: str) -> bool:
    """
    Store `new_hash` for one account and force a change at next login.

    Returns False if no account has exactly that identifier. The lookup is
    by the collation (case-insensitive), so the identifier is re-checked in
    Python before anything is written - otherwise `ADMIN` would reset `admin`.
    """
    table, column = ACCOUNT_TABLES[role]

    cursor.execute(
        f"SELECT id, `{column}` FROM `{table}` WHERE `{column}` = %s",
        (identifier,),
    )
    matches = [row for row in cursor.fetchall() if row[1] == identifier]

    if len(matches) != 1:
        return False

    cursor.execute(
        f"UPDATE `{table}` SET password = %s, must_change_password = 1 "
        f"WHERE id = %s",
        (new_hash, matches[0][0]),
    )

    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("role", nargs="?", choices=sorted(ACCOUNT_TABLES))
    parser.add_argument("identifier", nargs="?",
                        help="admin username or instructor ID, exactly as typed at login")
    parser.add_argument("--list", action="store_true",
                        help="Show every admin username and instructor ID, then stop.")
    arguments = parser.parse_args(argv)

    if not arguments.list and not (arguments.role and arguments.identifier):
        parser.error("give a role and an identifier, or --list")

    configure_logging()

    try:
        conn = mysql.connector.connect(**settings.db_kwargs())
    except mysql.connector.Error:
        logger.exception(
            "Could not connect to the database. Check that MySQL is running "
            "and that the credentials in .env are correct."
        )
        return 1

    try:
        cursor = conn.cursor()

        if arguments.list:
            for role, identifiers in list_accounts(cursor).items():
                print(f"{role}: {', '.join(identifiers) or '(none)'}")
            return 0

        password = temporary_password()

        if not reset_password(cursor, arguments.role, arguments.identifier,
                              hash_password(password)):
            conn.rollback()
            print(f"No {arguments.role} account is exactly "
                  f"{arguments.identifier!r}. Use --list to see them.")
            return 1

        conn.commit()

    except mysql.connector.Error:
        conn.rollback()
        logger.exception("The reset failed and was rolled back")
        return 1

    finally:
        conn.close()

    logger.warning("Password reset for %s %r from the command line",
                   arguments.role, arguments.identifier)

    print(f"Temporary password for {arguments.role} {arguments.identifier!r}: "
          f"{password}")
    print("It opens only the change-password page: a new password must be "
          "chosen at sign-in.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
