"""
The Flask application factory and its eight blueprints (MA-1).

`app.py` was 2,890 lines and 43 routes with SQL, filesystem work and process
management inline. The audit's complaint was not the line count on its own -
it was that a route was the only place any of those things existed, so nothing
below HTTP could be tested or reused. The layering is now:

    web/            HTTP: request parsing, redirects, templates, status codes
    services/       orchestration that is not about HTTP
    repositories/   every SQL statement
    infra/          database pool, camera, jobs, uploads, dataset store
    vision/         recognition, no camera and no database
    security/       access control, passwords, path validation

**URLs did not change; endpoint names did.** A blueprint prefixes its
endpoints, so `url_for('manage_students')` became
`url_for('students.manage_students')`. Every `url_for` in the templates and in
Python was updated with it, and `tests/test_route_security.py` walks
`app.url_map` asserting each endpoint still carries an access marker - so a
route that lost one during the move fails immediately rather than being served.

**`app.py` still exists and still exposes `app`.** `python app.py` is how this
system is deployed (PO-4), and the test suite and the integration fixtures all
import it. It is now a factory call and a `__main__` block.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from flask import Flask
from flask_wtf.csrf import CSRFProtect

from config.settings import settings as app_config
from security.access import install_access_control
from web.errors import register_error_handlers

logger = logging.getLogger(__name__)

csrf = CSRFProtect()


def create_app(config_overrides=None):
    """
    Build the application.

    `config_overrides` exists for tests that need a differently-configured
    instance; nothing in production passes it.
    """
    app = Flask(
        __name__,
        # The factory lives in web/, one level below the templates and static
        # files, which stay at the repository root where every existing path
        # and every deployment instruction expects them.
        template_folder="../templates",
        static_folder="../static",
    )

    # Was hardcoded as "ai_attendance_secret_key" (SE-8). That value is in git
    # history, and a known Flask secret key means session cookies can be
    # forged. Now required from the environment - see .env.example.
    app.secret_key = app_config.secret_key

    # ==============================
    # SESSION HARDENING (SE-11)
    # ==============================
    #
    # HTTPONLY keeps the cookie out of reach of any injected script.
    # SAMESITE=Lax stops a cross-site GET carrying it, which is the second half
    # of the CSRF defence below. The lifetime turns an unattended browser on a
    # shared classroom machine from a permanent session into a 30-minute one.
    #
    # SESSION_COOKIE_SECURE **follows whether TLS is configured**, rather than
    # being a flag somebody has to remember to flip on the day it arrives.
    # Hardcoding True would lock everyone out of an HTTP deployment; leaving it
    # False once there is TLS puts session cookies in clear on the wire. See
    # config/settings.py for the override, which exists for a terminating proxy.
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=app_config.secure_cookies,
        PERMANENT_SESSION_LIFETIME=timedelta(
            minutes=app_config.session_lifetime_minutes
        ),
        # Browser enrolment posts JPEG frames, and this is the first request
        # body the application has ever accepted. Without a cap, an
        # authenticated caller could post something that has to be read into
        # memory before it can be rejected. Werkzeug refuses anything larger
        # with a 413 before the view function runs.
        MAX_CONTENT_LENGTH=app_config.max_upload_bytes,
    )

    if config_overrides:
        app.config.update(config_overrides)

    # ==============================
    # CSRF (SE-7)
    # ==============================
    #
    # Applied to the whole application rather than form by form. None of the
    # ~15 state-changing routes had any CSRF protection, and protecting them
    # individually would mean the next route added is unprotected by default.
    # CSRFProtect rejects any POST without a valid token, so the failure mode
    # for a forgotten token is a visible 400 rather than a silent hole.
    #
    # The token reaches templates as `csrf_token()`; the fetch() calls send it
    # in an X-CSRFToken header.
    csrf.init_app(app)

    register_error_handlers(app)
    _register_blueprints(app)

    # ==============================
    # ACCESS CONTROL (SE-2, SE-4, SE-5)
    # ==============================
    #
    # Default deny. Every view is explicitly marked @public, @authenticated or
    # @role_required, and anything unmarked is refused.
    #
    # ⚠️ Installed *after* the blueprints, and the endpoint names it is given
    # are blueprint-qualified. A stale name here does not fail loudly: the hook
    # would redirect an unauthenticated visitor to an endpoint that does not
    # exist, so `url_for` raises inside the hook and every anonymous request
    # becomes a 500. `tests/test_route_security.py` drives that path.
    install_access_control(
        app,
        login_endpoint="auth.home",
        change_password_endpoint="auth.change_password",
    )

    return app


def _register_blueprints(app):
    """
    One import per blueprint, deliberately inside the function.

    At module scope these would form an import cycle - a blueprint imports
    `web.errors`, which is a sibling of this module - and the cycle would only
    break by accident of import order. This is the same class of latent trap as
    the name shadowing in lessons.md L5, and it is cheaper to avoid than to
    diagnose.
    """
    from web.account import account_bp
    from web.api import api_bp
    from web.auth import auth_bp
    from web.dashboard import dashboard_bp
    from web.enrolment import enrolment_bp
    from web.instructors import instructors_bp
    from web.reports import reports_bp
    from web.sessions import sessions_bp
    from web.students import students_bp
    from web.subjects import subjects_bp

    for blueprint in (
        auth_bp,
        account_bp,
        dashboard_bp,
        students_bp,
        enrolment_bp,
        subjects_bp,
        instructors_bp,
        sessions_bp,
        reports_bp,
        api_bp,
    ):
        app.register_blueprint(blueprint)

    # The API blueprint uses JWT Bearer tokens, not session cookies, so CSRF
    # protection does not apply and would block every request.
    csrf.exempt(api_bp)
