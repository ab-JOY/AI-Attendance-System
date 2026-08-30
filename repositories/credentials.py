"""
The two credential tables, `admin` and `instructors`.

⚠️ **No password is ever compared here, and that is SE-15 rather than
tidiness.** Authentication used to be `WHERE username=%s AND password=%s`,
which let MySQL decide the match - and the column collation is
`utf8mb4_general_ci`, case-insensitive and PAD SPACE, so `ADMIN` and
`admin   ` both authenticated as `admin` (measured against the live database).
bcrypt fixes the password half and does nothing for the username half.

Every function here *fetches a row by identifier*. `security/passwords.py`
decides, in Python, whether it matches - and the caller re-checks the
identifier for the same reason.
"""

from __future__ import annotations

# The two tables a signed-in account can belong to, and how a row in each is
# addressed. Written down once here rather than assembled at the call site:
# `change_password` interpolates the table and key column into its statements,
# so the set of acceptable values has to be a fixed literal that no request can
# reach.
CREDENTIAL_TABLES = {
    "admin": ("admin", "id"),
    "instructor": ("instructors", "instructor_id"),
}


def admin_by_username(cursor, username):
    cursor.execute(
        "SELECT * FROM admin WHERE username=%s",
        (username,)
    )

    return cursor.fetchone()


def instructor_by_id(cursor, instructor_id):
    cursor.execute(
        "SELECT * FROM instructors WHERE instructor_id=%s",
        (instructor_id,)
    )

    return cursor.fetchone()


def admin_by_id(cursor, admin_id):
    """
    One administrator account by primary key, for the settings screen.

    ⚠️ **This replaced `the_admin()`, which was `SELECT * FROM admin LIMIT 1`
    (SE-16, R8).** Login authenticates *any* row in this table by username, but
    the account screens assumed there was only ever one: `/settings` displayed
    whichever row LIMIT 1 happened to return and `rename_admin()` wrote
    `WHERE id=1`. With a second administrator, one of them would have edited
    the other's account and been shown the other's name while doing it.

    Latent on this deployment - the table holds one row - and cheap to close.
    SE-16 already fixed the *role* half of this: /change_password used to
    update `admin WHERE id=1` no matter who was signed in, so an instructor
    could not change their own password at all. The hardcoded key was carried
    over in the fix.
    """
    cursor.execute("SELECT * FROM admin WHERE id = %s", (admin_id,))
    return cursor.fetchone()


def rename_admin(cursor, admin_id, username):
    """Rename one administrator. See `admin_by_id()` on the key."""
    cursor.execute(
        "UPDATE admin SET username=%s WHERE id=%s",
        (username, admin_id),
    )


def password_hash(cursor, table, key_column, key_value):
    """
    The stored hash for one account.

    ⚠️ `table` and `key_column` are interpolated. They come from
    `CREDENTIAL_TABLES` above - a fixed pair of literals chosen by the
    signed-in role - and never from a request; the key value is always a bound
    parameter. Passing anything else here is a SQL injection.
    """
    cursor.execute(
        f"SELECT password FROM `{table}` WHERE `{key_column}` = %s",
        (key_value,)
    )

    return cursor.fetchone()


def set_password(cursor, table, key_column, key_value, new_hash):
    """
    Replace the hash and clear the forced-change flag. See `password_hash()`
    on the interpolation.
    """
    cursor.execute(
        f"UPDATE `{table}` SET password = %s, must_change_password = 0 "
        f"WHERE `{key_column}` = %s",
        (new_hash, key_value)
    )
