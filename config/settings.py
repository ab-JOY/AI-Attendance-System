"""
Single source of truth for everything that used to be hardcoded in source.

Addresses SE-8 (hardcoded credentials duplicated across five files) and PO-2
(no configuration layer - moving the system to another machine meant editing
source). Values come from environment variables, optionally loaded from a
local `.env`; see `.env.example` for the documented set.

Import the module-level `settings` object rather than constructing `Settings()`
yourself, so the whole process shares one validated configuration:

    from config.settings import settings
    conn = mysql.connector.connect(**settings.db_kwargs())

Two things deliberately do NOT live here - see the notes on
RECOGNITION_THRESHOLD below and on LBPH_PARAMS in train_model.py.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repository root: config/settings.py -> config/ -> project root.
BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Environment-driven configuration, validated once at import time."""

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # -----------------------------------------------------------------
    # Database
    # -----------------------------------------------------------------
    db_host: str = "127.0.0.1"
    db_port: int = 3306
    db_user: str = "root"
    db_password: str = ""
    db_name: str = "attendancesystem_db"

    # Connection pool size (PE-7). Five is mysql-connector's own default and
    # is ample for the Flask development server this deploys on (PO-4), which
    # serves a handful of concurrent requests at most. Raising it costs open
    # connections against MySQL's max_connections whether they are used or
    # not, so it is configurable rather than generous.
    #
    # A leak is what makes this dangerous rather than merely small: an
    # exception that skips a close permanently removes one connection from a
    # pool of five. infra/db.py exists so that cannot happen.
    db_pool_size: int = 5

    # Bring the schema up to date automatically when `python app.py` starts.
    #
    # On by default because this system is deployed by copying it to another
    # machine: a tester pulling a newer version onto a database created by an
    # older one would otherwise get no warning at all, since
    # CREATE TABLE IF NOT EXISTS is a no-op on an existing table. The
    # application would start and then fail on whichever page first touched a
    # column that was never added.
    #
    # ⚠️ Turn this off (`AUTO_MIGRATE=false`) anywhere that is not a
    # single-process development server. Two workers starting at once would
    # both try to apply the same migration, and nothing here takes a lock.
    # `python scripts/migrate.py` is the deliberate alternative.
    auto_migrate: bool = True

    # -----------------------------------------------------------------
    # Flask
    #
    # secret_key has no default on purpose. The previous hardcoded value
    # ("ai_attendance_secret_key") is in git history and therefore public;
    # a known Flask secret key means anyone can forge a session cookie and
    # log in as admin. A missing key must be a loud boot failure, never a
    # quiet fallback to something guessable.
    # -----------------------------------------------------------------
    secret_key: str = Field(min_length=16)
    flask_debug: bool = False
    flask_host: str = "127.0.0.1"

    # -----------------------------------------------------------------
    # TLS (CO-3, and the prerequisite for remote browser enrolment)
    #
    # ⚠️ **`getUserMedia` is only available in a secure context.** Browsers
    # expose the camera on HTTPS or on `localhost`, and nowhere else -
    # there is no flag, no prompt and no graceful degradation. So
    # enrolment works on the server machine as shipped and *cannot* work
    # from another machine's browser until these are set. That is not a
    # polish task; it is the whole of CO-3.
    #
    # Both or neither. One without the other is a startup failure rather
    # than a silent fall back to HTTP: an operator who set a certificate
    # and got plain HTTP anyway would have no way to tell, and would go
    # looking for the camera problem somewhere else entirely.
    #
    # ⚠️ The certificate **must carry a Subject Alternative Name** for the
    # host or IP it is reached by. Browsers stopped honouring the common
    # name years ago, and a CN-only certificate is refused outright with
    # no click-through. `scripts/make_dev_cert.py` generates correct SANs;
    # `mkcert` is better still, because it also installs a local CA and
    # there is then no warning screen to explain during a defense.
    # -----------------------------------------------------------------
    ssl_cert_file: Path | None = None
    ssl_key_file: Path | None = None

    # -----------------------------------------------------------------
    # Session hardening (SE-11)
    #
    # ⚠️ **session_cookie_secure now follows TLS rather than defaulting to
    # False**, and the change is the point.
    #
    # It was `False` because this deployment was plain HTTP on localhost:
    # setting Secure there means the browser never sends the cookie back
    # and nobody can log in at all. But that made the flip a thing somebody
    # had to remember on the day TLS arrived - and forgetting it means
    # session cookies travelling in clear over a network, which is the
    # failure the setting exists to prevent, in the one deployment where
    # it matters.
    #
    # Deriving it removes the remembering. Still overridable: set
    # SESSION_COOKIE_SECURE explicitly behind a TLS-terminating proxy,
    # where this process speaks HTTP but the browser does not.
    #
    # HTTPONLY and SAMESITE are not configurable: there is no deployment
    # of this application that wants JavaScript reading the session
    # cookie, or wants it sent on a cross-site POST.
    # -----------------------------------------------------------------
    session_cookie_secure: bool | None = None
    session_lifetime_minutes: int = 30

    # -----------------------------------------------------------------
    # Login throttling (SE-13)
    #
    # Consecutive failures per (username, client address). See
    # security/rate_limit.py for why this is in-memory.
    # -----------------------------------------------------------------
    login_max_attempts: int = 5
    login_lockout_seconds: int = 900
    login_attempt_window_seconds: int = 900

    # -----------------------------------------------------------------
    # Recognition
    #
    # Configurable, but NOT free to change. This is the LBPH distance below
    # which a match is accepted, and it is calibrated against the distance
    # scale that LBPH_PARAMS produces. Raising `neighbors` once moved that
    # scale from ~35 to 81-107 while this stayed at 58.0, which silently
    # rejected every face in the system - 0/60 on a held-out split, with no
    # error anywhere. See tasks/lessons.md L2 and todo.md PE-0.
    #
    # Deriving a justified operating point (FAR/FRR/EER) is Phase 6 work.
    # Until then 58.0 is the measured-working value, not a tuned one.
    # -----------------------------------------------------------------
    recognition_threshold: float = 58.0

    # -----------------------------------------------------------------
    # Logging
    # -----------------------------------------------------------------
    log_level: str = "INFO"
    log_dir: Path = BASE_DIR / "logs"
    log_max_bytes: int = 5 * 1024 * 1024
    log_backup_count: int = 5

    # -----------------------------------------------------------------
    # Paths
    #
    # dataset_dir holds face images of identifiable students and trainer_dir
    # holds the biometric templates derived from them. Both are gitignored
    # and must stay that way - see .gitignore and CLAUDE.md.
    # -----------------------------------------------------------------
    dataset_dir: Path = BASE_DIR / "dataset"
    trainer_dir: Path = BASE_DIR / "trainer"

    # Browser enrolment writes here first and the folder is moved into
    # dataset_dir only once all hundred images exist, so a cancelled or
    # abandoned capture leaves nothing behind (FS-9). A partial capture is
    # every bit as much biometric data as a finished one: gitignored, and it
    # must stay that way.
    dataset_staging_dir: Path = BASE_DIR / "dataset_staging"

    # ⚠️ `reports_dir` used to be here and is gone (Phase 5, PE-8/FS-11).
    #
    # Generated Excel exports were written to a fixed filename in the working
    # directory (MA-8), then to a timestamped file under `reports/`. Both kept
    # a copy of an attendance register on the server's disk for ever, which is
    # a retention question `docs/data_privacy.md` has no answer for.
    #
    # The export builds the workbook in an in-memory buffer and hands it
    # straight to `send_file`. **Nothing touches the disk**, so there is no
    # directory to configure. (This note used to say the export "builds a
    # temporary file, streams it and deletes it" - that was the first
    # implementation, and it was replaced during the same sprint because the
    # `call_on_close` hook that did the deleting does not fire under the test
    # client; every export left a complete register in the system temp
    # directory. Corrected in the handover and not here, until now.)
    #
    # `reports/` stays in `.gitignore` for anything an earlier version already
    # wrote there.

    # -----------------------------------------------------------------
    # Uploads
    #
    # Browser enrolment is the first thing in this application to accept a
    # request body, so there was no size limit anywhere before it. A frame is
    # a JPEG of a 1920x1080 capture, comfortably under 1 MB at the quality the
    # page encodes at; 4 MB leaves room for a generous camera without letting
    # an authenticated client post something that has to be held in memory to
    # be rejected.
    #
    # Flask turns a body over this into a 413 before the view runs.
    # -----------------------------------------------------------------
    max_upload_bytes: int = 4 * 1024 * 1024

    # An enrolment session that nobody has sent a frame to for this long is
    # abandoned - a closed tab, a machine that went to sleep - and the next
    # operator may take the slot. Without it, one closed tab locks enrolment
    # until the server restarts.
    enrolment_idle_timeout_seconds: int = 300

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        level = value.strip().upper()
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if level not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}, got {value!r}")
        return level

    @model_validator(mode="after")
    def _validate_tls(self) -> Settings:
        """
        The certificate and the key are a pair, and both must exist.

        Checked at start-up rather than at `app.run()`, so a mistyped path is
        a refusal to boot with the path in the message - not a server that
        comes up on HTTP while the operator believes it is on HTTPS.
        """
        if bool(self.ssl_cert_file) != bool(self.ssl_key_file):
            missing = "SSL_KEY_FILE" if self.ssl_cert_file else "SSL_CERT_FILE"
            raise ValueError(
                f"{missing} is required when the other is set. TLS needs both "
                "the certificate and its private key; one alone would silently "
                "leave the server on plain HTTP."
            )

        for label, path in (
            ("SSL_CERT_FILE", self.ssl_cert_file),
            ("SSL_KEY_FILE", self.ssl_key_file),
        ):
            if path is not None and not path.is_file():
                raise ValueError(f"{label} does not exist: {path}")

        return self

    @property
    def tls_enabled(self) -> bool:
        return self.ssl_cert_file is not None and self.ssl_key_file is not None

    @property
    def ssl_context(self) -> tuple[Path, Path] | None:
        """`(cert, key)` for `app.run(ssl_context=...)`, or None for HTTP."""
        if not self.tls_enabled:
            return None

        return (self.ssl_cert_file, self.ssl_key_file)

    @property
    def secure_cookies(self) -> bool:
        """
        Whether to set the `Secure` flag on the session cookie (SE-11).

        Follows TLS unless explicitly overridden - see the note on
        `session_cookie_secure`.
        """
        if self.session_cookie_secure is not None:
            return self.session_cookie_secure

        return self.tls_enabled

    @property
    def trainer_file(self) -> Path:
        return self.trainer_dir / "trainer.yml"

    @property
    def labels_file(self) -> Path:
        return self.trainer_dir / "labels.txt"

    def db_kwargs(self) -> dict[str, object]:
        """Connection keyword arguments for mysql.connector.connect()."""
        return {
            "host": self.db_host,
            "port": self.db_port,
            "user": self.db_user,
            "password": self.db_password,
            "database": self.db_name,
        }


settings = Settings()
