"""
Running training off the request thread (PE-5, US-2).

PE-5: "`train_model()` runs synchronously inside the HTTP request after every
capture. The request blocks for minutes with no progress feedback and will hit
any proxy/browser timeout."

US-2: "no loading indicators for the two multi-minute operations."

`BackgroundJob` is the smallest thing that fixes both: one run at a time, on a
worker thread, with a status record a polling endpoint can read.

**On "progress".** Phase 0 took training from multi-minute to roughly twenty
seconds at three students, so a percentage bar would be both hard to derive and
nearly pointless. What the UI gets instead is the *stage* the run is in and how
long it has been going - both true, both useful, neither invented. A percentage
computed from a guess is worse than no percentage; see lessons.md L7.

**Deliberately not a task queue.** No Celery, no Redis, no broker. This
deployment is one Flask development-server process on one machine (PO-4), a
thread is the honest fit, and the status is in memory for the same reason
`security/rate_limit.py` is: a restart clearing it is an accurate model of a
single-process deployment, not a shortcut. That is a stated limitation rather
than a hidden one - if this ever runs under more than one worker, this class is
wrong and a real queue is needed.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

logger = logging.getLogger(__name__)

IDLE = "idle"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"


@dataclass(frozen=True)
class JobStatus:
    """
    A snapshot of the job, safe to hand to a JSON endpoint.

    Frozen and copied out under the lock, so a poll cannot observe a half
    updated record - and so the caller cannot mutate the job by editing what it
    was given.
    """

    state: str = IDLE
    stage: str | None = None
    message: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    started_by: str | None = None

    @property
    def is_running(self) -> bool:
        return self.state == RUNNING

    def elapsed_seconds(self, now: float | None = None) -> float | None:
        if self.started_at is None:
            return None

        end = self.finished_at

        if end is None:
            end = time.time() if now is None else now

        return round(end - self.started_at, 1)

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "stage": self.stage,
            "message": self.message,
            "running": self.is_running,
            "elapsed_seconds": self.elapsed_seconds(),
            "started_by": self.started_by,
        }


@dataclass
class BackgroundJob:
    """
    One long-running task at a time, off the request thread.

    `runner` is called with a `report` callable it may use to name the stage it
    has reached, and must return `(ok, message)`.
    """

    name: str
    runner: Callable[..., tuple[bool, str]]
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _status: JobStatus = field(default_factory=JobStatus, repr=False)
    _thread: threading.Thread | None = field(default=None, repr=False)

    # -- reading -----------------------------------------------------------

    def status(self) -> JobStatus:
        with self._lock:
            return self._status

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._status.is_running

    # -- starting ----------------------------------------------------------

    def start(self, started_by: str | None = None) -> bool:
        """
        Begin a run. False if one is already going.

        Refusing rather than queueing is deliberate: two concurrent trainings
        would both write `trainer/trainer.yml`, and although
        `write_model_atomically()` makes each write safe on its own, the loser's
        model would silently replace the winner's. One at a time is the
        correct semantics, not a simplification.
        """
        with self._lock:
            if self._status.is_running:
                logger.info(
                    "%s already running, refusing a second run", self.name
                )
                return False

            self._status = JobStatus(
                state=RUNNING,
                stage="Starting",
                message=None,
                started_at=time.time(),
                finished_at=None,
                started_by=started_by,
            )

            self._thread = threading.Thread(
                target=self._run,
                name=f"job-{self.name}",
                daemon=True,
            )
            self._thread.start()

            logger.info("%s started by %r", self.name, started_by)
            return True

    # -- the worker --------------------------------------------------------

    def _report(self, stage: str) -> None:
        with self._lock:
            if self._status.is_running:
                self._status = replace(self._status, stage=stage)

        logger.info("%s: %s", self.name, stage)

    def _run(self) -> None:
        try:
            ok, message = self.runner(report=self._report)

        except Exception as error:
            # A worker thread that dies with an unhandled exception leaves the
            # UI polling a job that says "running" forever. The failure has to
            # land in the status, not only in the log.
            logger.exception("%s raised", self.name)
            self._finish(
                False,
                f"Training failed unexpectedly: {type(error).__name__}",
            )
            return

        self._finish(ok, message)

    def _finish(self, ok: bool, message: str) -> None:
        with self._lock:
            self._status = replace(
                self._status,
                state=SUCCEEDED if ok else FAILED,
                stage=None,
                message=message,
                finished_at=time.time(),
            )

        logger.info(
            "%s finished (%s) in %.1fs: %s",
            self.name,
            "ok" if ok else "failed",
            self._status.elapsed_seconds() or 0.0,
            message,
        )

    # -- tests -------------------------------------------------------------

    def wait(self, timeout: float = 30.0) -> bool:
        """Block until the current run finishes. For tests, not for routes."""
        thread = self._thread

        if thread is None:
            return True

        thread.join(timeout=timeout)
        return not thread.is_alive()

    def reset(self) -> None:
        """Forget the last result. For tests."""
        with self._lock:
            if self._status.is_running:
                raise RuntimeError("cannot reset a running job")

            self._status = JobStatus()
            self._thread = None
