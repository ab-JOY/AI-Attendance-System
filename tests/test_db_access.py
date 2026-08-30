"""
Every database access goes through the pooled context manager (PE-7).

**The finding this keeps closed, restated because it is not the one PE-7 was
filed as.** PE-7 asked for connection pooling. The audit for it found that 21
of `app.py`'s 27 `get_db_connection()` call sites were not inside a
`try`/`finally`, so an exception between opening and closing leaked the
connection. Against raw connections that is invisible - MySQL reaps them
eventually. Behind a fixed-size pool it is permanent: the connection is never
returned, and after `pool_size` such exceptions every subsequent request blocks
forever waiting for one. Pooling first would have converted an invisible
inefficiency into a hang.

So the ordering matters, and these tests are what stop it being undone. Two
kinds:

* **Source-level bans**, parsed with `ast`, that fail if the old pattern
  reappears anywhere. Cheap, and they catch the class rather than the instance
  - the same technique `tests/test_no_import_shadowing.py` uses.
* **Behavioural tests of `db_cursor()` itself** against a fake pool, proving it
  returns the connection on the exception path, commits only on success, and
  rolls back otherwise. No MySQL required.
"""

from __future__ import annotations

import ast

import pytest

import infra.db
from infra.db import db_connection, db_cursor
from tests.conftest import project_python_files

# Every first-party module *except* the four that legitimately connect outside
# the pool. Each exclusion is a decision rather than an oversight:
#
#   setup_db.py                  - creates the database itself, so it must
#                                  connect with no database selected.
#   scripts/migrate_passwords.py - a one-shot data migration run outside the
#                                  application.
#   scripts/migrate.py           - the migration CLI (PO-5).
#   infra/migrations.py          - the runner. It has to be able to point at a
#                                  database the pool is not configured for (a
#                                  scratch schema in the integration tests), and
#                                  it must not depend on a pool whose schema
#                                  assumptions it is in the middle of changing.
#
# ⚠️ **This used to be the literal tuple `("app.py", "recognize_face.py")`,
# with a comment admitting the gap: "a new service module that hand-rolls a
# connection would pass. Add it here when you write one."** Phase 5 then wrote
# twenty new modules and moved every SQL statement out of app.py - so the ban
# would have been scanning a file with no database access left in it while
# eight blueprints and seven repositories went unchecked. Derived now, so
# writing a module is enough to be covered by it.
#   infra/db.py                  - *is* the pool and the context manager. It
#                                  closes connections because that is its job.
#   eval_accuracy.py             - builds a throwaway **SQLite** scratch file
#                                  to score predictions against. Nothing to do
#                                  with the MySQL pool.
#
# ⚠️ The last two were found by widening the sweep, not by being remembered.
# That is the point of deriving the list.
UNPOOLED_BY_DESIGN = {
    "setup_db.py",
    "migrate_passwords.py",
    "migrate.py",
    "migrations.py",
    "db.py",
    "eval_accuracy.py",
}


def pooled_modules():
    return [
        path for path in project_python_files()
        if path.name not in UNPOOLED_BY_DESIGN
    ]


POOLED_MODULES = pooled_modules()


def parse(path):
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


# ---------------------------------------------------------------------------
# The old pattern must not come back
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", POOLED_MODULES, ids=lambda path: path.name
)
def test_no_module_opens_its_own_connection(path):
    """
    `mysql.connector.connect(...)` bypasses the pool entirely.

    A single call site doing this is not obviously wrong in review - it works,
    and the tests pass - which is exactly why it needs a machine to catch it.
    """
    offenders = []

    for node in ast.walk(parse(path)):
        if not isinstance(node, ast.Call):
            continue

        func = node.func

        if (
            isinstance(func, ast.Attribute)
            and func.attr == "connect"
            and isinstance(func.value, ast.Attribute)
            and func.value.attr == "connector"
        ):
            offenders.append(node.lineno)

    assert not offenders, (
        f"{path.name} calls mysql.connector.connect() at line(s) "
        f"{offenders}. Use db_cursor() from infra/db.py - a connection opened "
        f"by hand is outside the pool and outside the guaranteed close (PE-7)."
    )


