"""
Route-level regression tests for SE-2, SE-4, SE-5, SE-6, SE-7, SE-10.

**Why these drive real requests.** Every other test in this suite works on
modules in isolation, which is cheap and right for what it covers. Access
control is not one of those things: the Phase 1 shadowing bug passed a
module-level import check, a boot, 27 unit tests and a clean lint while
leaving every database call broken at request time (lessons.md L5). An
authorisation rule that is never exercised by a request is a claim, not a
test.

So this file imports `app` - the one place under tests/ allowed to - and
sends real requests through `app.test_client()`. It costs the 9 s PE-4 model
load once, in a session-scoped fixture, and is marked `slow`.

**No database required.** Everything here asserts a *denial*, and denials are
decided by the before_request hook before any route body runs. That keeps
the suite runnable in CI, where there is no MySQL.

**Every identifier below is deliberately fake** - `SEC-TEST-NOBODY`,
`SEC-TEST-NOONE`. Not tidiness: the first draft used a real student's ID,
and running the suite against the *unprotected* code to prove it caught
SE-5 executed `POST /delete_student/<real id>` for real, against the live
database. The row was restored from `trainer/labels.txt`, but the lesson is
that a test URL naming a real record is one missing guard away from
destroying it. See tasks/lessons.md L6.
"""

import ast
import re

import pytest

from tests.conftest import PROJECT_ROOT

pytestmark = pytest.mark.slow


@pytest.fixture(scope="session")
def flask_app():
    import app as app_module

    app_module.app.config["TESTING"] = True
    return app_module.app


@pytest.fixture
def client(flask_app):
    # WTF_CSRF_ENABLED stays on: CSRF rejection is one of the things under
    # test. Individual tests opt out where they are testing something else.
    return flask_app.test_client()


@pytest.fixture
def csrf_exempt_client(flask_app):
    """A client with CSRF off, for tests about authentication, not CSRF."""
    flask_app.config["WTF_CSRF_ENABLED"] = False
    yield flask_app.test_client()
    flask_app.config["WTF_CSRF_ENABLED"] = True


def sign_in_as(client, role, **extra):
    """Fabricate a session without touching the database."""
    with client.session_transaction() as session:
        session["user"] = f"test-{role}"
        session["role"] = role
        session.update(extra)


# ---------------------------------------------------------------------------
# 1. The audit itself
# ---------------------------------------------------------------------------


def test_every_route_is_explicitly_classified(flask_app):
    """
    SE-4 happened because seven routes were simply forgotten. Decorating the
    other twenty-eight fixes that once; this test is what keeps it fixed,
    because it fails the moment a route is added without a marker.
    """
    from security.access import classify

    unclassified = [
        rule.endpoint
        for rule in flask_app.url_map.iter_rules()
        if rule.endpoint != "static"
        and classify(flask_app.view_functions.get(rule.endpoint)) is None
    ]

    assert not unclassified, (
        "These endpoints carry no access-control marker and would be refused "
        f"at runtime: {sorted(unclassified)}. Add @public, @authenticated or "
        "@role_required in app.py."
    )


# ---------------------------------------------------------------------------
# 2. Unauthenticated access (SE-2, SE-4)
# ---------------------------------------------------------------------------

# GET routes an anonymous request must not be served. `/video_feed` is the
# one that matters most: it was the live camera feed of a classroom, open to
# anyone who could reach the host (SE-2).
PROTECTED_GET_ROUTES = [
    "/dashboard",
    "/students",
    "/manage_students",
    "/edit_student/SEC-TEST-NOBODY",
    "/subjects",
    "/edit_subject/1",
    "/instructors",
    "/manage_instructors",
    "/edit_instructor/SEC-TEST-NOONE",
    "/attendance",
    "/reports",
    "/export_excel",
    "/settings",
    "/change_password",
    "/video_feed",
]

PROTECTED_POST_ROUTES = [
    "/capture_face",
    "/search_student",
    "/delete_student/SEC-TEST-NOBODY",
    "/update_student/SEC-TEST-NOBODY",
    "/recapture_face",
    "/train_model",
    "/add_subject",
    "/update_subject/1",
    "/delete_subject/1",
    "/add_instructor",
    "/search_instructor",
    "/update_instructor/SEC-TEST-NOONE",
    "/delete_instructor/SEC-TEST-NOONE",
    "/start-attendance",
    "/stop_camera",
    "/end-attendance",
    "/change_password",
]


