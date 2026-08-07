"""
Logging setup for the attendance system (RE-5).

Replaces ~200 bare `print()` calls that had no levels, no timestamps, no
persistence, and no way to turn the noisy ones off. A per-face-per-frame
diagnostic and a fatal database error were previously indistinguishable
lines on the same stream.

Usage
-----
Library modules take a logger and nothing else:

    import logging
    logger = logging.getLogger(__name__)

Only entry points configure handlers, exactly once, before doing any work:

    from config.logging_config import configure_logging
    configure_logging()

That split matters. If a library module configured logging at import, then
importing it would silently reconfigure logging for whatever imported it -
so `import train_model` from a test or a script would hijack the caller's
handlers. Configuration belongs to whoever owns the process.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from config.settings import settings

_CONSOLE_FORMAT = "%(levelname)-8s %(name)s: %(message)s"
_FILE_FORMAT = "%(asctime)s %(levelname)-8s %(name)s:%(lineno)d %(message)s"

_configured = False


def configure_logging(level: str | None = None, *, force: bool = False) -> None:
    """
    Attach a console handler and a rotating file handler to the root logger.

    Safe to call more than once: repeated calls are ignored unless `force` is
    set, so an entry point that imports another entry point cannot end up with
    duplicated handlers printing every line twice.

    `level` overrides settings.log_level for the console and file handlers.
    Set LOG_LEVEL=DEBUG in .env to see the per-frame recognition diagnostics;
    at INFO they are suppressed, which is the point - they fire per face per
    frame and make the log unreadable within a minute.
    """
    global _configured

    if _configured and not force:
        return

    resolved_level = (level or settings.log_level).upper()

    root = logging.getLogger()
    root.setLevel(resolved_level)

    for existing in list(root.handlers):
        root.removeHandler(existing)

    console = logging.StreamHandler(stream=sys.stdout)
    console.setLevel(resolved_level)
    console.setFormatter(logging.Formatter(_CONSOLE_FORMAT))
    root.addHandler(console)

    # A file handler is a convenience, not a requirement. If the log directory
    # cannot be created - a read-only volume, a permissions problem - the
    # application must still start and still log to the console. Losing the
    # log file is an inconvenience; refusing to take attendance is an outage.
    try:
        settings.log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            settings.log_dir / "app.log",
            maxBytes=settings.log_max_bytes,
            backupCount=settings.log_backup_count,
            encoding="utf-8",
        )
        file_handler.setLevel(resolved_level)
        file_handler.setFormatter(logging.Formatter(_FILE_FORMAT))
        root.addHandler(file_handler)
    except OSError as error:
        root.warning("File logging disabled, could not open %s: %s", settings.log_dir, error)

    # mediapipe and matplotlib are chatty at DEBUG and drown out our own
    # output when LOG_LEVEL=DEBUG is set to inspect recognition behaviour.
    logging.getLogger("mediapipe").setLevel(logging.WARNING)
    logging.getLogger("matplotlib").setLevel(logging.WARNING)

    _configured = True
