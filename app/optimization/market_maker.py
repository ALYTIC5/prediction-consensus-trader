"""Market-maker detection - pure core + thin DB loader/writer, same split
every Phase 5/6 module in this project uses.

A market maker's positions are inventory artifacts of providing liquidity,
not directional views - including them in consensus weighting corrupts it
exactly the way a Sybil ring does (app/optimization/clustering.py), just
via a different mechanism: not fake independence, but a trading pattern
that never reflects an actual opinion on the outcome. Detected from three
signals, all derived from data already collected (position_history) - no
new collector needed:

1. HOLDING PERIOD: market makers flip fast. A wallet's median holding
   period (OPENED -> CLOSED round trip) far below the population median
   is suspicious.
2. BOTH-SIDES TRADING: a wallet that has opened BOTH outcome tokens of the
   same condition_id, across many of the markets it trades, is providing
   liquidity on both sides rather than taking a position.
3. BREADTH VS DEPTH: a wallet trading many markets shallowly (small
   average position size) looks like liquidity provision spread thin,
   not conviction concentrated in a few bets.

Explicitly NOT implemented - see docs/API_REFERENCE.md's /trades entry:
4. TAKER VS MAKER: verified live (2026-08-18) that the Data API's
   `/trades` response has no maker/taker role field at all - only `side`
   (BUY/SELL, a trade direction, not an order-book role). Not obtainable
   from any endpoint this project has documented. Documented here as a
   real limitation, not silently skipped.

Combined with a "noisy-OR": score = 1 - Π(1 - component_i). Any ONE
strong signal alone should flag a wallet (a wallet that holds both sides
of most markets it touches is a market maker regardless of its holding
period), not require all three to agree - this is why it's a
probabilistic OR, not a weighted average (which would dilute a single
strong signal against two weak ones) or a product (which would require
every signal to agree).
"""

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config.effective import get_effective_settings
from app.config.settings import Settings
from app.db.models import PositionHistory, Wallet, WalletMarketMakerScore
from app.db.session import db_session

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WalletPositionEvent:
    """One position_history row's market-maker-relevant fields - plain
    values, not an ORM row, so the pure functions below stay DB-free.
    """

    condition_id: str
    outcome: str
    event_type: str
    size_before: Decimal | None
    size_after: Decimal
    avg_price: Decimal
    detected_at: datetime


@dataclass(frozen=True)
class WalletFeatures:
    wallet_id: int
    median_holding_hours: Decimal | None  # None if no completed OPENED->CLOSED round trip
    both_sides_ratio: Decimal  # fraction of distinct markets where 2+ outcomes were opened
    breadth: int  # distinct condition_ids ever opened
    avg_depth_usd: Decimal  # average notional (size * avg_price) at OPENED events


@dataclass(frozen=True)
class MarketMakerScoreResult:
    wallet_id: int
    holding_period_component: Decimal
    both_sides_component: Decimal
    breadth_depth_component: Decimal
    score: Decimal


def compute_wallet_features(
    wallet_id: int, events: Sequence[WalletPositionEvent]
) -> WalletFeatures | None:
    """Derives holding period, both-sides ratio, breadth, and depth from
    one wallet's full position_history - no cross-wallet comparison here
    (that's compute_market_maker_score's job, once the population median
    is known), so this stays a pure per-wallet fold.
    """
    if not events:
        return None
    ordered = sorted(events, key=lambda e: e.detected_at)

    open_at: dict[tuple[str, str], datetime] = {}
    holding_hours: list[Decimal] = []
    outcomes_by_condition: dict[str, set[str]] = {}
    depths: list[Decimal] = []

    for event in ordered:
        key = (event.condition_id, event.outcome)
        if event.event_type == "OPENED":
            open_at[key] = event.detected_at
            outcomes_by_condition.setdefault(event.condition_id, set()).add(event.outcome)
            depths.append(event.size_after * event.avg_price)
        elif event.event_type == "CLOSED" and key in open_at:
            elapsed = event.detected_at - open_at.pop(key)
            holding_hours.append(Decimal(str(elapsed.total_seconds() / 3600)))

    breadth = len(outcomes_by_condition)
    if breadth == 0:
        return None
    both_sides_markets = sum(1 for outcomes in outcomes_by_condition.values() if len(outcomes) >= 2)
    both_sides_ratio = Decimal(both_sides_markets) / Decimal(breadth)

    median_holding_hours = _median(holding_hours) if holding_hours else None
    avg_depth_usd = sum(depths, Decimal(0)) / Decimal(len(depths)) if depths else Decimal(0)

    return WalletFeatures(
        wallet_id=wallet_id,
        median_holding_hours=median_holding_hours,
        both_sides_ratio=both_sides_ratio,
        breadth=breadth,
        avg_depth_usd=avg_depth_usd,
    )


