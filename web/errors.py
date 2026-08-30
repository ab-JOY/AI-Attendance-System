"""
The generic error page, and the handlers that route every failure to it
(SE-10, US-1, RE-8).

Around fifteen routes used to do `return f"Database error: {error}", 500`. That
put the driver's message - table names, column names, MySQL version - on a
blank page with no navigation: a schema disclosure and a dead end at the same
time. The details go to the log, where they are useful; the client gets a code
and a way back.

`error_page()` lives here rather than in a blueprint because every blueprint
needs it, and a helper that half the application imports from `web.students`
would be a worse dependency than the one it replaced.

**One page, two envelopes.** A route marked `@json_api` is polled or posted to
by `fetch()`, and handing it `error.html` means the caller runs `JSON.parse`
over a document and reports a syntax error - the operator is told the page is
broken rather than what went wrong. The marker used to be honoured only for
401 and 403, decided in the access hook, plus a hardcoded `/enrol/` prefix for
413. Everything else - 400, 404, 409, 500 - answered markup. `error_page()`
consults the marker itself now, so the envelope follows what the route
declared and there is one place that decides it.
"""

from __future__ import annotations

import logging

from flask import current_app, jsonify, render_template, request
from flask_wtf.csrf import CSRFError

from security.access import wants_json

logger = logging.getLogger(__name__)

ERROR_MESSAGES = {
    400: "That request could not be processed. Please go back and try again.",
    403: "You do not have permission to view this page.",
    404: "That page could not be found.",
    405: "That action is not available from this page.",
    409: "That change conflicts with existing data.",
    413: "That upload is too large.",
    500: "Something went wrong on the server. The details have been logged.",
}

CSRF_ERROR_MESSAGE = (
    "This form has expired or was not submitted from this site. "
    "Reload the page and try again."
)


def _request_wants_json():
    """
    Whether the route this request matched is marked `@json_api`.

    ⚠️ **`request.endpoint` is None when routing itself failed** - a 404 on a
    URL with no rule, or a request refused before dispatch. `wants_json(None)`
    is False, so those fall through to the HTML page, which is the right answer
    for a browser typing a bad URL.

    A 413 does still arrive here with an endpoint set: Werkzeug refuses the
    body during form parsing, which happens *after* routing has matched. That
    is what lets the enrolment uploader get JSON without this module knowing
    anything about `/enrol/` - it used to be a hardcoded URL-prefix test, which
    covered the one case somebody had hit and left every other JSON route
    answering markup.
    """
    if request.endpoint is None:
        return False

    return wants_json(current_app.view_functions.get(request.endpoint))


def error_page(status_code, message=None):
    """
    Render the generic error page. Never include exception text here.

    Answers JSON instead when the matched route is `@json_api`, so a `fetch()`
    caller gets `{"success": false, "message": ...}` rather than a document to
    `JSON.parse`. The message is the same either way; only the envelope
    changes.
    """
    message = message or ERROR_MESSAGES.get(status_code, ERROR_MESSAGES[500])

    if _request_wants_json():
        return jsonify({"success": False, "message": message}), status_code

    return (
        render_template(
            "error.html",
            status_code=status_code,
            message=message,
        ),
        status_code,
    )


def register_error_handlers(app):
    """Attach every handler to `app`. Called by the factory."""

    @app.errorhandler(CSRFError)
    def handle_csrf_error(error):
        """
        Distinct from the generic 400 on purpose.

        For the operator, "reload the page" is actionable where "that request
        could not be processed" is not - a rejected token is usually just an
        expired session, not an attack. For the test suite, it is the
        difference between proving CSRF was enforced and proving *something*
        returned 400: a POST with a missing form field also produces 400, so a
        test that only checked the status code would pass on code with no CSRF
        protection at all.
        """
        logger.warning(
            "CSRF validation failed for %s: %s", request.path, error.description
        )
        return error_page(400, CSRF_ERROR_MESSAGE)

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(405)
    @app.errorhandler(409)
    @app.errorhandler(413)
    def handle_http_error(error):
        # ⚠️ This used to special-case `/enrol/` paths for 413 and nothing
        # else. That was the one JSON caller anybody had actually watched fail;
        # every other JSON route still answered 400, 404 and 500 with
        # `error.html`. `error_page()` consults the @json_api marker now, so
        # the rule is "whatever the route declared" rather than a URL prefix
        # somebody remembered to add.
        return error_page(error.code)

    @app.errorhandler(500)
    def handle_internal_error(error):
        logger.exception("Unhandled server error", exc_info=error)
        return error_page(500)

    @app.errorhandler(Exception)
    def handle_unexpected_exception(error):
        """
        Catch-all so an exception raised anywhere cannot reach the browser as a
        traceback. Flask re-raises HTTPExceptions to their own handlers, so
        this only sees genuine faults.
        """
        logger.exception("Unhandled exception while serving %s", request.path)
        return error_page(500)