@pytest.mark.parametrize("path", PROTECTED_GET_ROUTES)
def test_anonymous_get_is_refused(client, path):
    response = client.get(path)

    assert response.status_code != 200, f"{path} served an anonymous request"
    assert response.status_code in (302, 401, 403)

    if response.status_code == 302:
        assert response.headers["Location"].endswith("/")


@pytest.mark.parametrize("path", PROTECTED_POST_ROUTES)
def test_anonymous_post_is_refused(csrf_exempt_client, path):
    """
    CSRF is disabled here deliberately. With it on, every one of these would
    return 400 and the test would pass without proving anything about
    authentication - a green result for the wrong reason.
    """
    response = csrf_exempt_client.post(path, data={})

    assert response.status_code != 200, f"{path} served an anonymous request"
    assert response.status_code in (302, 401, 403)


def test_video_feed_does_not_stream_to_anonymous_callers(client):
    """SE-2, stated on its own because of what leaks: a classroom camera."""
    response = client.get("/video_feed")

    assert response.status_code == 302
    assert "multipart/x-mixed-replace" not in response.headers.get("Content-Type", "")


# ---------------------------------------------------------------------------
# 3. Role enforcement (SE-5)
# ---------------------------------------------------------------------------

# The sidebar hides these from instructors; before Phase 2 the routes did not
# check, so a logged-in instructor could call them directly.
ADMIN_ONLY_GET_ROUTES = [
    "/students",
    "/manage_students",
    "/edit_student/SEC-TEST-NOBODY",
    "/subjects",
    "/edit_subject/1",
    "/instructors",
    "/manage_instructors",
    "/edit_instructor/SEC-TEST-NOONE",
    "/settings",
]

ADMIN_ONLY_POST_ROUTES = [
    "/capture_face",
    "/search_student",
    "/delete_student/SEC-TEST-NOBODY",
    "/update_student/SEC-TEST-NOBODY",
    "/recapture_face",
    "/train_model",
    "/add_subject",
    "/update_subject/1",
    "/delete_subject/1",
    "/add_instructor",
    "/search_instructor",
    "/update_instructor/SEC-TEST-NOONE",
    "/delete_instructor/SEC-TEST-NOONE",
    "/update_admin",
]


@pytest.mark.parametrize("path", ADMIN_ONLY_GET_ROUTES)
def test_instructor_cannot_reach_admin_pages(client, path):
    sign_in_as(client, "instructor", instructor_id="SEC-TEST-NOONE")

    assert client.get(path).status_code == 403


@pytest.mark.parametrize("path", ADMIN_ONLY_POST_ROUTES)
def test_instructor_cannot_call_admin_actions(csrf_exempt_client, path):
    sign_in_as(csrf_exempt_client, "instructor", instructor_id="SEC-TEST-NOONE")

    assert csrf_exempt_client.post(path, data={}).status_code == 403


def test_an_unknown_role_is_refused(client):
    """A forged or stale session with a role nobody recognises gets nothing."""
    with client.session_transaction() as session:
        session["user"] = "someone"
        session["role"] = "superuser"

    assert client.get("/students").status_code == 403


# ---------------------------------------------------------------------------
# 4. Destructive routes are POST-only (SE-6)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/delete_student/SEC-TEST-NOBODY",
        "/delete_subject/1",
        "/delete_instructor/SEC-TEST-NOONE",
    ],
)
def test_deletes_reject_get(client, path):
    """
    A destructive action behind GET is triggerable by an <img src>, a link
    prefetch or a crawler. 405 here means the URL simply does not do anything
    on a GET, whoever is signed in.
    """
    sign_in_as(client, "admin")

    assert client.get(path).status_code == 405


# ---------------------------------------------------------------------------
# 5. CSRF (SE-7)
# ---------------------------------------------------------------------------