def _median(values: Sequence[Decimal]) -> Decimal:
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / Decimal(2)


def compute_holding_period_component(
    wallet_median_hours: Decimal | None, population_median_hours: Decimal | None
) -> Decimal:
    """1.0 when the wallet's median holding period is far below the
    population's (flips fast relative to everyone else this project
    tracks); 0 at or above the population median. No completed round
    trips on either side means "no evidence" - 0, not a fabricated
    extreme in either direction.
    """
    if (
        wallet_median_hours is None
        or population_median_hours is None
        or population_median_hours <= 0
    ):
        return Decimal(0)
    ratio = wallet_median_hours / population_median_hours
    return max(Decimal(0), min(Decimal(1), Decimal(1) - ratio))


def compute_breadth_depth_component(
    breadth: int, avg_depth_usd: Decimal, breadth_reference: int, depth_reference: Decimal
) -> Decimal:
    """High breadth (many markets) AND low depth (small average size)
    together look like liquidity spread thin, not conviction - this is a
    product of the two sub-scores (both conditions matter), unlike the
    outer noisy-OR combination (any ONE of the three top-level signals is
    independently enough to flag a wallet).
    """
    if breadth_reference <= 0 or depth_reference <= 0:
        return Decimal(0)
    breadth_score = max(Decimal(0), min(Decimal(1), Decimal(breadth) / Decimal(breadth_reference)))
    depth_score = max(Decimal(0), min(Decimal(1), Decimal(1) - avg_depth_usd / depth_reference))
    return breadth_score * depth_score


def compute_market_maker_score(
    both_sides_ratio: Decimal,
    holding_period_component: Decimal,
    breadth_depth_component: Decimal,
) -> Decimal:
    """Noisy-OR: any one strong signal alone should flag a wallet - see
    the module docstring for why this isn't a weighted average or a
    product.
    """
    combined = Decimal(1) - (
        (Decimal(1) - both_sides_ratio)
        * (Decimal(1) - holding_period_component)
        * (Decimal(1) - breadth_depth_component)
    )
    return max(Decimal(0), min(Decimal(1), combined))


def score_wallet(
    wallet_id: int,
    events: Sequence[WalletPositionEvent],
    population_median_holding_hours: Decimal | None,
    breadth_reference: int,
    depth_reference: Decimal,
) -> MarketMakerScoreResult | None:
    """The one entrypoint app/optimization/market_maker.py's DB layer
    needs - features and the combined score together, so a caller never
    accidentally scores from stale/mismatched features.
    """
    features = compute_wallet_features(wallet_id, events)
    if features is None:
        return None
    holding_component = compute_holding_period_component(
        features.median_holding_hours, population_median_holding_hours
    )
    breadth_depth_component = compute_breadth_depth_component(
        features.breadth, features.avg_depth_usd, breadth_reference, depth_reference
    )
    score = compute_market_maker_score(
        features.both_sides_ratio, holding_component, breadth_depth_component
    )
    return MarketMakerScoreResult(
        wallet_id=wallet_id,
        holding_period_component=holding_component,
        both_sides_component=features.both_sides_ratio,
        breadth_depth_component=breadth_depth_component,
        score=score,
    )


def consensus_weight_multiplier(score: Decimal, settings: Settings) -> Decimal:
    """The factor a wallet's category score gets multiplied by before it
    reaches consensus weighting (app/signals/generator.py's
    _build_groups) - 1.0 (no adjustment) below the threshold,
    (1 - score) above it in "downweight" mode (continuous - a score of
    0.9 leaves only 10% of the wallet's normal weight), or 0.0 above it
    in "exclude" mode.
    """
    if score < settings.market_maker_score_threshold:
        return Decimal(1)
    if settings.market_maker_mode == "exclude":
        return Decimal(0)
    return max(Decimal(0), Decimal(1) - score)


# --- DB loader / writer - orchestration only, no logic lives here ---


