"""
Running behind Cloudflare Tunnel + Caddy (docs/deployment.md).

Behind a reverse proxy every request reaches the app from 127.0.0.1. The login
lockout (SE-13) is keyed on (username, client address), so without PROXY_HOPS
five wrong passwords from *anyone* would lock that account out for *everyone*.

⚠️ These drive the real /login route through **real waitress over a socket**,
with the options serve.py uses. The first version of this file used Flask's
test client and passed - while waitress, in front of the app in production,
was deleting X-Forwarded-For before ProxyFix could read it. Only running
Caddy in front of waitress showed that (tasks/lessons.md L5).
"""

import contextlib
import http.client
import threading
import urllib.parse
from pathlib import Path

import pytest
from waitress.server import create_server

import web.auth
from config.settings import settings as app_config
from security.rate_limit import LoginRateLimiter
from serve import proxy_misconfiguration, server_options
from web import create_app

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def serve_app(monkeypatch):
    """Start the app under waitress with the given PROXY_HOPS; no database."""

    @contextlib.contextmanager
    def no_database(dictionary=False, commit=False):
        yield object()

    # Every login fails: there is no such admin. Only the lockout is under test.
    monkeypatch.setattr(web.auth, "db_cursor", no_database)
    monkeypatch.setattr(
        web.auth.credentials_repo, "admin_by_username", lambda cursor, name: None
    )
    monkeypatch.setattr(web.auth, "login_rate_limiter", LoginRateLimiter(max_attempts=5))

    servers = []

    def start(proxy_hops):
        monkeypatch.setattr(app_config, "proxy_hops", proxy_hops)
        monkeypatch.setattr(app_config, "flask_host", "127.0.0.1")
        monkeypatch.setattr(app_config, "wsgi_port", 0)  # any free port

        app = create_app({"WTF_CSRF_ENABLED": False})
        server = create_server(app, **server_options(app_config))
        threading.Thread(target=server.run, daemon=True).start()
        servers.append(server)

        return server.effective_port

    yield start

    for server in servers:
        server.close()
        server.task_dispatcher.shutdown()


def _login(port, forwarded_for):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.request(
        "POST",
        "/login",
        body=urllib.parse.urlencode(
            {"role": "admin", "user_id": "admin", "password": "wrong"}
        ),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            # What Caddy forwards: Cloudflare's visitor address, then Caddy's
            # own hop for cloudflared on this machine.
            "X-Forwarded-For": f"{forwarded_for}, 127.0.0.1",
            "X-Forwarded-Proto": "https",
        },
    )
    status = connection.getresponse().status
    connection.close()
    return status


def test_behind_the_proxy_one_visitor_cannot_lock_out_another(serve_app):
    port = serve_app(proxy_hops=2)

    for _ in range(5):
        _login(port, "203.0.113.7")

    assert _login(port, "203.0.113.7") == 429
    assert _login(port, "198.51.100.20") != 429


def test_without_proxy_hops_every_visitor_shares_one_lockout(serve_app):
    # The failure PROXY_HOPS exists to prevent, pinned so the test above
    # cannot pass for some unrelated reason.
    port = serve_app(proxy_hops=0)

    for _ in range(5):
        _login(port, "203.0.113.7")

    assert _login(port, "198.51.100.20") == 429


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_trusting_forwarded_headers_on_loopback_is_allowed(host):
    assert proxy_misconfiguration(host, proxy_hops=2) is None


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.51", "::"])
def test_trusting_forwarded_headers_on_the_lan_is_refused(host):
    # Reachable without the proxy, a client could send a new X-Forwarded-For
    # with every guess and never be locked out.
    assert "FLASK_HOST=127.0.0.1" in proxy_misconfiguration(host, proxy_hops=2)


def test_any_host_is_allowed_when_no_header_is_trusted():
    assert proxy_misconfiguration("0.0.0.0", proxy_hops=0) is None


@pytest.mark.parametrize("setup", ["cloudflare", "gcp"])
def test_the_caddyfile_proxies_to_the_port_serve_py_listens_on(setup):
    caddyfile = (PROJECT_ROOT / "deploy" / setup / "Caddyfile").read_text(encoding="utf-8")
    directives = [
        line.strip()
        for line in caddyfile.splitlines()
        if line.strip().startswith("reverse_proxy")
    ]

    assert directives
    assert all("127.0.0.1:8000" in line for line in directives)
    assert type(app_config).model_fields["wsgi_port"].default == 8000
