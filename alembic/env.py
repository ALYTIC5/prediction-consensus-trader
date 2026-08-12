"""Alembic environment.

Why the URL is injected here instead of alembic.ini: get_settings() is the
single source of truth for credentials (CLAUDE.md — os.environ is read only
in app/config/settings.py), so migrations always target the same database
the running app would, in every environment, with no second place to update.
"""

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool, text

import app.db.models  # noqa: F401  (registers every model on Base.metadata)
from alembic import context
from app.config.settings import get_settings
from app.db.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# configparser treats "%" as the start of an interpolation token. Our
# database_url can contain a "%" (quote_plus-encoded password characters),
# so it must be escaped as "%%" before being stored via set_main_option,
# which writes through a ConfigParser-backed option.
_db_url = get_settings().database_url
config.set_main_option("sqlalchemy.url", _db_url.replace("%", "%%"))


def run_migrations_offline() -> None:
    """Run migrations without a live DB connection, emitting SQL to stdout."""
    context.configure(
        url=_db_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


# Arbitrary fixed key for pg_advisory_lock - only needs to be constant across
# every migrator, never collide with another advisory lock user, and fit in
# a bigint. Two services can now both have "alembic upgrade head" as a
# pre-deploy command (collectors and scout) without racing DDL against each
# other: the second migrator blocks here until the first's transaction (and
# therefore its lock) releases, then finds itself already at head and exits
# immediately.
_MIGRATION_LOCK_KEY = 727_310_001


def run_migrations_online() -> None:
    """Run migrations against a live DB connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": _MIGRATION_LOCK_KEY})
        # pg_advisory_lock is session-scoped, not transaction-scoped, so
        # committing here doesn't release it - it only closes out the
        # transaction SQLAlchemy auto-began for that SELECT. Without this,
        # Alembic's own begin_transaction() below silently joins that
        # already-open transaction instead of managing one of its own, and
        # the migration's DDL is never actually committed - it looked like
        # "upgrade head" succeeded (no error, exit 0) while leaving the
        # database completely unchanged.
        connection.commit()
        try:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
            )

            with context.begin_transaction():
                context.run_migrations()
        finally:
            connection.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": _MIGRATION_LOCK_KEY}
            )


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