def load_wallet_events(
    session: Session, wallet_ids: set[int]
) -> dict[int, list[WalletPositionEvent]]:
    """Every non-bootstrap position_history row for these wallets, grouped
    by wallet - is_bootstrap rows are excluded (same reasoning every other
    consumer of this table uses: a wallet's first-ever sweep makes every
    already-held position look like a brand-new OPENED event, which would
    fabricate holding-period and depth signals from backfill, not real
    trading behavior).
    """
    if not wallet_ids:
        return {}
    rows = session.execute(
        select(
            PositionHistory.wallet_id,
            PositionHistory.condition_id,
            PositionHistory.outcome,
            PositionHistory.event_type,
            PositionHistory.size_before,
            PositionHistory.size_after,
            PositionHistory.avg_price,
            PositionHistory.detected_at,
        ).where(PositionHistory.wallet_id.in_(wallet_ids), PositionHistory.is_bootstrap.is_(False))
    ).all()
    by_wallet: dict[int, list[WalletPositionEvent]] = {}
    for row in rows:
        by_wallet.setdefault(row.wallet_id, []).append(
            WalletPositionEvent(
                condition_id=row.condition_id,
                outcome=row.outcome,
                event_type=row.event_type,
                size_before=row.size_before,
                size_after=row.size_after,
                avg_price=row.avg_price,
                detected_at=row.detected_at,
            )
        )
    return by_wallet


def run_market_maker_scoring(settings: Settings) -> int:
    """The full pipeline: load every tracked wallet's position history,
    derive per-wallet features, compute the population median holding
    period (the "far below the population median" comparison the
    holding-period signal needs - see the module docstring), score every
    wallet, write one row per wallet.
    """
    with db_session() as session:
        wallet_ids = set(
            session.execute(select(Wallet.id).where(Wallet.is_tracked.is_(True))).scalars()
        )
        events_by_wallet = load_wallet_events(session, wallet_ids)

    features_by_wallet: dict[int, WalletFeatures] = {}
    for wallet_id, events in events_by_wallet.items():
        features = compute_wallet_features(wallet_id, events)
        if features is not None:
            features_by_wallet[wallet_id] = features

    holding_samples = [
        f.median_holding_hours
        for f in features_by_wallet.values()
        if f.median_holding_hours is not None
    ]
    population_median_holding_hours = _median(holding_samples) if holding_samples else None

    now = datetime.now(UTC)
    rows = []
    for wallet_id in features_by_wallet:
        result = score_wallet(
            wallet_id,
            events_by_wallet[wallet_id],
            population_median_holding_hours,
            settings.market_maker_breadth_reference,
            settings.market_maker_depth_reference_usd,
        )
        if result is None:
            continue
        rows.append(
            WalletMarketMakerScore(
                wallet_id=result.wallet_id,
                holding_period_component=result.holding_period_component,
                both_sides_component=result.both_sides_component,
                breadth_depth_component=result.breadth_depth_component,
                score=result.score,
                computed_at=now,
            )
        )

    if rows:
        with db_session() as session:
            session.add_all(rows)

    logger.info(
        "market_maker: scored %d wallets (population median holding %s hours)",
        len(rows),
        f"{population_median_holding_hours:.1f}"
        if population_median_holding_hours is not None
        else "n/a",
    )
    return len(rows)


def latest_market_maker_scores(session: Session, wallet_ids: set[int]) -> dict[int, Decimal]:
    """Each wallet's most recent market_maker_score - the lookup
    app/signals/generator.py needs, same "latest row per key via
    row_number()" pattern app/optimization/scoring_category.py's
    latest_category_scores already establishes.
    """
    if not wallet_ids:
        return {}
    ranked = (
        select(
            WalletMarketMakerScore.wallet_id,
            WalletMarketMakerScore.score,
            func.row_number()
            .over(
                partition_by=WalletMarketMakerScore.wallet_id,
                order_by=WalletMarketMakerScore.computed_at.desc(),
            )
            .label("rn"),
        )
        .where(WalletMarketMakerScore.wallet_id.in_(wallet_ids))
        .subquery()
    )
    rows = session.execute(select(ranked.c.wallet_id, ranked.c.score).where(ranked.c.rn == 1)).all()
    return dict(rows)


def latest_market_maker_multipliers(
    session: Session, wallet_ids: set[int], settings: Settings
) -> dict[int, Decimal]:
    """Each wallet's consensus-weight multiplier, resolved from its latest
    market_maker_score - a wallet never scored yet gets 1.0 (no
    adjustment), same "no evidence, no penalty" default every other
    optional signal in this project uses.
    """
    scores = latest_market_maker_scores(session, wallet_ids)
    return {
        wallet_id: consensus_weight_multiplier(score, settings)
        for wallet_id, score in scores.items()
    }


async def run_market_maker_scoring_job() -> None:
    """Scheduler entrypoint - same asyncio.to_thread + get_effective_settings
    pattern every other Phase 6 job uses.
    """
    settings = get_effective_settings()
    count = await asyncio.to_thread(run_market_maker_scoring, settings)
    if count:
        logger.info("market_maker: wrote %d wallet scores", count)
