"""
Pooled, exception-safe database access (PE-7).

PE-7 asked for connection pooling. **The audit for it found something bigger,
and it changes the order of the work.**

`handover-phase-3a.md` §6 warned of "29 `get_db_connection()` calls in `app.py`
and 28 `conn.close()` - find the mismatch first". There is no mismatch: the
29th "call" is the `def` line, matched by a text grep, and `update_instructor`'s
two closes sit on mutually exclusive branches. Counted with `ast`, it was 27
calls and 27 closes, balanced.

**The real hazard is exception paths: 21 of those 27 call sites were not inside
a `try`/`finally`.** An exception between `get_db_connection()` and
`conn.close()` - a bad query, a `KeyError` on a form field, a template error -
leaked the connection. Today MySQL eventually reaps it and nobody notices.
*Behind a fixed-size pool a leak is permanent*: the connection is never
returned, and after `pool_size` such exceptions every subsequent request blocks
forever waiting for one. Adding a pool without fixing that would have converted
an invisible inefficiency into a hang.

So the deliverable is a context manager first and a pool second. `db_cursor()`
guarantees the close on both paths, which is what makes the pool safe to
introduce at all, and `tests/test_db_access.py` fails if a raw
`get_db_connection()` pattern reappears in `app.py`.

**The pool is created lazily, on first use.** Building it at import time would
open connections to MySQL when anything imported this module - including the
test suite and CI, which have no database. Nothing here touches the network
until a query is actually run.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from contextlib import contextmanager

import mysql.connector
from mysql.connector import pooling

from config.settings import settings

logger = logging.getLogger(__name__)

POOL_NAME = "attendance_pool"

_pool: pooling.MySQLConnectionPool | None = None
_pool_lock = threading.Lock()


def get_pool() -> pooling.MySQLConnectionPool:
    """
    The process-wide connection pool, built on first use.

    Double-checked locking: Flask serves requests on several threads, and two
    of them arriving here at once must not build two pools - each pool opens
    `pool_size` connections, so the loser's would be leaked whole.
    """
    global _pool

    if _pool is not None:
        return _pool

    with _pool_lock:
        if _pool is None:
            _pool = pooling.MySQLConnectionPool(
                pool_name=POOL_NAME,
                pool_size=settings.db_pool_size,
                pool_reset_session=True,
                **settings.db_kwargs(),
            )

            logger.info(
                "Database connection pool created (size %d)",
                settings.db_pool_size,
            )

    return _pool


def reset_pool() -> None:
    """
    Drop the pool so the next call builds a new one.

    For tests, and for the case where configuration changed under a running
    process. Existing checked-out connections are unaffected.
    """
    global _pool

    with _pool_lock:
        _pool = None


@contextmanager
def db_connection():
    """
    A pooled connection, always returned to the pool.

    `close()` on a pooled connection is a return, not a disconnect - which is
    why the `finally` here is the whole point of the module rather than
    housekeeping.
    """
    conn = get_pool().get_connection()

    try:
        # A pooled connection can have been idle long enough for MySQL to have
        # dropped it (`wait_timeout`, eight hours by default). Without this the
        # first query after a quiet night fails with "MySQL Connection not
        # available" and the operator sees an error page on their first action
        # of the day.
        if not conn.is_connected():
            conn.reconnect(attempts=2, delay=0)

        yield conn

    finally:
        conn.close()


@contextmanager
def db_cursor(dictionary: bool = False, commit: bool = False):
    """
    A cursor on a pooled connection. Closes both, whatever happens.

    Pass `commit=True` for anything that writes: the commit then happens on
    clean exit only, and an exception rolls back instead of leaving a partial
    transaction on a connection that is about to be handed to another request.
    That last part is new - the old code committed inline and, on a pooled
    connection, an uncommitted write would otherwise travel to the next user of
    that connection.

        with db_cursor(dictionary=True) as cursor:
            cursor.execute("SELECT * FROM students")
            students = cursor.fetchall()

        with db_cursor(commit=True) as cursor:
            cursor.execute("DELETE FROM subjects WHERE id=%s", (subject_id,))
    """
    with db_connection() as conn:
        cursor = conn.cursor(dictionary=dictionary)

        try:
            yield cursor

            if commit:
                conn.commit()

        except Exception:
            with contextlib.suppress(mysql.connector.Error):
                conn.rollback()
            raise

        finally:
            cursor.close()
