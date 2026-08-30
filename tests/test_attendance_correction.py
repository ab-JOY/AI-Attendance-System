"""
FS-10, the half that needs no database.

The correction screen's *validation* is decided before any SQL runs, so it
belongs in the fast suite. The transaction - does the status change and its
audit row land together, and does the status change roll back if the audit
insert fails - needs a real server and lives in
tests/integration/test_attendance_correction.py.

**Why the validation is worth its own tests rather than trusting the column
definitions.** This server has no STRICT_TRANS_TABLES. An over-long reason is
not rejected by `VARCHAR(255)`, it is silently truncated, and the request
reports success while storing a mangled record of why a biometric
determination was overridden. That is lessons.md L11 - "never let a constraint
be the guard" - applied to ordinary DML rather than to a migration.
"""

from __future__ import annotations

import ast

import pytest

from tests.conftest import PROJECT_ROOT


@pytest.fixture(scope="session")
def flask_app():
    import app as app_module

    app_module.app.config["TESTING"] = True
    return app_module.app


@pytest.fixture
def client(flask_app):
    flask_app.config["WTF_CSRF_ENABLED"] = False
    yield flask_app.test_client()
    flask_app.config["WTF_CSRF_ENABLED"] = True


def sign_in_as(client, role="instructor"):
    with client.session_transaction() as session:
        session["user"] = f"test-{role}"
        session["role"] = role


# A record id nothing in this project will ever reach. Every rejection below
# happens before the row is looked up, so no database is touched - but the URL
# still must not name a real record (lessons.md L6).
NOWHERE = "/attendance/999999999/correct"


# ===========================================================================
# The status allowlist
# ===========================================================================


@pytest.mark.parametrize(
    "status",
    ["", "present", "PRESENT", "Excused", "Absent; DROP TABLE attendance", "Sick"],
)
def test_a_status_outside_the_allowlist_is_refused(client, status):
    """
    Not free text. /reports and /export_excel aggregate by exact string match,
    so a typed "present" would be a fourth category that every counter ignores
    - FS-7's defect one layer down, where a free-text subject code created
    orphan sessions no report could find.
    """
    sign_in_as(client)

    response = client.post(NOWHERE, data={"status": status, "reason": "a reason"})

    assert response.status_code == 400


def test_the_allowlist_is_exactly_the_statuses_the_system_produces():
    """
    If recognition ever starts writing a fourth status, this fails and the
    screen is updated with it rather than quietly being unable to set it.
    """
    from web.sessions import CORRECTABLE_STATUSES

    assert set(CORRECTABLE_STATUSES) == {"Present", "Late", "Absent"}


# ===========================================================================
# The reason
# ===========================================================================


def test_a_correction_without_a_reason_is_refused(client):
    """
    The audit trail is the feature. A correction with no stated reason is a
    silent change with a username attached, which is most of what migration
    006 exists to prevent.
    """
    sign_in_as(client)

    response = client.post(NOWHERE, data={"status": "Present", "reason": ""})

    assert response.status_code == 400


def test_a_whitespace_only_reason_is_refused(client):
    sign_in_as(client)

    response = client.post(NOWHERE, data={"status": "Present", "reason": "   \t  "})

    assert response.status_code == 400


def test_an_over_long_reason_is_refused_rather_than_truncated(client):
    """
    ⚠️ The one that matters most here.

    `attendance_audit.reason` is VARCHAR(255) and this server is not in strict
    mode, so the column will not refuse a longer value - it will store the
    first 255 characters and report success. The check has to be in Python, or
    the permanent record of why attendance was overridden is silently cut in
    half. lessons.md L11.
    """
    from web.sessions import MAX_CORRECTION_REASON

    sign_in_as(client)

    response = client.post(
        NOWHERE,
        data={"status": "Present", "reason": "x" * (MAX_CORRECTION_REASON + 1)},
    )

    assert response.status_code == 400


def test_a_reason_at_the_limit_is_accepted_by_the_validator(client):
    """
    The boundary from the other side: 255 exactly must get *past* validation.

    ⚠️ Asserted as "not 400" rather than as a status code, on purpose. Past
    the validator this route looks the record up, and this file must not need
    a database - the lint-and-test CI job has none, and a test that passes
    locally because MySQL happens to be running is the shape of bug this
    project keeps producing (L5). What is being claimed here is only that the
    length check let it through, and that is exactly what this asserts.
    """
    from web.sessions import MAX_CORRECTION_REASON

    sign_in_as(client)

    response = client.post(
        NOWHERE,
        data={"status": "Present", "reason": "x" * MAX_CORRECTION_REASON},
    )

    assert response.status_code != 400, (
        "A reason of exactly the column width was rejected by the validator."
    )


def test_the_python_limit_matches_the_column_width():
    """
    The two numbers must not drift. If a migration widens the column, this
    fails until the validator is widened with it.
    """
    from web.sessions import MAX_CORRECTION_REASON

    migration = (PROJECT_ROOT / "migrations" / "006_attendance_audit.sql").read_text(
        encoding="utf-8"
    )

    assert f"reason VARCHAR({MAX_CORRECTION_REASON})" in migration, (
        f"web.sessions.MAX_CORRECTION_REASON is {MAX_CORRECTION_REASON}, which "
        f"no longer "
        "matches attendance_audit.reason in migration 006. With no strict mode "
        "on this server, a mismatch means silent truncation."
    )


# ===========================================================================
# Access
# ===========================================================================