@pytest.mark.parametrize(
    "path", POOLED_MODULES, ids=lambda path: path.name
)
def test_no_module_defines_its_own_connection_helper(path):
    """
    `get_db_connection()` was the shape of the bug, not just its name.

    It handed out a connection and left closing to 27 separate callers. If it
    comes back - even returning a pooled connection - the "who closes this?"
    question comes back with it.
    """
    defined = [
        node.name
        for node in ast.walk(parse(path))
        if isinstance(node, ast.FunctionDef)
        and node.name in ("get_db_connection", "get_connection")
    ]

    assert not defined, (
        f"{path.name} defines {defined}. Connections come from "
        f"infra/db.py's db_cursor()/db_connection(), which close on both "
        f"paths (PE-7)."
    )


@pytest.mark.parametrize(
    "path", POOLED_MODULES, ids=lambda path: path.name
)
def test_no_module_closes_a_connection_by_hand(path):
    """
    A hand-written `conn.close()` means something is being managed by hand,
    and every one of those was a chance to miss an exception path. The context
    manager owns the close now, so any remaining call is a leak waiting to be
    reintroduced.
    """
    offenders = []

    for node in ast.walk(parse(path)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "close"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in ("conn", "connection")
        ):
            offenders.append(node.lineno)

    assert not offenders, (
        f"{path.name} closes a connection by hand at line(s) {offenders}. "
        f"db_cursor() and db_connection() already close on both the success "
        f"and the exception path (PE-7)."
    )


REPOSITORY_MODULES = [
    path for path in project_python_files() if path.parent.name == "repositories"
]


@pytest.mark.parametrize(
    "path", REPOSITORY_MODULES, ids=lambda path: path.name
)
def test_no_repository_opens_its_own_cursor(path):
    """
    A repository takes a cursor. It never opens one.

    ⚠️ **This is the rule the whole `repositories/` package rests on, and it is
    about transactions rather than tidiness.** `db_cursor()` is one transaction
    per `with` block. A repository that opened its own would put each statement
    in its own transaction, so a caller needing two writes to commit together -
    the absent register (FS-4), the correction and its audit row (FS-10) -
    could not express that at all. Both properties would disappear with no
    visible change at any call site, which is the silent-regression shape this
    project keeps producing.

    The caller decides the boundary because the caller is the only thing that
    knows what "together" means.
    """
    offenders = [
        f"line {node.lineno}"
        for node in ast.walk(parse(path))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in ("db_cursor", "db_connection")
    ]

    assert not offenders, (
        f"repositories/{path.name} opens its own cursor at {offenders}. A "
        "repository takes one, so the caller controls the transaction "
        "boundary - see the note in repositories/__init__.py."
    )


def test_the_repository_package_is_actually_being_scanned():
    """
    Guards the sweep above against measuring nothing.

    If `repositories/` were renamed or the discovery in conftest stopped
    covering it, the parametrised ban would silently have no cases and pass.
    """
    assert len(REPOSITORY_MODULES) >= 5, (
        f"Only {len(REPOSITORY_MODULES)} repository modules found. The ban "
        "above is probably scanning nothing."
    )


def test_every_db_cursor_use_is_a_with_statement():
    """
    `db_cursor()` called without `with` returns a context manager that never
    enters, so the query never runs and nothing is closed. It would fail
    loudly, but only at the call site that did it - this fails at build time.
    """
    offenders = []

    for path in POOLED_MODULES:
        tree = parse(path)

        managed = {
            item.context_expr
            for node in ast.walk(tree)
            if isinstance(node, ast.With)
            for item in node.items
        }

        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in ("db_cursor", "db_connection")
                and node not in managed
            ):
                offenders.append(f"{path.name}:{node.lineno}")

    assert not offenders, (
        f"db_cursor()/db_connection() used outside a `with` at {offenders}."
    )


# ---------------------------------------------------------------------------
# db_cursor() itself
# ---------------------------------------------------------------------------


