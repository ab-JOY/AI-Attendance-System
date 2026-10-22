"""
scripts/reset_password.py - recovering an account nobody can sign in to.

No database: a fake cursor holds the rows. The login check at the end uses the
real bcrypt verify, so "the temporary password opens the account" is tested,
not assumed.
"""

from __future__ import annotations

import pytest

from scripts import reset_password as script
from security.passwords import hash_password, verify_password


class FakeCursor:
    """Answers the two statements the script sends, like MySQL's collation."""

    def __init__(self, rows):
        self.rows = rows            # {table: [dict(id, ident, password, flag)]}
        self._result = []

    def execute(self, sql, params=()):
        table = sql.split("FROM `")[-1].split("`")[0] if "FROM" in sql else \
            sql.split("UPDATE `")[1].split("`")[0]

        if sql.startswith("SELECT id"):
            wanted = params[0].lower()   # case-insensitive, like utf8mb4_general_ci
            self._result = [(r["id"], r["ident"]) for r in self.rows[table]
                            if r["ident"].lower() == wanted]
        elif sql.startswith("SELECT"):
            self._result = [(r["ident"],) for r in self.rows[table]]
        else:
            new_hash, row_id = params
            for row in self.rows[table]:
                if row["id"] == row_id:
                    row["password"], row["flag"] = new_hash, 1

    def fetchall(self):
        return self._result


@pytest.fixture
def cursor():
    return FakeCursor({
        "admin": [{"id": 1, "ident": "admin", "password": "old", "flag": 0}],
        "instructors": [{"id": 42, "ident": "INS-001", "password": "old", "flag": 0}],
    })


@pytest.mark.parametrize("role, identifier, table", [
    ("admin", "admin", "admin"),
    ("instructor", "INS-001", "instructors"),
])
def test_the_temporary_password_opens_the_account_and_forces_a_change(
    cursor, role, identifier, table
):
    password = script.temporary_password()

    assert script.reset_password(cursor, role, identifier, hash_password(password))

    row = cursor.rows[table][0]
    assert verify_password(password, row["password"])
    assert row["flag"] == 1


@pytest.mark.parametrize("role, identifier", [
    ("admin", "nobody"),
    ("admin", "ADMIN"),           # SE-15: not reset through the collation
    ("instructor", "ins-001"),
    ("instructor", "admin"),      # an admin username is not an instructor ID
])
def test_an_identifier_that_is_not_exact_changes_nothing(cursor, role, identifier):
    assert not script.reset_password(cursor, role, identifier, "new-hash")

    for rows in cursor.rows.values():
        assert all(row["password"] == "old" and row["flag"] == 0 for row in rows)


def test_list_shows_every_identifier(cursor):
    assert script.list_accounts(cursor) == {
        "admin": ["admin"], "instructor": ["INS-001"],
    }


def test_temporary_passwords_are_random_and_fit_bcrypt():
    passwords = {script.temporary_password() for _ in range(50)}

    assert len(passwords) == 50
    assert all(12 <= len(p) <= 72 for p in passwords)


def test_needs_a_role_and_identifier_or_list():
    with pytest.raises(SystemExit):
        script.main([])