@pytest.mark.parametrize("role", ["instructor", "admin"])
def test_the_access_hook_lets_both_roles_through(client, role):
    """
    FS-10's premise: the instructor is the person who watched the miss happen
    and cannot currently fix it. A refusal here would mean the finding is not
    closed for the role it was written about.

    ⚠️ Asserts on the *denial* codes only, so this needs no database - the
    same reason tests/test_route_security.py gives for asserting denials: they
    are decided by the before_request hook before any route body runs. Whether
    the screen then renders is proved in tests/integration/, against a server.
    """
    sign_in_as(client, role)

    response = client.get(NOWHERE)

    assert response.status_code not in (302, 401, 403), (
        f"A signed-in {role} was refused the correction screen "
        f"({response.status_code})."
    )


# ===========================================================================
# The transaction, asserted at the source level
#
# The integration suite proves the behaviour against a real server. This
# proves the *structure* that produces it, which is what a future edit is
# most likely to break: someone moves the audit insert into its own
# db_cursor() block, both still work, and the atomicity is silently gone.
# ===========================================================================


def test_the_status_update_and_the_audit_insert_share_one_transaction():
    """
    ⚠️ db_cursor() is one transaction per `with` (handover-phase-4 §1.7). Two
    blocks would mean a status change that commits and an audit row that may
    not - exactly the silent override migration 006 exists to prevent.

    **This test moved a layer in Phase 5 and the property did not change.**
    The SQL used to be inline in the route, so the check looked for the two
    statements inside one `with db_cursor(...)`. The statements now live in
    `repositories/attendance.py` and the route calls `set_status()` and
    `record_correction()` - so what has to share a block is the two *calls*.
    Verified by mutation both ways: moving either call into its own
    `db_cursor` block fails this, and so does dropping one entirely.

    Checked with `ast` rather than by reading the source as text, so
    reformatting cannot make it pass or fail spuriously - and, per lessons.md
    L12, so that the comment above the code cannot satisfy the assertion.
    """
    tree = ast.parse(
        (PROJECT_ROOT / "web" / "sessions.py").read_text(encoding="utf-8")
    )

    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "correct_attendance"
    )

    # Every `with db_cursor(...)` block in the route, paired with the names of
    # the repository functions called anywhere inside it.
    blocks = []

    for node in ast.walk(function):
        if not isinstance(node, ast.With):
            continue

        opens_cursor = any(
            isinstance(item.context_expr, ast.Call)
            and getattr(item.context_expr.func, "id", None) == "db_cursor"
            for item in node.items
        )

        if not opens_cursor:
            continue

        called = {
            inner.func.attr
            for inner in ast.walk(node)
            if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)
        }

        blocks.append(called)

    writing = [
        called for called in blocks
        if "set_status" in called or "record_correction" in called
    ]

    assert len(writing) == 1, (
        "The status update and the audit insert are in "
        f"{len(writing)} separate db_cursor() blocks. That is "
        "one transaction each, so a correction can commit without its audit "
        "row. They must share a single block."
    )

    assert "set_status" in writing[0], (
        "The correction route never calls set_status()."
    )
    assert "record_correction" in writing[0], (
        "The correction route changes a status without writing an audit row."
    )


def correction_repository_sql():
    """
    Every SQL string the correction path actually executes.

    ⚠️ Extracted from the `cursor.execute()` calls with `ast`, **not** by
    searching the source text. The first draft of the two tests below did
    search the text, and both were vacuous: the code's own docstrings and
    comments say "FOR UPDATE" and "time_in", so deleting the real SQL left them
    passing. Caught by mutating and finding the test did not notice - which is
    the whole reason lessons.md L4 says to run a check against the problem it
    claims to detect, and lessons.md L12 says a grep will match the prose.

    Phase 5 moved the statements from the route into
    `repositories/attendance.py`; this reads them where they now are. The three
    functions named below are the correction path, and naming them rather than
    scanning the whole module keeps an unrelated statement from satisfying an
    assertion about this one.
    """
    tree = ast.parse(
        (PROJECT_ROOT / "repositories" / "attendance.py").read_text(encoding="utf-8")
    )

    wanted = {"lock_for_correction", "set_status", "record_correction"}

    return [
        arg.value.upper()
        for function in ast.walk(tree)
        if isinstance(function, ast.FunctionDef) and function.name in wanted
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("execute", "executemany")
        for arg in node.args
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
    ]


def test_the_correction_locks_the_row_it_is_about_to_change():
    """
    FOR UPDATE. Without it two operators correcting the same record both read
    the same "before" value, and the trail records two changes from a state
    only one of them actually saw.
    """
    reads = [
        sql for sql in correction_repository_sql()
        if sql.lstrip().startswith("SELECT") and "FROM ATTENDANCE" in sql
    ]

    assert reads, "The correction path does not read the attendance row at all."

    assert any("FOR UPDATE" in sql for sql in reads), (
        "The correction reads the old status without locking the row. Two "
        "concurrent corrections would both record the same 'before' value."
    )


def test_the_correction_does_not_invent_a_time_in():
    """
    Correcting an Absent to Present must not stamp an arrival time. A
    fabricated timestamp in a biometric register is worse than an obviously
    untouched one - the audit row is where the truth lives.
    """
    writes = [sql for sql in correction_repository_sql() if sql.lstrip().startswith("UPDATE")]

    for sql in writes:
        assert "TIME_IN" not in sql, (
            "The correction path writes time_in. Setting an arrival time for "
            f"a student the camera never saw invents evidence: {sql}"
        )
