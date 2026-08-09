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

from pydantic import Field, field_validator
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

    # -----------------------------------------------------------------
    # Session hardening (SE-11)
    #
    # session_cookie_secure defaults to False, and that is a decision
    # rather than an oversight. The deployed system is the Flask
    # development server over plain HTTP on localhost (PO-4, CO-3);
    # setting Secure there means the browser never sends the cookie back
    # and nobody can log in at all. Turn it on - and you should - the
    # moment this sits behind TLS.
    #
    # HTTPONLY and SAMESITE are not configurable: there is no deployment
    # of this application that wants JavaScript reading the session
    # cookie, or wants it sent on a cross-site POST.
    # -----------------------------------------------------------------
    session_cookie_secure: bool = False
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

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        level = value.strip().upper()
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if level not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}, got {value!r}")
        return level

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
