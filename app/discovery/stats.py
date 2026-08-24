"""Aggregate niche_trade_grades into niche_wallet_stats - the niche-scoped
analogue of app/scout/screening.py's Stage 1 stat computation, reusing the
same wilson_interval (app/risk/calibration.py) rather than reimplementing
it.

Recomputed wholesale each run, not incrementally mutated - same "current
state, upserted in place" convention app/scout/screening.py's daily
re-screen and app/paper_trading/consensus.py's upsert both already
establish, and for the identical reason: idempotent under retry, and never
silently drifts from what niche_trade_grades actually contains.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config.settings import Settings
from app.db.models import NicheTradeGrade, NicheWalletStat
from app.db.session import db_session
from app.risk.calibration import wilson_interval

logger = logging.getLogger(__name__)

_CONFIDENCE_Z = Decimal("1.96")


@dataclass(frozen=True)
class GradeRecord:
    """Plain values, not an ORM row - keeps compute_wallet_niche_stat DB-free."""

    won: bool
    stake: Decimal
    price: Decimal


@dataclass(frozen=True)
class NicheStatResult:
    resolved_n: int
    wins: int
    wilson_low: Decimal
    wilson_high: Decimal
    roi: Decimal | None


def _payout_pnl(record: GradeRecord) -> Decimal:
    """Single-leg bet payout: won pays (1 - price) per dollar staked, lost
    forfeits the stake - the same binary economics
    app/scout/screening.py's profit_factor()/roi() apply to a closed
    Position, here applied to a single graded trade (see
    NicheTradeGrade's docstring on why trades aren't netted into positions).
    """
    if record.won:
        if record.price <= 0:
            return Decimal(0)
        return record.stake * (Decimal(1) - record.price) / record.price
    return -record.stake


def compute_wallet_niche_stat(
    records: list[GradeRecord], confidence_z: Decimal = _CONFIDENCE_Z
) -> NicheStatResult:
    """Pure: Wilson bound over win/loss, ROI over staked capital - no DB access."""
    total = len(records)
    wins = sum(1 for r in records if r.won)
    wilson_low, wilson_high = wilson_interval(wins, total, confidence_z)

    total_stake = sum((r.stake for r in records), Decimal(0))
    roi = None
    if total_stake > 0:
        total_pnl = sum((_payout_pnl(r) for r in records), Decimal(0))
        roi = total_pnl / total_stake

    return NicheStatResult(
        resolved_n=total, wins=wins, wilson_low=wilson_low, wilson_high=wilson_high, roi=roi
    )


def _load_grades_by_wallet_niche(session: Session) -> dict[tuple[int, str], list[GradeRecord]]:
    rows = session.execute(
        select(
            NicheTradeGrade.wallet_id,
            NicheTradeGrade.niche,
            NicheTradeGrade.won,
            NicheTradeGrade.stake,
            NicheTradeGrade.price,
        )
    ).all()
    grouped: dict[tuple[int, str], list[GradeRecord]] = defaultdict(list)
    for row in rows:
        grouped[(row.wallet_id, row.niche)].append(
            GradeRecord(won=row.won, stake=row.stake, price=row.price)
        )
    return dict(grouped)


def _upsert_stat(
    session: Session, wallet_id: int, niche: str, result: NicheStatResult, now: datetime
) -> None:
    stmt = (
        pg_insert(NicheWalletStat)
        .values(
            wallet_id=wallet_id,
            niche=niche,
            resolved_n=result.resolved_n,
            wins=result.wins,
            wilson_low=result.wilson_low,
            wilson_high=result.wilson_high,
            roi=result.roi,
            computed_at=now,
        )
        .on_conflict_do_update(
            index_elements=[NicheWalletStat.wallet_id, NicheWalletStat.niche],
            set_={
                "resolved_n": result.resolved_n,
                "wins": result.wins,
                "wilson_low": result.wilson_low,
                "wilson_high": result.wilson_high,
                "roi": result.roi,
                "computed_at": now,
            },
        )
    )
    session.execute(stmt)


@dataclass(frozen=True)
class StatsSummary:
    wallet_niches_computed: int


def run_niche_stats(settings: Settings) -> StatsSummary:
    """Recompute every (wallet, niche) pair's stats from scratch each run -
    the full niche_trade_grades table is small enough (one row per graded
    trade, not per snapshot) for this to stay cheap for a long time, same
    reasoning app/scout/screening.py's full-population re-screen already
    relies on.
    """
    now = datetime.now(UTC)
    with db_session() as session:
        grouped = _load_grades_by_wallet_niche(session)
        for (wallet_id, niche), records in grouped.items():
            result = compute_wallet_niche_stat(records)
            _upsert_stat(session, wallet_id, niche, result, now)

    summary = StatsSummary(wallet_niches_computed=len(grouped))
    logger.info(
        "discovery: niche_stats computed=%d wallet-niche pairs", summary.wallet_niches_computed
    )
    return summary
