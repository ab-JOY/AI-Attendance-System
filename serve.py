"""
The production entry point: the same application, under waitress (PO-4).

`python app.py` runs the Flask development server, which says of itself that
it is not for deployment. This file runs the identical app object, after the
identical start-up checks, under waitress - a pure-Python WSGI server, so it
installs the same way on the Windows classroom machine and on Linux, with
nothing to compile.

**One process, several threads, and that is load-bearing.** The recognition
session, the enrolment slot and the login lockout are all in this process's
memory (see `wsgi_threads` in config/settings.py). Do not put this behind
gunicorn with several workers.

TLS is not handled here. Cloudflare terminates HTTPS and reaches this process
through cloudflared and Caddy on the same machine - see docs/deployment.md.
SSL_CERT_FILE and SSL_KEY_FILE are for `python app.py` only and are ignored here.

    python serve.py
"""

import ipaddress
import logging
import sys

from waitress import serve

from app import app, check_database_on_startup, sweep_abandoned_enrolments
from config.settings import settings as app_config

logger = logging.getLogger(__name__)


def proxy_misconfiguration(host, proxy_hops):
    """
    Why this host/PROXY_HOPS pair is unsafe to serve, or None if it is fine.

    PROXY_HOPS tells the app to believe X-Forwarded-For. That is only true of
    requests the proxy forwarded. Listening on 0.0.0.0 as well lets any
    machine on the LAN connect directly and send whatever address it likes -
    a fresh one per guess, so the login lockout (SE-13) never fires.
    """
    if proxy_hops == 0:
        return None

    if host == "localhost":
        return None

    try:
        if ipaddress.ip_address(host).is_loopback:
            return None
    except ValueError:
        pass

    return (
        f"PROXY_HOPS={proxy_hops} trusts X-Forwarded-For, but FLASK_HOST={host} "
        "also accepts connections that did not come through the proxy, and "
        "those can forge their address. Set FLASK_HOST=127.0.0.1 in .env."
    )


def server_options(config):
    """
    Keyword arguments for waitress, shared with tests/test_deployment.py so
    the test runs the server exactly as this file does.
    """
    return {
        "host": config.flask_host,
        "port": config.wsgi_port,
        "threads": config.wsgi_threads,
        "ident": "attendance",
        # ⚠️ waitress deletes X-Forwarded-For before the app sees it unless it
        # is told otherwise. With the default, ProxyFix had nothing to read
        # and every client was 127.0.0.1 again - found by running Caddy in
        # front of waitress, after the Flask test client had passed. Kept
        # only when PROXY_HOPS says a proxy is there to set the header;
        # otherwise a direct client's forged header is still stripped.
        "clear_untrusted_proxy_headers": config.proxy_hops == 0,
    }


if __name__ == '__main__':
    problem = proxy_misconfiguration(app_config.flask_host, app_config.proxy_hops)

    if problem:
        logger.error("Cannot start: %s", problem)
        sys.exit(1)

    if not check_database_on_startup():
        sys.exit(1)

    sweep_abandoned_enrolments()

    if app_config.proxy_hops == 0:
        # Not fatal: serving browsers directly is a legitimate setup. But behind
        # a proxy this is the misconfiguration that makes the login lockout lock
        # out everybody at once, and nothing else would say so.
        logger.warning(
            "PROXY_HOPS is 0. Behind Cloudflare + Caddy set PROXY_HOPS=2 - "
            "otherwise every client shares one address and the login lockout "
            "applies to all of them together."
        )

    logger.info(
        "Serving on %s:%d with %d threads",
        app_config.flask_host,
        app_config.wsgi_port,
        app_config.wsgi_threads,
    )

    serve(app, **server_options(app_config))
