"""
TLS configuration, and the SE-11 cookie flag that follows it.

**The finding underneath this is CO-3, not "add HTTPS".** Browser enrolment
calls `getUserMedia`, which browsers expose only in a *secure context* - HTTPS,
or `localhost`. On a plain-HTTP page served to another machine the call is
rejected outright, with no prompt and nothing to click through. So enrolment
from a second machine is impossible until a certificate is configured, and
"enrolment in the browser" and "no TLS" cannot both be true (todo.md §7 Q2).

Two properties are worth protecting, and both are about *silence*:

1. **A half-configured certificate must not start.** A certificate with no key
   would leave the server on HTTP while the operator believes it is on HTTPS -
   and the symptom is a camera that does not work, which sends them looking in
   entirely the wrong place.
2. **The Secure cookie flag must not be a thing somebody remembers.** SE-11
   left `session_cookie_secure=False` because the deployment was HTTP; the flip
   was documented as a to-do. A forgotten flip means session cookies in clear
   on the wire in the one deployment where it matters.

Verified live before these were written: with a certificate configured the
server answers `https://` and sets `Secure; HttpOnly; SameSite=Lax`; without
one it answers `http://` and omits `Secure`, and logs why enrolment will only
work locally.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from config.settings import Settings

# Long enough to satisfy the SECRET_KEY minimum. Not a real key: this file
# never starts a server.
FAKE_SECRET = "x" * 32


def build(**overrides):
    """A Settings instance with the required fields filled in."""
    return Settings(secret_key=FAKE_SECRET, **overrides)


@pytest.fixture
def certificate(tmp_path):
    cert = tmp_path / "dev-cert.pem"
    key = tmp_path / "dev-key.pem"
    cert.write_text("not a real certificate", encoding="utf-8")
    key.write_text("not a real key", encoding="utf-8")
    return cert, key


# ---------------------------------------------------------------------------
# The pair
# ---------------------------------------------------------------------------


def test_no_certificate_is_a_valid_configuration(monkeypatch):
    """HTTP on localhost is how this system is deployed today (PO-4)."""
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_KEY_FILE", raising=False)

    settings = build(ssl_cert_file=None, ssl_key_file=None)

    assert settings.tls_enabled is False
    assert settings.ssl_context is None


def test_both_halves_configure_tls(certificate):
    cert, key = certificate

    settings = build(ssl_cert_file=cert, ssl_key_file=key)

    assert settings.tls_enabled is True
    assert settings.ssl_context == (cert, key)


def test_a_certificate_without_a_key_refuses_to_start(certificate):
    """
    ⚠️ A startup failure, not a fall back to HTTP.

    Falling back would leave the operator on plain HTTP believing they were on
    HTTPS - and the symptom they would chase is a camera that does not work.
    """
    cert, _key = certificate

    with pytest.raises(ValidationError, match="SSL_KEY_FILE"):
        build(ssl_cert_file=cert, ssl_key_file=None)


def test_a_key_without_a_certificate_refuses_to_start(certificate):
    _cert, key = certificate

    with pytest.raises(ValidationError, match="SSL_CERT_FILE"):
        build(ssl_cert_file=None, ssl_key_file=key)


def test_a_path_that_does_not_exist_refuses_to_start(tmp_path, certificate):
    """
    A mistyped path is caught at start-up with the path in the message.

    Werkzeug would otherwise fail somewhere inside the socket setup, and the
    error names an SSL routine rather than the setting that is wrong.
    """
    cert, key = certificate

    with pytest.raises(ValidationError, match="SSL_KEY_FILE does not exist"):
        build(ssl_cert_file=cert, ssl_key_file=tmp_path / "missing.pem")


# ---------------------------------------------------------------------------
# SE-11 - the Secure flag follows TLS
# ---------------------------------------------------------------------------


def test_secure_cookies_are_off_without_tls():
    """
    Setting Secure on an HTTP origin means the browser never sends the cookie
    back, so nobody can log in at all. That is why SE-11 left it False.
    """
    settings = build(ssl_cert_file=None, ssl_key_file=None)

    assert settings.secure_cookies is False


def test_secure_cookies_turn_on_with_tls(certificate):
    """
    ⚠️ **The flip is derived, not remembered.**

    SE-11 recorded `SESSION_COOKIE_SECURE=false` with a note to turn it on the
    day TLS arrived. A note is a thing somebody has to act on; this is not.
    """
    cert, key = certificate

    settings = build(ssl_cert_file=cert, ssl_key_file=key)

    assert settings.secure_cookies is True


def test_an_explicit_setting_still_wins(certificate):
    """
    Behind a TLS-terminating proxy this process speaks HTTP while the browser
    does not, so the derived answer is wrong and has to be overridable.
    """
    cert, key = certificate

    assert build(
        ssl_cert_file=cert, ssl_key_file=key, session_cookie_secure=False
    ).secure_cookies is False

    assert build(
        ssl_cert_file=None, ssl_key_file=None, session_cookie_secure=True
    ).secure_cookies is True


# ---------------------------------------------------------------------------
# The certificate generator
# ---------------------------------------------------------------------------


def test_the_generated_config_carries_subject_alternative_names():
    """
    ⚠️ **A certificate with no SAN is refused outright and no click-through
    overrides it.** Browsers stopped honouring the common name years ago, and
    a plain `openssl req` produces no SAN at all - which is why the script
    writes a config file rather than passing flags.

    Hostnames must be `DNS:` entries and addresses `IP:` entries; an address
    listed as a DNS name matches nothing.
    """
    from scripts.make_dev_cert import openssl_config

    config = openssl_config(["localhost", "127.0.0.1", "192.168.1.42"])

    assert "subjectAltName = @alt_names" in config
    assert "DNS.1 = localhost" in config
    assert "IP.1 = 127.0.0.1" in config
    assert "IP.2 = 192.168.1.42" in config


def test_a_leaf_certificate_does_not_claim_to_be_a_certificate_authority():
    """
    Some browsers reject a leaf certificate with `CA:TRUE`, and the failure
    reads as an unrelated handshake error rather than as a certificate problem.
    """
    from scripts.make_dev_cert import openssl_config

    config = openssl_config(["localhost"])

    assert "basicConstraints = CA:FALSE" in config
    assert "extendedKeyUsage = serverAuth" in config
