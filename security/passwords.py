"""
Password hashing and verification (SE-1, SE-15).

Two separate defects are fixed here, and the second one is the subtle one.

**SE-1** - passwords were stored in plaintext. Anyone with read access to the
database, a backup, or a dump had every credential in the system.

**SE-15** - passwords were compared *inside the SQL query*:

    SELECT * FROM admin WHERE username=%s AND password=%s

MySQL, not Python, decided whether the two strings matched, and the column
collation is `utf8mb4_general_ci` - case-insensitive and PAD SPACE. Measured
against the live database in Phase 1: the password `admin` was accepted as
`ADMIN`, `AdMiN` and `admin   `. That collapses an alphanumeric alphabet from
62 symbols to 36; an 8-character password drops from ~2.2e14 to ~2.8e12
combinations, roughly 79x weaker.

So the fix is not merely "hash the passwords". It is **compare in Python**.
A bcrypt digest is opaque bytes, and `bcrypt.checkpw` is an exact
constant-time comparison, so collation never gets a vote again. Nothing in
this codebase may put a password into a WHERE clause, hashed or not - fetch
the row by identifier, then call `verify_password()`.

The same collation still applies to the *username* lookup. bcrypt does not
help there, so the caller has to compare the identifier in Python too; see
the login route in app.py.
"""

from __future__ import annotations

import bcrypt

# bcrypt hashes to a fixed 60-character string; the `password` columns are
# VARCHAR(255), so there is ample room and no schema change is needed for
# the hash itself.
_BCRYPT_PREFIXES = ("$2a$", "$2b$", "$2x$", "$2y$")

# bcrypt raises ValueError above 72 bytes rather than truncating silently
# (verified with bcrypt 5.0.0). Silently truncating would mean two different
# long passwords authenticating each other, so the limit is enforced at the
# boundary instead and surfaced to the user as a validation error.
MAX_PASSWORD_BYTES = 72

# Credentials that ship with the system and are therefore public knowledge.
# An account still using one of these is flagged `must_change_password`.
KNOWN_DEFAULT_PASSWORDS = ("admin",)


class PasswordTooLongError(ValueError):
    """Raised when a password exceeds what bcrypt can hash without truncation."""


def hash_password(password: str) -> str:
    """
    Return a bcrypt hash of `password`, suitable for storing in a VARCHAR(255).

    Raises PasswordTooLongError if the password would be truncated. Callers
    must turn that into a user-facing validation message, never swallow it.
    """
    encoded = password.encode("utf-8")

    if len(encoded) > MAX_PASSWORD_BYTES:
        raise PasswordTooLongError(
            f"Password must be at most {MAX_PASSWORD_BYTES} bytes when "
            f"UTF-8 encoded; got {len(encoded)}."
        )

    return bcrypt.hashpw(encoded, bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, stored_hash: str | None) -> bool:
    """
    Return True only if `password` is exactly the password behind `stored_hash`.

    Exact means exact: case, trailing whitespace and Unicode form all matter,
    which is the whole point of SE-15.

    Returns False - never raises - for a missing hash, a plaintext value left
    behind by an unmigrated row, or any other malformed input. A row that has
    not been through scripts/migrate_passwords.py must fail to authenticate,
    not crash the login route: `bcrypt.checkpw` raises ValueError("Invalid
    salt") when handed a plaintext string, and an unhandled exception there
    would be an availability bug introduced by a security fix.
    """
    if not stored_hash:
        return False

    encoded = password.encode("utf-8")

    if len(encoded) > MAX_PASSWORD_BYTES:
        # Cannot be the password behind any hash we could have produced,
        # because hash_password() refuses to create one.
        return False

    try:
        return bcrypt.checkpw(encoded, stored_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def is_hashed(stored_value: str | None) -> bool:
    """
    True if `stored_value` looks like a bcrypt hash rather than a plaintext
    password. Used by the migration script to stay idempotent.
    """
    if not stored_value:
        return False

    return stored_value.startswith(_BCRYPT_PREFIXES)


def is_known_default(password_or_hash: str | None) -> bool:
    """
    True if the stored value is - or hashes - one of the credentials this
    system ships with. Drives the `must_change_password` flag.
    """
    if not password_or_hash:
        return False

    if is_hashed(password_or_hash):
        return any(
            verify_password(default, password_or_hash)
            for default in KNOWN_DEFAULT_PASSWORDS
        )

    return password_or_hash in KNOWN_DEFAULT_PASSWORDS