class FakeCursor:
    def __init__(self, dictionary=False):
        self.dictionary = dictionary
        self.closed = False
        self.statements = []

    def execute(self, statement, params=None):
        self.statements.append((statement, params))

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self):
        self.cursors = []
        self.commits = 0
        self.rollbacks = 0
        self.returned = False
        self.connected = True
        self.reconnects = 0

    def cursor(self, dictionary=False):
        cursor = FakeCursor(dictionary=dictionary)
        self.cursors.append(cursor)
        return cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def is_connected(self):
        return self.connected

    def reconnect(self, attempts=1, delay=0):
        self.reconnects += 1
        self.connected = True

    def close(self):
        # On a pooled connection this is a return to the pool, not a
        # disconnect. That is the whole reason the `finally` matters.
        self.returned = True


class FakePool:
    def __init__(self):
        self.handed_out = []

    def get_connection(self):
        conn = FakeConnection()
        self.handed_out.append(conn)
        return conn


@pytest.fixture
def pool(monkeypatch):
    fake = FakePool()
    monkeypatch.setattr(infra.db, "get_pool", lambda: fake)
    return fake


def test_the_connection_is_returned_on_the_happy_path(pool):
    with db_cursor() as cursor:
        cursor.execute("SELECT 1")

    assert pool.handed_out[0].returned
    assert pool.handed_out[0].cursors[0].closed


def test_the_connection_is_returned_when_the_body_raises(pool):
    """
    The 21 unprotected call sites, in one assertion.

    This is the difference between a failed request and a pool permanently one
    connection smaller.
    """
    with pytest.raises(ValueError), db_cursor() as cursor:
        cursor.execute("SELECT 1")
        raise ValueError("something went wrong mid-request")

    assert pool.handed_out[0].returned, "an exception leaked the connection"
    assert pool.handed_out[0].cursors[0].closed


def test_the_connection_is_returned_when_the_caller_returns_early(pool):
    """Several routes `return` from inside the block - a 404, a redirect."""

    def route():
        with db_cursor() as cursor:
            cursor.execute("SELECT 1")
            return "early"

    assert route() == "early"
    assert pool.handed_out[0].returned


def test_nothing_is_committed_without_asking(pool):
    with db_cursor() as cursor:
        cursor.execute("SELECT 1")

    assert pool.handed_out[0].commits == 0


def test_commit_happens_on_a_clean_exit(pool):
    with db_cursor(commit=True) as cursor:
        cursor.execute("DELETE FROM subjects WHERE id=%s", (1,))

    assert pool.handed_out[0].commits == 1
    assert pool.handed_out[0].rollbacks == 0


def test_an_exception_rolls_back_instead_of_committing(pool):
    """
    On a pooled connection this is not housekeeping. An uncommitted write left
    on the connection travels to whichever request is handed it next.
    """
    with pytest.raises(RuntimeError), db_cursor(commit=True) as cursor:
        cursor.execute("DELETE FROM students WHERE student_id=%s", ("SEC-TEST-NOBODY",))
        raise RuntimeError("failed after the write")

    conn = pool.handed_out[0]

    assert conn.commits == 0, "a failed request was committed"
    assert conn.rollbacks == 1
    assert conn.returned


def test_an_early_return_still_commits(pool):
    """
    `change_password` and `delete_student` both return from inside the block
    on the success path. A commit that only happened at the end of the block
    would silently discard their write.
    """

    def route():
        with db_cursor(commit=True) as cursor:
            cursor.execute("UPDATE admin SET username=%s WHERE id=1", ("x",))
            return "done"

    assert route() == "done"
    assert pool.handed_out[0].commits == 1


def test_dictionary_is_passed_through(pool):
    with db_cursor(dictionary=True) as cursor:
        cursor.execute("SELECT 1")

    assert pool.handed_out[0].cursors[0].dictionary is True


def test_a_dropped_connection_is_reconnected_before_use(pool, monkeypatch):
    """
    A pooled connection idle past MySQL's `wait_timeout` is dead but still in
    the pool. Without this the first action of the morning is an error page.
    """
    original = pool.get_connection

    def stale_connection():
        conn = original()
        conn.connected = False
        return conn

    monkeypatch.setattr(pool, "get_connection", stale_connection)

    with db_connection() as conn:
        assert conn.reconnects == 1
        assert conn.is_connected()

    assert pool.handed_out[0].returned
