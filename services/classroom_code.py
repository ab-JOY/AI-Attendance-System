"""
The classroom code: proof that a phone verifying attendance is in the room.

Face verification on a phone proves *who* is holding it, not *where*. Before
this, a student could open the app at home during class time and be marked
Present. The code is what only the room has: the instructor's attendance page
shows six digits, changing every PERIOD_SECONDS. The
student types them in the app, and only then may their phone send
frames - `/api/attendance/frame` refuses without a pass, before any face is
judged.

How the code is made: an HMAC of the running session and the current
PERIOD_SECONDS step, under a secret derived from the server's SECRET_KEY. So
it needs no storage, cannot be predicted from earlier codes, and is different
for every session even of the same subject. The current and the previous step
are accepted, so a code typed a second before it changed still works - a
code is good for PERIOD_SECONDS to twice that.

A correct code gives that student a pass for PASS_SECONDS, for that session
only: long enough to verify their face, not long enough to walk out and lend
the phone.

⚠️ **What this does not stop, said plainly:** a student in the room can photograph
the code and send it to a friend at home, who has up to ~12 s to use it. The
friend still has to pass the face check and the liveness challenge *as
themselves, live*, on their own account - so it takes an accomplice in the
room at that moment, not just a shared password. Stronger proofs (the campus
network's address, GPS) are open decisions in tasks/todo.md §18.

Six digits leave a million codes, two of them valid at a time; MAX_WRONG_CODES
wrong tries lock that student out of checking in for the rest of the session,
so guessing has about a 1-in-50,000 chance.

Nothing here imports Flask, the camera or the model. `clock` is injected.
"""

from __future__ import annotations

import hashlib
import hmac
import threading
import time
from collections.abc import Callable
from enum import Enum

# Short, so a photo of the code relayed outside the room is stale by the time
# it is typed. 6 s (was 20, 2026-10-25, the user's choice): with the previous
# step also accepted a code works for 6-12 s - typing the digits takes
# ~5 s. If students typing it are refused, 10 s is the next step up.
PERIOD_SECONDS = 6
CODE_DIGITS = 6
PASS_SECONDS = 180
MAX_WRONG_CODES = 10


def secret_from(secret_key: str) -> bytes:
    """
    The key codes are made with. Derived from the server's SECRET_KEY rather
    than being it, so a code reveals nothing about the cookie key, and every
    code changes whenever that key does.
    """
    return hmac.new(secret_key.encode(), b"classroom-code", hashlib.sha256).digest()


def session_key(subject) -> str:
    """Which attendance session a code or a pass belongs to."""
    return f"{subject.subject_id}:{subject.session_id}"


def code_at(secret: bytes, key: str, step: int) -> str:
    """The code for one session at one PERIOD_SECONDS step."""
    digest = hmac.new(secret, f"{key}|{step}".encode(), hashlib.sha256).digest()
    number = int.from_bytes(digest[:8], "big") % 10**CODE_DIGITS

    return f"{number:0{CODE_DIGITS}d}"


def current_code(secret: bytes, key: str, now: float) -> tuple[str, int]:
    """`(code, seconds until it changes)` - what the instructor's page shows."""
    step = int(now // PERIOD_SECONDS)
    seconds_left = PERIOD_SECONDS - int(now % PERIOD_SECONDS)

    return code_at(secret, key, step), seconds_left


def accepts(secret: bytes, key: str, code: str, now: float) -> bool:
    """True for the current step's code or the previous step's."""
    step = int(now // PERIOD_SECONDS)

    return any(
        hmac.compare_digest(code, code_at(secret, key, candidate))
        for candidate in (step, step - 1)
    )


def parse_code(text: str | None) -> str | None:
    """The six digits of a typed code, or None."""
    if not text:
        return None

    text = text.strip()

    text = text.replace(" ", "")

    if len(text) != CODE_DIGITS or not text.isdigit():
        return None

    return text


class CheckIn(Enum):
    ACCEPTED = "accepted"
    WRONG = "wrong"
    LOCKED = "locked"


class CheckIns:
    """
    The passes granted in the running session, and the wrong tries.

    In memory, on purpose: a pass lasts minutes and belongs to one session. A
    restarted server forgets them, and the student types the code again.
    """

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._key: str | None = None
        self._passes: dict[str, float] = {}
        self._wrong: dict[str, int] = {}

    def _for_session(self, key: str) -> None:
        # A new session starts everyone again. Called under the lock.
        if key != self._key:
            self._key = key
            self._passes.clear()
            self._wrong.clear()

    def check_in(
        self, student_id: str, key: str, code: str | None, secret: bytes
    ) -> tuple[CheckIn, int]:
        """
        Try a code. Returns the outcome and, for WRONG, the tries left.

        A locked student is refused before the code is even compared, so a
        lock cannot be probed for the right answer.
        """
        now = self._clock()

        with self._lock:
            self._for_session(key)

            if self._wrong.get(student_id, 0) >= MAX_WRONG_CODES:
                return CheckIn.LOCKED, 0

            if code is not None and accepts(secret, key, code, now):
                self._passes[student_id] = now + PASS_SECONDS
                return CheckIn.ACCEPTED, MAX_WRONG_CODES - self._wrong.get(student_id, 0)

            wrong = self._wrong.get(student_id, 0) + 1
            self._wrong[student_id] = wrong

            if wrong >= MAX_WRONG_CODES:
                return CheckIn.LOCKED, 0

            return CheckIn.WRONG, MAX_WRONG_CODES - wrong

    def seconds_left(self, student_id: str, key: str) -> int:
        """Seconds left on this student's pass for this session; 0 for none."""
        with self._lock:
            if key != self._key:
                return 0

            expires = self._passes.get(student_id)

        if expires is None:
            return 0

        return max(int(expires - self._clock()), 0)
