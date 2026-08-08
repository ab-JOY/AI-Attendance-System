"""
Tests for SE-13 - unbounded credential brute force against `/login`.

The clock is injected rather than slept through: a 15-minute lockout is not
something a test suite can wait for, and `time.sleep` in a unit test is a
slow test that still proves nothing about the boundary.
"""

import pytest

from security.rate_limit import LoginRateLimiter


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def limiter(clock):
    return LoginRateLimiter(
        max_attempts=5,
        lockout_seconds=900.0,
        window_seconds=900.0,
        time_source=clock,
    )


KEY = ("admin", "127.0.0.1")


def test_a_fresh_key_is_not_locked(limiter):
    assert limiter.is_locked(KEY) is False
    assert limiter.seconds_until_unlocked(KEY) == 0.0


def test_attempts_below_the_limit_stay_unlocked(limiter):
    for _ in range(4):
        assert limiter.record_failure(KEY) == 0.0

    assert limiter.is_locked(KEY) is False


def test_the_fifth_failure_locks_the_key(limiter):
    for _ in range(4):
        limiter.record_failure(KEY)

    remaining = limiter.record_failure(KEY)

    assert remaining == pytest.approx(900.0)
    assert limiter.is_locked(KEY) is True


def test_a_successful_login_clears_the_counter(limiter):
    for _ in range(4):
        limiter.record_failure(KEY)

    limiter.record_success(KEY)

    assert limiter.is_locked(KEY) is False
    assert limiter.record_failure(KEY) == 0.0


def test_the_lockout_expires(limiter, clock):
    for _ in range(5):
        limiter.record_failure(KEY)

    clock.advance(899.0)
    assert limiter.is_locked(KEY) is True

    clock.advance(2.0)
    assert limiter.is_locked(KEY) is False


def test_the_budget_resets_after_the_lockout_rather_than_relocking(limiter, clock):
    """
    A user who waits out a lockout and then mistypes once must not be locked
    straight back out - that turns a 15-minute penalty into a permanent one.
    """
    for _ in range(5):
        limiter.record_failure(KEY)

    clock.advance(901.0)

    assert limiter.record_failure(KEY) == 0.0
    assert limiter.is_locked(KEY) is False


def test_old_failures_are_forgiven_after_the_window(limiter, clock):
    """Four typos spread over a month should never accumulate into a lockout."""
    for _ in range(4):
        limiter.record_failure(KEY)
        clock.advance(901.0)

    assert limiter.record_failure(KEY) == 0.0
    assert limiter.is_locked(KEY) is False


def test_locking_one_account_does_not_lock_another(limiter):
    other = ("instructor-1", "127.0.0.1")

    for _ in range(5):
        limiter.record_failure(KEY)

    assert limiter.is_locked(KEY) is True
    assert limiter.is_locked(other) is False


def test_an_attacker_cannot_lock_a_user_out_from_a_different_address(limiter):
    """
    Keying on the address as well as the username stops the lockout being
    used as a denial of service against a legitimate account.
    """
    attacker = ("admin", "10.0.0.9")

    for _ in range(5):
        limiter.record_failure(attacker)

    assert limiter.is_locked(attacker) is True
    assert limiter.is_locked(KEY) is False


def test_the_key_folds_case_so_variants_share_one_budget(limiter):
    """
    Usernames authenticate case-sensitively (SE-15), but `Admin` and `admin`
    are the same guess to an attacker. Counting them apart would double the
    attempt budget for free.
    """
    assert LoginRateLimiter.key("Admin", "127.0.0.1") == LoginRateLimiter.key(
        "  admin  ", "127.0.0.1"
    )


def test_a_missing_address_still_produces_a_usable_key():
    assert LoginRateLimiter.key("admin", None) == ("admin", "unknown")


def test_reset_clears_everything(limiter):
    for _ in range(5):
        limiter.record_failure(KEY)

    limiter.reset()

    assert limiter.is_locked(KEY) is False


def test_tracked_keys_do_not_grow_without_bound(clock):
    """
    A spray across random usernames must not turn the rate limiter into the
    memory-exhaustion vector it exists to prevent.
    """
    limiter = LoginRateLimiter(
        max_attempts=5, lockout_seconds=60.0, window_seconds=60.0, time_source=clock
    )

    for index in range(2000):
        limiter.record_failure((f"user-{index}", "10.0.0.9"))
        clock.advance(1.0)

    assert len(limiter._failures) < 2000
