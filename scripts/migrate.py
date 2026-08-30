"""
Apply schema migrations (PO-5).

    python scripts/migrate.py --status     # what has run, what has not
    python scripts/migrate.py --dry-run    # what would run, without running it
    python scripts/migrate.py              # apply everything pending

Connection details come from config/settings.py, so this cannot disagree with
the application about which server or database it is talking to. `setup_db.py`
calls the same code after creating the database, so a fresh install needs only
that one command; this script is for bringing an *existing* database forward.

`--database` points at a different schema, which is what the integration tests
use to build a scratch database. It is not a way to migrate production from a
laptop: everything else still comes from the local configuration.
"""

import argparse
import logging
import sys
from pathlib import Path

# Run as a script from the repository root, so the root has to be importable.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.logging_config import configure_logging  # noqa: E402
from infra.migrations import MigrationError, apply_all, status  # noqa: E402

logger = logging.getLogger(__name__)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Apply database schema migrations.")
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show which migrations have been applied, and exit.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show which migrations would be applied, without applying them.",
    )
    parser.add_argument(
        "--database",
        default=None,
        help="Migrate this database instead of the configured one.",
    )

    args = parser.parse_args(argv)

    configure_logging()

    try:
        if args.status:
            rows = status(database=args.database)

            if not rows:
                logger.warning("No migration files found")
                return 0

            for migration, applied in rows:
                logger.info(
                    "%s %s", "[applied]" if applied else "[pending]", migration.label
                )

            outstanding = sum(1 for _, applied in rows if not applied)
            logger.info("%d migration(s) pending", outstanding)
            return 0

        applied = apply_all(database=args.database, dry_run=args.dry_run)

        if not applied:
            logger.info("Nothing to do")
        elif args.dry_run:
            logger.info("%d migration(s) would be applied", len(applied))
        else:
            logger.info("%d migration(s) applied", len(applied))

        return 0

    except MigrationError:
        logger.exception("Migration failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
