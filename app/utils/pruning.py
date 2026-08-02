"""Retention pruning for append-only time-series tables.

Added for the Railway Postgres volume-at-98% incident: prices grows ~230k
rows/day (one row per outcome token, per markets-collector cycle) with
nothing ever deleting old rows - market_history and position_history are
the same shape, just slower-growing. All three are pure time-series logs
nothing downstream reads past a few weeks old, so a rolling retention
window is a safe, ordinary maintenance operation, not a data-loss risk.

Deletes are batched (BATCH_SIZE rows per DELETE, one committed transaction
per batch) specifically so a multi-million-row prune never holds one long
lock against a table the collectors are actively writing to - many small,
fast transactions instead of one giant one.

Table/column names below are a small, fixed, code-defined list (never
user input) - the only reason raw SQL with an interpolated identifier is
safe here at all; nothing about this pattern would be safe with a
caller-supplied table name.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config.effective import get_effective_settings
from app.config.settings import Settings
from app.db.session import db_session

logger = logging.getLogger(__name__)

BATCH_SIZE = 10000


@dataclass(frozen=True)
class PruneTarget:
    table: str
    timestamp_column: str
    retention_days: int


@dataclass(frozen=True)
class PruneResult:
    table: str
    rows_before: int
    rows_deleted: int
    rows_after: int


def prune_targets_from_settings(settings: Settings) -> list[PruneTarget]:
    return [
        PruneTarget("prices", "captured_at", settings.prices_retention_days),
        PruneTarget("market_history", "captured_at", settings.market_history_retention_days),
        PruneTarget("position_history", "detected_at", settings.position_history_retention_days),
    ]


def _row_count(session: Session, table: str) -> int:
    return session.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()  # noqa: S608


def _delete_batch(
    session: Session, table: str, timestamp_column: str, cutoff: datetime, batch_size: int
) -> int:
    """One DELETE, bounded to at most batch_size rows via a subselect -
    Postgres has no DELETE ... LIMIT, so "delete these specific ids" is the
    standard way to bound a single DELETE statement's size.
    """
    result = session.execute(
        text(
            f"DELETE FROM {table} WHERE id IN "  # noqa: S608
            f"(SELECT id FROM {table} WHERE {timestamp_column} < :cutoff LIMIT :batch_size)"
        ),
        {"cutoff": cutoff, "batch_size": batch_size},
    )
    return result.rowcount


def prune_table(
    session: Session, target: PruneTarget, now: datetime, batch_size: int = BATCH_SIZE
) -> PruneResult:
    """Deletes every row in target.table older than target.retention_days,
    batch_size rows at a time, committing after each batch. Returns the
    row count before and after so the caller can log/verify the effect.
    """
    cutoff = now - timedelta(days=target.retention_days)
    rows_before = _row_count(session, target.table)

    deleted = 0
    while True:
        batch_deleted = _delete_batch(
            session, target.table, target.timestamp_column, cutoff, batch_size
        )
        deleted += batch_deleted
        session.commit()
        if batch_deleted < batch_size:
            break

    rows_after = rows_before - deleted
    logger.info(
        "pruning: %s rows_before=%d deleted=%d rows_after=%d (retention=%dd, cutoff=%s)",
        target.table,
        rows_before,
        deleted,
        rows_after,
        target.retention_days,
        cutoff.isoformat(),
    )
    return PruneResult(
        table=target.table, rows_before=rows_before, rows_deleted=deleted, rows_after=rows_after
    )


def prune_all(session: Session, settings: Settings, now: datetime) -> list[PruneResult]:
    return [prune_table(session, target, now) for target in prune_targets_from_settings(settings)]


def _run_pruning(settings: Settings) -> list[PruneResult]:
    now = datetime.now(UTC)
    with db_session() as session:
        return prune_all(session, settings, now)


async def run_pruner_job() -> None:
    """Scheduler entrypoint - same asyncio.to_thread + get_effective_settings
    pattern every periodic job in this project uses. Row counts before/
    after each table are already logged at INFO inside prune_table(); this
    just adds the run-level total.
    """
    settings = get_effective_settings()
    results = await asyncio.to_thread(_run_pruning, settings)
    total_deleted = sum(result.rows_deleted for result in results)
    if total_deleted:
        logger.info("pruner: total rows deleted this run=%d", total_deleted)