def assert_rejected_for_csrf(response):
    """
    Assert the request was refused *because of CSRF*, not merely refused.

    This distinction cost a false pass while these tests were being written:
    against the unprotected code, `POST /add_subject` with an empty body
    already returned 400, because `request.form['subject_code']` raises
    BadRequestKeyError. Checking only the status code made a route with no
    CSRF protection at all look protected. The dedicated CSRFError handler
    in app.py exists so this assertion has something specific to check.
    """
    assert response.status_code == 400
    assert b"expired or was not submitted from this site" in response.data, (
        "The request was refused, but not by CSRF validation - so this "
        "proves nothing about SE-7."
    )


@pytest.mark.parametrize(
    "path",
    [
        "/add_subject",
        "/delete_subject/1",
        "/add_instructor",
        "/delete_instructor/SEC-TEST-NOONE",
        "/update_admin",
    ],
)
def test_state_changing_post_without_a_token_is_refused(client, path):
    sign_in_as(client, "admin")

    assert_rejected_for_csrf(client.post(path, data={}))


def test_a_wrong_token_is_refused(client):
    sign_in_as(client, "admin")

    assert_rejected_for_csrf(
        client.post("/add_subject", data={"csrf_token": "not-a-real-token"})
    )


def test_login_itself_requires_a_token(client):
    """
    Otherwise a third-party page can silently sign a visitor into an account
    the attacker controls, and everything they then do is recorded against it.
    """
    assert_rejected_for_csrf(
        client.post(
            "/login",
            data={"role": "admin", "user_id": "admin", "password": "admin"},
        )
    )


# ---------------------------------------------------------------------------
# 6. Forced password change
# ---------------------------------------------------------------------------


def test_a_flagged_account_is_confined_to_the_change_password_page(client):
    from security.access import MUST_CHANGE_PASSWORD

    sign_in_as(client, "admin", **{MUST_CHANGE_PASSWORD: True})

    for path in ("/dashboard", "/students", "/reports", "/settings"):
        response = client.get(path)

        assert response.status_code == 302, f"{path} was served to a flagged account"
        assert response.headers["Location"].endswith("/change_password")


def test_a_flagged_account_can_still_reach_the_change_password_page(client):
    from security.access import MUST_CHANGE_PASSWORD

    sign_in_as(client, "admin", **{MUST_CHANGE_PASSWORD: True})

    assert client.get("/change_password").status_code == 200


def test_a_flagged_account_can_still_log_out(client):
    """Otherwise the only way out of the flag is to clear cookies by hand."""
    from security.access import MUST_CHANGE_PASSWORD

    sign_in_as(client, "admin", **{MUST_CHANGE_PASSWORD: True})

    response = client.get("/logout")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")


# ---------------------------------------------------------------------------
# 7. Session hardening (SE-11)
# ---------------------------------------------------------------------------


def test_session_cookie_is_hardened(flask_app):
    assert flask_app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert flask_app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
    assert flask_app.config["PERMANENT_SESSION_LIFETIME"].total_seconds() > 0


# ---------------------------------------------------------------------------
# 8. Error pages leak nothing (SE-10)
# ---------------------------------------------------------------------------


def test_a_missing_page_renders_the_generic_error_page(client):
    response = client.get("/no-such-page")

    assert response.status_code == 404
    assert b"Traceback" not in response.data


def test_no_route_returns_an_exception_to_the_client():
    """
    Source-level guard, in the spirit of test_no_import_shadowing.py: catch
    the class rather than the instance.

    Around fifteen routes used to `return f"Database error: {error}", 500`,
    putting the driver's message - table names, column names, MySQL version -
    straight into the response. Any new one would be a fresh instance of
    SE-10, and no request-level test would notice until that exact error
    happened to fire.
    """
    source = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="app.py")

    # Names commonly bound to a caught exception in this file.
    exception_names = re.compile(r"\{\s*(error|err|e|exc|exception)\b")
    offenders = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Return) or node.value is None:
            continue

        # `return <expr>, <status>` and `return <expr>` both matter.
        candidates = (
            node.value.elts if isinstance(node.value, ast.Tuple) else [node.value]
        )

        for candidate in candidates:
            if not isinstance(candidate, ast.JoinedStr):
                continue

            rendered = ast.unparse(candidate)

            if exception_names.search(rendered):
                offenders.append(f"line {node.lineno}: {rendered}")

    assert not offenders, (
        "These returns interpolate a caught exception into the response "
        "(SE-10). Log it with logger.exception and return error_page(...) "
        "instead:\n  " + "\n  ".join(offenders)
    )
