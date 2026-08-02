"""Delete rows older than their retention window from the append-only
time-series tables (prices, market_history, position_history).

Added for the Railway Postgres volume-at-98% incident - prices grows
~230k rows/day with no retention policy. Safe to run repeatedly (each run
only ever deletes rows already past their own table's retention window);
the same core (app/utils/pruning.py) is what the daily "pruner"
PeriodicJob in app/main.py runs automatically going forward - this script
is for a manual/on-demand run.

Usage:
    uv run python scripts/prune_old_data.py
"""

import logging
from datetime import UTC, datetime

from app.config.settings import get_settings
from app.db.session import db_session
from app.utils.logging import setup_logging
from app.utils.pruning import prune_all

logger = logging.getLogger(__name__)


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_dir, settings.log_to_file)

    now = datetime.now(UTC)
    with db_session() as session:
        results = prune_all(session, settings, now)

    total_deleted = sum(result.rows_deleted for result in results)
    logger.info("prune_old_data: done, %d total rows deleted", total_deleted)
    for result in results:
        print(
            f"{result.table}: {result.rows_before} -> {result.rows_after} "
            f"(deleted {result.rows_deleted})"
        )


if __name__ == "__main__":
    main()
