"""
Regression tests for SE-1 (plaintext passwords) and SE-15 (case-insensitive
comparison).

SE-15 is the one to read carefully. Phase 1 measured it against the live
database: with passwords compared inside the SQL query and the column in
`utf8mb4_general_ci`, the password `admin` was accepted as `ADMIN`, `AdMiN`
and `admin   `. The three tests below encode exactly that, inverted - they
fail against the old plaintext-in-SQL comparison and pass only because
verification now happens in Python over a bcrypt digest.
"""

import pytest

from security.passwords import (
    MAX_PASSWORD_BYTES,
    PasswordTooLongError,
    hash_password,
    is_hashed,
    is_known_default,
    verify_password,
)

# bcrypt is deliberately slow - that is the point of it - so the fixed
# "admin" digest is computed once here rather than inside each of the
# parametrised rejection tests.
ADMIN_HASH = hash_password("admin")


def test_hash_is_bcrypt_and_not_the_password():
    assert ADMIN_HASH != "admin"
    assert ADMIN_HASH.startswith("$2b$")
    assert is_hashed(ADMIN_HASH)


def test_the_same_password_hashes_differently_every_time():
    """Distinct salts: two accounts sharing a password must not look alike."""
    assert hash_password("admin") != hash_password("admin")


def test_correct_password_verifies():
    assert verify_password("admin", ADMIN_HASH) is True


@pytest.mark.parametrize("variant", ["ADMIN", "AdMiN", "aDMIN"])
def test_case_variants_are_rejected(variant):
    """SE-15: `utf8mb4_general_ci` used to accept every one of these."""
    assert verify_password(variant, ADMIN_HASH) is False


@pytest.mark.parametrize("variant", ["admin ", "admin   ", " admin"])
def test_whitespace_variants_are_rejected(variant):
    """SE-15: the collation is PAD SPACE, so trailing spaces were ignored."""
    assert verify_password(variant, ADMIN_HASH) is False


def test_wrong_password_is_rejected():
    assert verify_password("hunter2", ADMIN_HASH) is False


def test_a_plaintext_row_fails_closed_instead_of_raising():
    """
    An unmigrated row still holds a plaintext password. bcrypt.checkpw raises
    ValueError("Invalid salt") on one of those, and an unhandled exception in
    the login route would turn a security fix into an outage.
    """
    assert verify_password("admin", "admin") is False
    assert verify_password("anything", "not-a-hash") is False


@pytest.mark.parametrize("stored", [None, ""])
def test_missing_hash_is_rejected(stored):
    assert verify_password("admin", stored) is False
    assert is_hashed(stored) is False


def test_password_longer_than_bcrypt_supports_is_refused_not_truncated():
    """
    bcrypt caps input at 72 bytes. Truncating silently would mean two
    different long passwords authenticating each other, so hashing refuses.
    """
    too_long = "x" * (MAX_PASSWORD_BYTES + 1)

    with pytest.raises(PasswordTooLongError):
        hash_password(too_long)

    # And verification of an over-long candidate is False, never an exception.
    assert verify_password(too_long, hash_password("x" * MAX_PASSWORD_BYTES)) is False


def test_maximum_length_password_still_works():
    at_limit = "x" * MAX_PASSWORD_BYTES

    assert verify_password(at_limit, hash_password(at_limit)) is True


def test_non_ascii_password_round_trips():
    """UTF-8 encoding is explicit, so a multi-byte password must survive it."""
    assert verify_password("contraseña-ñ", hash_password("contraseña-ñ")) is True


def test_known_default_is_detected_in_both_forms():
    """Drives the must_change_password flag, for hashed and unmigrated rows."""
    assert is_known_default("admin") is True
    assert is_known_default(ADMIN_HASH) is True

    assert is_known_default("a-real-password") is False
    assert is_known_default(hash_password("a-real-password")) is False
    assert is_known_default(None) is False
