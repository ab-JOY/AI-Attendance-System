"""
Route access control (SE-2, SE-4, SE-5).

The audit found seven routes with no authentication at all - including
`/video_feed`, the live camera stream of a classroom - and no role check
anywhere. Every protected route tested only `if 'user' not in session`, so a
logged-in *instructor* could call `/delete_student` and `/update_admin`. The
sidebar hid the links; the routes did not enforce.

**Why this is a `before_request` hook and not 35 decorators.** todo.md asks
for the full route table to be audited rather than spot-fixed. Decorating
every route by hand satisfies that on the day it is done and fails silently
the first time someone adds a route and forgets. So:

- The hook denies *everything* that is not explicitly classified. A new route
  with no marker is refused with a 403 and a loud log line, not quietly
  served.
- `@public`, `@authenticated` and `@role_required(...)` are the three
  classifications, and they are declarations rather than wrappers - they set
  an attribute and return the same function, so decorator order relative to
  `@app.route` does not matter and there is no per-request wrapper cost.
- `tests/test_route_security.py` walks `app.url_map` and asserts every
  endpoint carries a marker. That test, not the markers, is what keeps the
  audit true - it fails the moment route 36 arrives unclassified.

The hook also enforces `must_change_password`, so the forced change of the
seeded `admin`/`admin` credential cannot be side-stepped by typing a URL.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable

from flask import Flask, abort, jsonify, redirect, request, session, url_for

logger = logging.getLogger(__name__)

# Attribute names are private to this module; use the decorators below.
_ACCESS_ATTR = "_security_access"
_ROLES_ATTR = "_security_roles"
_JSON_ATTR = "_security_json_api"

PUBLIC = "public"
AUTHENTICATED = "authenticated"
ROLE_RESTRICTED = "role"

# Session key set at login for an account still using a shipped default
# credential.
MUST_CHANGE_PASSWORD = "must_change_password"


def public(view: Callable) -> Callable:
    """Reachable without a session: the login page, the login POST, logout."""
    setattr(view, _ACCESS_ATTR, PUBLIC)
    return view


def authenticated(view: Callable) -> Callable:
    """Any logged-in user, admin or instructor."""
    setattr(view, _ACCESS_ATTR, AUTHENTICATED)
    return view


def role_required(*roles: str) -> Callable[[Callable], Callable]:
    """Logged in *and* holding one of `roles`."""
    if not roles:
        raise ValueError("role_required needs at least one role")

    def decorator(view: Callable) -> Callable:
        setattr(view, _ACCESS_ATTR, ROLE_RESTRICTED)
        setattr(view, _ROLES_ATTR, frozenset(roles))
        return view

    return decorator


def json_api(view: Callable) -> Callable:
    """
    Marks a route whose caller is `fetch()`, not a browser navigation.

    A redirect to the login page is useless to XHR - it either follows it and
    parses HTML as JSON, or reports a confusing success. These endpoints get
    401/403 with a JSON body instead.
    """
    setattr(view, _JSON_ATTR, True)
    return view


def classify(view: Callable | None) -> str | None:
    """The declared classification of a view, or None if it carries no marker."""
    if view is None:
        return None
    return getattr(view, _ACCESS_ATTR, None)


def required_roles(view: Callable | None) -> frozenset[str]:
    if view is None:
        return frozenset()
    return getattr(view, _ROLES_ATTR, frozenset())


def install_access_control(
    app: Flask,
    *,
    login_endpoint: str = "home",
    change_password_endpoint: str = "change_password",
    extra_public_endpoints: Iterable[str] = ("static",),
) -> None:
    """
    Install the default-deny hook on `app`.

    `extra_public_endpoints` exists for endpoints Flask registers itself -
    `static` above all - which cannot be decorated at their definition.
    """
    always_public = frozenset(extra_public_endpoints)

    def _deny_unauthenticated(is_json: bool):
        if is_json:
            return jsonify({"success": False, "message": "Authentication required"}), 401
        return redirect(url_for(login_endpoint))

    def _deny_forbidden(is_json: bool):
        if is_json:
            return jsonify({"success": False, "message": "Forbidden"}), 403
        # Rendered by the 403 error handler in app.py, which shows a generic
        # page rather than leaking why the request was refused.
        abort(403)

    @app.before_request
    def _enforce_access_control():
        endpoint = request.endpoint

        # No endpoint means no matching rule; let Flask produce its 404 and
        # let the error handler render it.
        if endpoint is None:
            return None

        if endpoint in always_public:
            return None

        view = app.view_functions.get(endpoint)
        access = classify(view)
        is_json = bool(view is not None and getattr(view, _JSON_ATTR, False))

        if access is None:
            # Fail closed. An unclassified route is a mistake, and serving it
            # is how SE-4 happened in the first place.
            logger.error(
                "Refusing request to unclassified endpoint %r. Mark the view "
                "with @public, @authenticated or @role_required.",
                endpoint,
            )
            return _deny_forbidden(is_json)

        if access == PUBLIC:
            return None

        if "user" not in session:
            return _deny_unauthenticated(is_json)

        if access == ROLE_RESTRICTED and session.get("role") not in required_roles(view):
            logger.warning(
                "Role check failed: user %r with role %r requested %r",
                session.get("user"),
                session.get("role"),
                endpoint,
            )
            return _deny_forbidden(is_json)

        # An account still on a shipped default credential may do exactly one
        # thing: change it. Enforced here rather than at the login redirect so
        # it cannot be bypassed by navigating straight to another URL.
        if session.get(MUST_CHANGE_PASSWORD) and endpoint != change_password_endpoint:
            if is_json:
                return (
                    jsonify(
                        {
                            "success": False,
                            "message": "Change your password before continuing.",
                        }
                    ),
                    403,
                )
            return redirect(url_for(change_password_endpoint))

        return None
