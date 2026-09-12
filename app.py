"""
The entry point.

**This file was 2,890 lines and 43 routes** - HTTP, SQL, filesystem work and
process management in the same function bodies (MA-1). Phase 5 split it into
`web/` (eight blueprints), `services/` (orchestration) and `repositories/`
(every SQL statement); see `web/__init__.py` for the layering and why the
endpoint names changed while the URLs did not.

What is left here is what genuinely belongs to *the process*: configuring
logging, building the app, and the two start-up checks that must run when
somebody types `python app.py` and must **not** run when a test imports this
module.
"""

import logging
import sys

from config.logging_config import configure_logging

# Aliased deliberately. There used to be a route handler named `settings()` in
# this file, and importing the config object as a bare `settings` let that def
# shadow it: nothing failed at import, and every database call then raised
# AttributeError at request time (tasks/lessons.md L5). The route now lives in
# web/account.py, but the alias stays - it costs nothing and the trap it
# documents is one import away from returning.
from config.settings import settings as app_config
from infra import dataset_store
from infra.migrations import ensure_schema_current
from web import create_app

# app.py is the entry point, so it owns logging configuration for the process.
configure_logging()

logger = logging.getLogger(__name__)

# Module-level, and it must stay that way: `python app.py` runs it, the test
# suite does `import app` and drives `app.app.test_client()`, and the
# integration fixtures redirect `settings.db_name` and then import this.
app = create_app()


# ==============================
# START-UP SCHEMA CHECK
#
# This system is deployed by copying it to another machine, so the database a
# given copy talks to may have been created by an older version of the code.
# Nothing used to notice: `CREATE TABLE IF NOT EXISTS` is a no-op on a table
# that already exists, so the application started happily and then failed on
# whichever page first touched a column that was never added. An attendance
# system that boots and *then* 500s on the register is worse than one that
# refuses to boot and says why.
#
# ⚠️ Called from __main__ only, never at import. The test suite imports this
# module and CI has no database at all; a connection attempt at import would
# make the whole suite depend on a running server. An `ast` test enforces it.
# ==============================


def check_database_on_startup():
    """
    Verify the schema, and bring it up to date if `auto_migrate` allows.

    Returns True if the application should start. Each failure prints the one
    instruction that fixes it, because "start MySQL", "run setup_db.py" and "a
    migration failed" are three different problems and a single "database
    error" tells the operator none of them apart.
    """
    state = ensure_schema_current(apply=app_config.auto_migrate)

    if state.ok:
        if state.reason == "migrated":
            logger.warning(
                "Database schema was out of date. Applied %d migration(s): %s",
                len(state.applied),
                ", ".join(m.label for m in state.applied),
            )
        else:
            logger.info("Database schema is up to date")

        return True

    logger.error("Cannot start: %s", state.detail or state.reason)

    if state.reason == "stale":
        logger.error(
            "Automatic migration is disabled (AUTO_MIGRATE=false). "
            "Run: python scripts/migrate.py"
        )

    elif state.reason == "migration-failed":
        logger.error(
            "The schema is part-migrated. Do not start the application until "
            "this is resolved - see the migration file named above and "
            "tasks/lessons.md L11."
        )

    return False


def sweep_abandoned_enrolments():
    """
    Remove staging folders left by closed tabs.

    They are unreadable by the rest of the system, but they are still face
    images of somebody who may never have completed enrolment, and
    docs/data_privacy.md is a document this system is meant to be able to
    honour. Failure here is logged, never fatal: leftover images are a privacy
    tidy-up, not a reason to refuse to run.
    """
    try:
        removed = dataset_store.sweep()

        if removed:
            logger.info(
                "Removed %d abandoned enrolment staging folder(s)", removed
            )

    except OSError:
        logger.exception("Could not sweep abandoned enrolment staging folders")


# ==============================
# RUN APP
# ==============================
if __name__ == '__main__':
    if not check_database_on_startup():
        sys.exit(1)

    sweep_abandoned_enrolments()

    ssl_context = app_config.ssl_context

    if ssl_context is None:
        # ⚠️ Not a warning about security in the abstract. Browser enrolment
        # needs a *secure context*, so on plain HTTP it works from the server's
        # own browser via localhost and cannot work from anywhere else - the
        # camera call is rejected outright, with no prompt and nothing to
        # click through. An operator trying to enrol from a laptop will
        # otherwise conclude the camera is broken.
        logger.warning(
            "Starting without TLS. Enrolment will work on this machine "
            "(http://localhost) and nowhere else - getUserMedia needs a "
            "secure context. Run: python scripts/make_dev_cert.py"
        )
    else:
        # ⚠️ Host 0.0.0.0 only once TLS is configured. Binding every interface
        # on plain HTTP would put the session cookie and every register on the
        # network in clear, which is the one thing SESSION_COOKIE_SECURE
        # cannot help with.
        logger.info(
            "Starting with TLS from %s", app_config.ssl_cert_file
        )

    # Still the Flask development server - that is PO-4, and a WSGI server
    # belongs with a deployment decision rather than with this phase.
    app.run(
        host=app_config.flask_host,
        debug=app_config.flask_debug,
        ssl_context=ssl_context,
    )
