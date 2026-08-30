"""
Generate a self-signed development certificate, with correct SANs.

    python scripts/make_dev_cert.py            # localhost, 127.0.0.1, this machine's LAN IP
    python scripts/make_dev_cert.py --host 192.168.1.42 --host attendance.local

Then put the two paths in `.env`:

    SSL_CERT_FILE=certs/dev-cert.pem
    SSL_KEY_FILE=certs/dev-key.pem

**Why this exists at all.** Browser enrolment calls `getUserMedia`, which is
only available in a *secure context* - HTTPS, or `localhost`. On a plain-HTTP
page served to another machine the call is rejected outright: no prompt, no
warning, no graceful degradation. So "enrolment in the browser" and "no TLS"
cannot both be true (todo.md §7 Q2), and this is the smallest thing that makes
the first one possible.

⚠️ **A certificate with no Subject Alternative Name is refused, and no
click-through overrides it.** Browsers stopped honouring the common name years
ago. A generated-in-thirty-seconds `openssl req` certificate typically has no
SAN at all, which is why this script writes a config file rather than passing
flags - and why the LAN address has to be an `IP:` entry, not a `DNS:` one.

⚠️ **`mkcert` is better and is what the todo recommends.** It installs a local
CA into the OS and browser trust stores, so there is no interstitial warning at
all. That matters beyond comfort: a browser security warning on a projector
during a defense invites "so is it secure?", and answering that costs more than
the setup would have. Use this when mkcert is not available.

    mkcert -install
    mkcert -cert-file certs/dev-cert.pem -key-file certs/dev-key.pem \\
        localhost 127.0.0.1 192.168.1.42

⚠️ **The key is a credential.** `certs/`, `*.pem`, `*.key` and `*.crt` are
gitignored and must stay that way - committing a private key is the same class
of mistake as the hardcoded SECRET_KEY that Phase 1 removed, and git history is
permanent.
"""

from __future__ import annotations

import argparse
import ipaddress
import logging
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.logging_config import configure_logging  # noqa: E402
from config.settings import BASE_DIR  # noqa: E402

logger = logging.getLogger("make_dev_cert")

CERTS_DIR = BASE_DIR / "certs"

DAYS_VALID = 825  # The longest a browser will accept for a leaf certificate.


def lan_address():
    """
    This machine's address on the local network, or None.

    Connecting a UDP socket sets a route without sending anything, which is
    the portable way to ask "which interface would be used to reach the
    outside world?" - `gethostbyname(gethostname())` returns 127.0.0.1 on many
    Linux configurations and the wrong adapter on Windows machines with a VPN.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    try:
        probe.connect(("10.255.255.255", 1))
        return probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()


def san_entries(hosts):
    """`DNS:`/`IP:` lines for the SAN extension, in openssl's config syntax."""
    dns_index = 0
    ip_index = 0
    lines = []

    for host in hosts:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            dns_index += 1
            lines.append(f"DNS.{dns_index} = {host}")
        else:
            ip_index += 1
            lines.append(f"IP.{ip_index} = {host}")

    return "\n".join(lines)


def openssl_config(hosts):
    """
    A full openssl config, because the SAN cannot be passed as a flag on the
    `openssl req` of every version this might meet.

    `basicConstraints=CA:FALSE` and the key usages are not decoration: some
    browsers reject a leaf certificate that claims to be a CA, and the failure
    reads as an unrelated handshake error.
    """
    return f"""
[req]
distinguished_name = dn
x509_extensions = v3_req
prompt = no

[dn]
CN = {hosts[0]}
O = AI Attendance System (development)

[v3_req]
basicConstraints = CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @alt_names

[alt_names]
{san_entries(hosts)}
"""


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--host",
        action="append",
        default=None,
        help="a hostname or IP the certificate must cover; repeatable",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=CERTS_DIR,
        help="where to write dev-cert.pem and dev-key.pem",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing certificate",
    )

    args = parser.parse_args(argv)

    configure_logging()

    hosts = args.host or []

    if not hosts:
        hosts = ["localhost", "127.0.0.1"]

        address = lan_address()

        if address and address not in hosts:
            hosts.append(address)
            logger.info("Including this machine's LAN address: %s", address)
        else:
            logger.warning(
                "Could not determine a LAN address. The certificate will cover "
                "localhost only - pass --host <ip> to reach this server from "
                "another machine."
            )

    cert_path = args.out / "dev-cert.pem"
    key_path = args.out / "dev-key.pem"

    if (cert_path.exists() or key_path.exists()) and not args.force:
        logger.error(
            "%s already exists. Pass --force to replace it.", cert_path
        )
        return 1

    args.out.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as workspace:
        config_path = Path(workspace) / "openssl.cnf"
        config_path.write_text(openssl_config(hosts), encoding="utf-8")

        command = [
            "openssl", "req", "-x509", "-nodes",
            "-newkey", "rsa:2048",
            "-days", str(DAYS_VALID),
            "-keyout", str(key_path),
            "-out", str(cert_path),
            "-config", str(config_path),
        ]

        logger.info("Covering: %s", ", ".join(hosts))

        try:
            subprocess.run(command, check=True, capture_output=True)
        except FileNotFoundError:
            logger.error(
                "openssl was not found on PATH. It ships with Git for Windows "
                "(Git Bash), or install mkcert instead - see this script's "
                "docstring."
            )
            return 2
        except subprocess.CalledProcessError as error:
            logger.error(
                "openssl failed:\n%s", error.stderr.decode("utf-8", "replace")
            )
            return 2

    # The key is readable by this user only. Not a strong control on Windows,
    # where the ACL is what matters, but it is free and correct on POSIX.
    try:
        key_path.chmod(0o600)
    except OSError:
        logger.warning("Could not restrict permissions on %s", key_path)

    logger.info("Wrote %s", cert_path)
    logger.info("Wrote %s", key_path)
    logger.info("")
    logger.info("Add to .env:")
    logger.info("  SSL_CERT_FILE=%s", cert_path.relative_to(BASE_DIR))
    logger.info("  SSL_KEY_FILE=%s", key_path.relative_to(BASE_DIR))
    logger.info("")
    logger.info(
        "This certificate is self-signed, so the first visit in each browser "
        "shows a warning that has to be accepted once. Accepting it *does* "
        "give a secure context, so the camera works. mkcert avoids the "
        "warning entirely."
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
