"""Per-category wallet skill scoring - docs/PHASE6_DESIGN.md workstream 3's
win-rate/shrinkage/decay/zombie machinery, applied per (wallet, category)
rather than folded into the global TraderScore.

Why a parallel table, not a TraderScore column: the GMX "ROI-per-asset"
lesson this was built for is that skill is category-specific - a wallet
sharp on Sports consensus may have no edge (or negative edge) on Politics,
and averaging the two into one number hides exactly the signal the
consensus engine needs (see app/consensus/engine.py's _weighted_score()
docstring for how this compounds with Workstream 1's clustering). The
existing TraderScore/compute_score (app/consensus/scoring.py) stays exactly
as-is and keeps doing what it already does well: PnL + consistency-based
ranking for is_tracked selection.

Split into a pure core (decayed tally, per-category population mean,
shrinkage, Wilson lower bound) and a thin DB loader/writer
(load_category_trade_records, run_category_scoring), same convention as
app/risk/calibration.py and every other Phase 5/6 module - the statistics
are unit-testable without a database.

Formula (docs/PHASE6_DESIGN.md workstream 3, flags 9-12):
1. Raw population: closed Position rows (is_open=false, cash_pnl>0 wins)
   plus "zombie" positions (is_open=true, market.end_date more than
   ranking_zombie_grace_days in the past) forced to count as a loss - a
   stuck position was never honestly resolved, and pretending it doesn't
   exist is exactly the exploit the zombie guard exists to close.
2. Time decay: each qualifying position contributes
   0.5 ** (age_days / ranking_halflife_days) to both its (wallet, category)
   bucket's decayed wins and decayed n - age_days measured from closed_at,
   or from (market.end_date + ranking_zombie_grace_days) for a zombie (the
   moment it was judged a loss, not whenever it first opened).
3. Shrinkage toward the CATEGORY's own population mean (not the global
   one - each category has its own base rate):
   shrunk_wins = wins + ranking_prior_strength * category_population_mean
   shrunk_n = n + ranking_prior_strength
4. Score = the Wilson lower bound of the shrunk (wins, n) pair, reusing
   app/risk/calibration.py's wilson_interval() rather than a second
   implementation (flag 12). At n=0 this reduces to the Wilson lower bound
   of (k*mean, k) - anchored to the category's own base rate, appropriately
   low/neutral rather than defaulting to the wallet's unrelated global
   score.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config.effective import get_effective_settings
from app.config.settings import Settings
from app.db.models import Market, Position, TraderCategoryScore, Wallet
from app.db.session import db_session
from app.risk.calibration import wilson_interval

logger = logging.getLogger(__name__)

_CONFIDENCE_Z = Decimal("1.96")


@dataclass(frozen=True)
class RankingConfig:
    """Every Workstream-3-machinery tunable - see the module docstring."""

    halflife_days: Decimal
    prior_strength: Decimal
    zombie_grace_days: int
    confidence_z: Decimal = _CONFIDENCE_Z

    @classmethod
    def from_settings(cls, settings: Settings) -> "RankingConfig":
        return cls(
            halflife_days=Decimal(settings.ranking_halflife_days),
            prior_strength=settings.ranking_prior_strength,
            zombie_grace_days=settings.ranking_zombie_grace_days,
        )


@dataclass(frozen=True)
class CategoryTradeRecord:
    """One qualifying position's category-scoring-relevant features - plain
    values, not an ORM row, so the pure functions below stay DB-free.
    age_days is already resolved by the loader (closed_at, or the zombie's
    forced-loss moment) - the pure core never touches "now" itself.
    """

    wallet_id: int
    category: str
    won: bool
    age_days: Decimal


@dataclass(frozen=True)
class WalletCategoryTally:
    wins: Decimal
    n: Decimal


def decayed_tallies(
    records: list[CategoryTradeRecord], halflife_days: Decimal
) -> dict[tuple[int, str], WalletCategoryTally]:
    """Groups records by (wallet_id, category) and folds each into a
    decay-weighted (wins, n) pair - one entry per contributing position,
    0.5 ** (age_days / halflife_days) contributed to n (and to wins too, if
    it was a win).
    """
    wins_by_key: dict[tuple[int, str], Decimal] = {}
    n_by_key: dict[tuple[int, str], Decimal] = {}
    for record in records:
        key = (record.wallet_id, record.category)
        weight = Decimal("0.5") ** (record.age_days / halflife_days)
        n_by_key[key] = n_by_key.get(key, Decimal(0)) + weight
        if record.won:
            wins_by_key[key] = wins_by_key.get(key, Decimal(0)) + weight

    return {
        key: WalletCategoryTally(wins=wins_by_key.get(key, Decimal(0)), n=n)
        for key, n in n_by_key.items()
    }


def population_means_by_category(
    tallies: dict[tuple[int, str], WalletCategoryTally],
) -> dict[str, Decimal]:
    """Each category's population mean win rate: the average, across every
    wallet with at least one qualifying position in that category, of that
    wallet's own decayed win rate (wins/n) - not a pooled tally
    (docs/PHASE6_DESIGN.md workstream 3: "averaged across every tracked
    wallet with at least one qualifying position").
    """
    rates_by_category: dict[str, list[Decimal]] = {}
    for (_, category), tally in tallies.items():
        if tally.n <= 0:
            continue
        rates_by_category.setdefault(category, []).append(tally.wins / tally.n)

    return {
        category: sum(rates, Decimal(0)) / Decimal(len(rates))
        for category, rates in rates_by_category.items()
    }


def compute_category_score(
    tally: WalletCategoryTally, population_mean: Decimal, config: RankingConfig
) -> Decimal:
    """Shrink the wallet's decayed (wins, n) toward the category's
    population mean, then take the Wilson lower bound of the shrunk pair -
    shrink first, bound second (docs/PHASE6_DESIGN.md workstream 3 flag 12).
    """
    shrunk_wins = tally.wins + config.prior_strength * population_mean
    shrunk_n = tally.n + config.prior_strength
    lower_bound, _ = wilson_interval(shrunk_wins, shrunk_n, config.confidence_z)
    return lower_bound


def run_category_scoring(settings: Settings) -> int:
    """The full pipeline: load qualifying positions, tally + shrink + bound
    per (wallet, category), write one TraderCategoryScore row per pair.
    Every category with at least one wallet's worth of population data gets
    a row for every tracked wallet (not just wallets with n>0 in it) - an
    n=0 wallet's score in that category is exactly as meaningful (anchored
    to the category's own base rate) as any other, and the consensus engine
    needs a real row to look up, not a hardcoded fallback, for the common
    case of "this wallet has never traded in this category."
    """
    config = RankingConfig.from_settings(settings)
    now = datetime.now(UTC)

    with db_session() as session:
        records = load_category_trade_records(session, config.zombie_grace_days, now)
        tracked_wallet_ids = set(
            session.execute(select(Wallet.id).where(Wallet.is_tracked.is_(True))).scalars()
        )

    tallies = decayed_tallies(records, config.halflife_days)
    population_means = population_means_by_category(tallies)

    rows = []
    for category, population_mean in population_means.items():
        for wallet_id in tracked_wallet_ids:
            tally = tallies.get((wallet_id, category), WalletCategoryTally(Decimal(0), Decimal(0)))
            score = compute_category_score(tally, population_mean, config)
            rows.append(
                TraderCategoryScore(
                    wallet_id=wallet_id,
                    category=category,
                    score=score,
                    resolved_trades=tally.n,
                    computed_at=now,
                )
            )

    if rows:
        with db_session() as session:
            session.add_all(rows)

    return len(rows)


def load_category_trade_records(
    session: Session, zombie_grace_days: int, now: datetime
) -> list[CategoryTradeRecord]:
    """Every tracked wallet's closed positions (wins if cash_pnl > 0) plus
    zombie positions (still open, market ended more than zombie_grace_days
    ago - forced losses), joined to Market for the category.
    """
    closed_rows = session.execute(
        select(Position.wallet_id, Market.category, Position.cash_pnl, Position.closed_at)
        .join(Market, Market.condition_id == Position.condition_id)
        .join(Wallet, Wallet.id == Position.wallet_id)
        .where(
            Wallet.is_tracked.is_(True),
            Position.is_open.is_(False),
            Position.closed_at.is_not(None),
        )
    ).all()

    records = [
        CategoryTradeRecord(
            wallet_id=row.wallet_id,
            category=row.category,
            won=row.cash_pnl > 0,
            age_days=_age_days(row.closed_at, now),
        )
        for row in closed_rows
    ]

    grace = timedelta(days=zombie_grace_days)
    zombie_rows = session.execute(
        select(Position.wallet_id, Market.category, Market.end_date)
        .join(Market, Market.condition_id == Position.condition_id)
        .join(Wallet, Wallet.id == Position.wallet_id)
        .where(
            Wallet.is_tracked.is_(True),
            Position.is_open.is_(True),
            Market.end_date.is_not(None),
            Market.end_date + grace < now,
        )
    ).all()

    records.extend(
        CategoryTradeRecord(
            wallet_id=row.wallet_id,
            category=row.category,
            won=False,
            age_days=_age_days(row.end_date + grace, now),
        )
        for row in zombie_rows
    )
    return records


def _age_days(past: datetime, now: datetime) -> Decimal:
    seconds = max((now - past).total_seconds(), 0.0)
    return Decimal(str(seconds / 86400))


def latest_category_scores(
    session: Session, wallet_ids: set[int]
) -> dict[tuple[int, str], Decimal]:
    """Each (wallet_id, category)'s most recent score - the lookup
    app/signals/generator.py needs for category-aware consensus weighting.
    """
    if not wallet_ids:
        return {}

    ranked = (
        select(
            TraderCategoryScore.wallet_id,
            TraderCategoryScore.category,
            TraderCategoryScore.score,
            func.row_number()
            .over(
                partition_by=[TraderCategoryScore.wallet_id, TraderCategoryScore.category],
                order_by=TraderCategoryScore.computed_at.desc(),
            )
            .label("rn"),
        )
        .where(TraderCategoryScore.wallet_id.in_(wallet_ids))
        .subquery()
    )
    rows = session.execute(
        select(ranked.c.wallet_id, ranked.c.category, ranked.c.score).where(ranked.c.rn == 1)
    ).all()
    return {(row.wallet_id, row.category): row.score for row in rows}


async def run_category_scoring_job() -> None:
    """Scheduler entrypoint - same asyncio.to_thread + get_effective_settings
    pattern every other Phase 6 job uses (app/optimization/clustering.py,
    clv.py, bandit.py).
    """
    settings = get_effective_settings()
    count = await asyncio.to_thread(run_category_scoring, settings)
    if count:
        logger.info("category_scoring: wrote %d (wallet, category) scores", count)
