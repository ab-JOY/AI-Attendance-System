"""
Failed-login throttling (SE-13).

`/login` had no rate limit and no lockout, so credential brute force was
bounded only by how fast an attacker could send requests. Combined with SE-15
(case-insensitive comparison collapsing the alphabet) and SE-1 (plaintext
storage), guessing the seeded `admin`/`admin` took no effort at all.

**Deliberately in-memory, with no new dependency.** The honest scope of this
system is a single Flask development-server process on one machine
(PO-4, CO-3), so a process-local counter is an accurate model of it and adds
nothing to install or operate. The two limitations that follow are real and
should be stated rather than discovered:

- Counters reset when the process restarts. An attacker who can restart the
  server has already won, but an operator restarting to clear a lockout is a
  supported - if crude - recovery path.
- It does not survive being run behind multiple workers. Moving to gunicorn
  with more than one worker (PO-4, Phase 5) means moving this to shared
  storage, and the class boundary here is what makes that a swap rather than
  a rewrite.

Attempts are keyed on `(identifier, client address)`, so one attacker cannot
lock a legitimate user out of their own account from a different address, and
one address cannot spray many accounts for free.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

# Beyond this many tracked keys, expired entries are swept. Without a sweep a
# spray across random usernames would grow the dict without bound - the rate
# limiter itself becoming the denial of service.
_SWEEP_THRESHOLD = 1024


class LoginRateLimiter:
    """
    Counts consecutive failures per key and locks the key once the limit is hit.

    `time_source` exists so the tests can advance the clock instead of
    sleeping; production uses `time.monotonic`, which is immune to the system
    clock being adjusted.
    """

    def __init__(
        self,
        max_attempts: int = 5,
        lockout_seconds: float = 900.0,
        window_seconds: float = 900.0,
        time_source: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")

        self.max_attempts = max_attempts
        self.lockout_seconds = lockout_seconds
        self.window_seconds = window_seconds
        self._now = time_source

        # key -> (consecutive failures, timestamp of the most recent failure)
        self._failures: dict[tuple[str, str], tuple[int, float]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def key(identifier: str | None, remote_address: str | None) -> tuple[str, str]:
        """
        Build the tracking key.

        The identifier is lower-cased on purpose, and only here. Usernames are
        matched case-sensitively at authentication (SE-15), so `Admin` and
        `admin` are different accounts to the login route - but they are the
        same guess to an attacker, and counting them separately would hand out
        a free multiplier on the attempt budget.
        """
        return ((identifier or "").strip().lower(), (remote_address or "unknown"))

    def seconds_until_unlocked(self, key: tuple[str, str]) -> float:
        """
        Remaining lockout in seconds, or 0.0 if the key may attempt a login.
        """
        with self._lock:
            return self._seconds_until_unlocked_locked(key)

    def is_locked(self, key: tuple[str, str]) -> bool:
        return self.seconds_until_unlocked(key) > 0.0

    def record_failure(self, key: tuple[str, str]) -> float:
        """
        Record a failed attempt. Returns the remaining lockout in seconds,
        which is 0.0 while attempts are still available.
        """
        with self._lock:
            now = self._now()
            count, last_seen = self._failures.get(key, (0, now))

            # A quiet spell longer than the window forgives earlier failures,
            # so an occasional typo never accumulates into a lockout.
            if now - last_seen > self.window_seconds:
                count = 0

            self._failures[key] = (count + 1, now)

            if len(self._failures) > _SWEEP_THRESHOLD:
                self._sweep_locked(now)

            return self._seconds_until_unlocked_locked(key)

    def record_success(self, key: tuple[str, str]) -> None:
        """Clear the counter after a successful authentication."""
        with self._lock:
            self._failures.pop(key, None)

    def reset(self) -> None:
        """Forget every tracked key. For tests and for operator recovery."""
        with self._lock:
            self._failures.clear()

    # -- internals; callers already hold the lock -------------------------

    def _seconds_until_unlocked_locked(self, key: tuple[str, str]) -> float:
        entry = self._failures.get(key)

        if entry is None:
            return 0.0

        count, last_seen = entry

        if count < self.max_attempts:
            return 0.0

        remaining = self.lockout_seconds - (self._now() - last_seen)

        if remaining <= 0.0:
            # The lockout has expired. Drop the entry so the next attempt
            # starts from a clean budget rather than re-locking on one more
            # mistake.
            del self._failures[key]
            return 0.0

        return remaining

    def _sweep_locked(self, now: float) -> None:
        horizon = max(self.window_seconds, self.lockout_seconds)

        self._failures = {
            key: entry
            for key, entry in self._failures.items()
            if now - entry[1] <= horizon
        }
