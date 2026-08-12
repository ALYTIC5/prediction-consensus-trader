"""Startup guard: refuse to run a service against a database whose schema
is behind the code's migrations.

Built after the Scout service ran for 10 days against a database missing
its tables - every job tick raised UndefinedTable, was logged, and retried
on schedule forever, indistinguishable from a healthy idle service. Railway
only ran "alembic upgrade head" as a pre-deploy step for the collectors
service, so a scout-only deploy could (and did) ship code whose migrations
had never been applied. This check makes that fail loudly at startup
instead: log CRITICAL naming the missing revisions and exit non-zero, so
Railway shows a failed deploy rather than a process that starts and is
never actually able to do anything.
"""

import logging
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]


class SchemaOutOfDateError(RuntimeError):
    """The database's alembic revision is behind the code's head revision."""


def _script_directory() -> ScriptDirectory:
    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_REPO_ROOT / "alembic"))
    return ScriptDirectory.from_config(cfg)


def check_schema_current(service: str, engine: Engine) -> None:
    """Compare the database's current alembic revision(s) against the code's
    head revision(s). Returns silently if they match.

    A database that can't be reached at all is deliberately NOT an error
    here - that's /healthz's job (it already returns 503 on a DB outage),
    not a reason to refuse to boot. It also keeps this check safe to run at
    import time in the dashboard, whose test suite reloads app.dashboard.main
    against a DATABASE_URL that was never meant to resolve.
    """
    script = _script_directory()
    code_heads = set(script.get_heads())

    try:
        with engine.connect() as connection:
            db_heads = set(MigrationContext.configure(connection).get_current_heads())
    except Exception:
        logger.warning(
            "%s: could not reach the database to verify schema revision - "
            "continuing startup, /healthz will catch a real DB outage",
            service,
            exc_info=True,
        )
        return

    if db_heads == code_heads:
        return

    missing = [rev.revision for rev in script.iterate_revisions(list(code_heads), list(db_heads))]
    logger.critical(
        "%s: database schema is behind the code - db is at %s, code expects %s. "
        "Missing revisions (newest first): %s. Run 'alembic upgrade head' "
        "against this database before starting %s.",
        service,
        sorted(db_heads) or "no revisions applied",
        sorted(code_heads),
        missing,
        service,
    )
    raise SchemaOutOfDateError(
        f"{service}: database is missing {len(missing)} migration(s): {missing}"
    )
