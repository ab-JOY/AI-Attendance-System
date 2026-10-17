#!/usr/bin/env bash
#
# Set up the attendance system on a fresh Google Compute Engine VM running
# Debian 12, behind Caddy. Run as root, from the unpacked source tree:
#
#   sudo bash /opt/attendance/deploy/gcp/bootstrap.sh attendance.example.edu
#
# docs/deployment-gcp.md is the full procedure; this script is its step 4.
#
# Safe to re-run. An existing .env, database user and admin password are kept;
# the venv, the systemd unit and the Caddyfile are refreshed.
#
# ⚠️ It does not copy dataset/ or trainer/. Moving students' face images onto
# a cloud provider is a data-protection decision (RA 10173), not a setup step.
# See docs/deployment-gcp.md section 6.

set -euo pipefail

DOMAIN="${1:-}"
APP_DIR=/opt/attendance
APP_USER=attendance
DB_NAME=attendancesystem_db
DB_USER=attendance

step() { printf '\n=== %s\n' "$1"; }
fail() { printf '\nFAILED: %s\n' "$1" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || fail "run as root (sudo)"
[ -n "$DOMAIN" ] || fail "usage: sudo bash deploy/gcp/bootstrap.sh <domain>"
[ -f "$APP_DIR/serve.py" ] || fail "the source tree is not at $APP_DIR"

# ---------------------------------------------------------------------------
step "1. System packages"
#
# Python 3.11 is not a preference: mediapipe 0.10.14 has no wheels for 3.12+
# (pyproject.toml). Debian 12 ships 3.11, which is why the VM is Debian 12 and
# not Ubuntu 24.04 (3.12). libgl1 and libglib2.0-0 are what opencv and
# mediapipe load at import on a server with no desktop.
# ---------------------------------------------------------------------------
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q \
    python3 python3-venv libgl1 libglib2.0-0 \
    mariadb-server \
    curl gnupg debian-keyring debian-archive-keyring apt-transport-https

python3 -c 'import sys; sys.exit(sys.version_info[:2] != (3, 11))' \
    || fail "python3 is $(python3 --version), the app needs 3.11 - use a Debian 12 image"

# Caddy from its own apt repository - Debian 12's archive does not carry a
# current release.
if ! command -v caddy >/dev/null; then
    curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
        | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
        > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -q
    apt-get install -y -q caddy
fi

# ---------------------------------------------------------------------------
step "2. Service account and writable folders"
# ---------------------------------------------------------------------------
id "$APP_USER" >/dev/null 2>&1 \
    || useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"

for dir in dataset dataset_staging trainer logs; do
    mkdir -p "$APP_DIR/$dir"
done

chown -R "$APP_USER:$APP_USER" "$APP_DIR"
# Face images and templates: readable by the service account only.
chmod 700 "$APP_DIR/dataset" "$APP_DIR/dataset_staging" "$APP_DIR/trainer"

# ---------------------------------------------------------------------------
step "3. MariaDB"
# ---------------------------------------------------------------------------
install -m 644 "$APP_DIR/deploy/gcp/mariadb-attendance.cnf" \
    /etc/mysql/mariadb.conf.d/60-attendance.cnf
systemctl restart mariadb

# ---------------------------------------------------------------------------
step "4. .env"
#
# Written once. Re-running keeps the existing secrets, because a new
# SECRET_KEY logs everyone out and a new DB password would no longer match
# the database user.
# ---------------------------------------------------------------------------
ENV_FILE="$APP_DIR/.env"

if [ ! -f "$ENV_FILE" ]; then
    DB_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
    SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"

    cat > "$ENV_FILE" <<EOF
# Written by deploy/bootstrap.sh. Holds credentials - keep it mode 600.
SECRET_KEY=$SECRET_KEY
FLASK_DEBUG=false

# serve.py listens on loopback; only Caddy reaches it.
FLASK_HOST=127.0.0.1
WSGI_PORT=8000
WSGI_THREADS=8

# One proxy (Caddy) sets X-Forwarded-For. See config/settings.py.
PROXY_HOPS=1
# The browser is on HTTPS even though this process speaks HTTP to Caddy.
SESSION_COOKIE_SECURE=true

DB_HOST=127.0.0.1
DB_PORT=3306
DB_USER=$DB_USER
DB_PASSWORD=$DB_PASSWORD
DB_NAME=$DB_NAME
AUTO_MIGRATE=true

RECOGNITION_THRESHOLD=58.0
LOG_LEVEL=INFO
EOF
    chown "$APP_USER:$APP_USER" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    echo "    wrote $ENV_FILE"

    # The app connects as a user that owns one database and nothing else.
    mariadb <<SQL
CREATE USER IF NOT EXISTS '$DB_USER'@'localhost' IDENTIFIED BY '$DB_PASSWORD';
ALTER USER '$DB_USER'@'localhost' IDENTIFIED BY '$DB_PASSWORD';
GRANT ALL PRIVILEGES ON \`$DB_NAME\`.* TO '$DB_USER'@'localhost';
SQL
else
    echo "    $ENV_FILE exists - kept"
fi

# ---------------------------------------------------------------------------
step "5. Python environment"
#
# Editable install, as scripts/setup.sh does. A plain `pip install .` copies
# web/ into site-packages, where its template_folder="../templates" no longer
# points at the templates.
# ---------------------------------------------------------------------------
sudo -u "$APP_USER" python3 -m venv "$APP_DIR/.venv"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --upgrade pip --quiet
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -e "$APP_DIR" --quiet

# ---------------------------------------------------------------------------
step "6. Database schema and admin account"
#
# setup_db.py seeds admin/admin, which is public knowledge. On a machine on
# the internet that is a working login for anyone who tries it first, so it is
# replaced here with a random password, printed once. must_change_password
# stays set: the first login still has to choose a new one.
# ---------------------------------------------------------------------------
cd "$APP_DIR"
sudo -u "$APP_USER" .venv/bin/python setup_db.py

sudo -u "$APP_USER" .venv/bin/python - <<'PY'
import secrets

from infra.db import db_cursor
from security.passwords import hash_password, verify_password

with db_cursor(commit=True) as cursor:
    cursor.execute("SELECT password FROM admin WHERE username = 'admin'")
    row = cursor.fetchone()

    if row is None or not verify_password("admin", row[0]):
        print("    admin password is not the default - kept")
    else:
        password = secrets.token_urlsafe(12)
        cursor.execute(
            "UPDATE admin SET password = %s, must_change_password = 1 "
            "WHERE username = 'admin'",
            (hash_password(password),),
        )
        print()
        print("    ONE-TIME ADMIN PASSWORD (shown once, not stored anywhere):")
        print(f"        username: admin    password: {password}")
        print("    You will be asked to change it at first login.")
PY

# ---------------------------------------------------------------------------
step "7. Services"
# ---------------------------------------------------------------------------
install -m 644 "$APP_DIR/deploy/gcp/attendance.service" /etc/systemd/system/attendance.service
systemctl daemon-reload
systemctl enable attendance
systemctl restart attendance

sed "s/{\$DOMAIN}/$DOMAIN/" "$APP_DIR/deploy/gcp/Caddyfile" > /etc/caddy/Caddyfile
mkdir -p /var/log/caddy
chown caddy:caddy /var/log/caddy
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
systemctl enable caddy
systemctl reload-or-restart caddy

step "Done"
echo "    https://$DOMAIN"
echo "    Logs: journalctl -u attendance -f    |    journalctl -u caddy -f"
