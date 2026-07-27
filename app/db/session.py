"""Engine, session factory, and transactional scope for the app.

Why lazy: importing this module (e.g. from Alembic's env.py, or from a test
that only needs models) must never open a socket or require a live database.
The engine is only constructed the first time get_engine() is called.
"""

from collections.abc import Generator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config.settings import get_settings


@lru_cache
def get_engine() -> Engine:
    """Create (once) and cache the SQLAlchemy engine.

    pool_pre_ping=True: issues a cheap SELECT 1 before handing out a pooled
    connection, so a connection dropped by the DB/network (idle timeout,
    restart) is detected and replaced instead of raising mid-transaction.
    """
    return create_engine(get_settings().database_url, pool_pre_ping=True)


@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    """Cache the sessionmaker bound to the lazily-created engine."""
    return sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)


@contextmanager
def db_session() -> Generator[Session, None, None]:
    """Provide a transactional session: commit on success, rollback on error, always close."""
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
