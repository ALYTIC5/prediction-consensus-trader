"""Consensus V2 orchestration: whale-implied probability scan, divergence
signal, and paper-trading into the consensus_prob portfolio. See
docs/CONSENSUS_V2_DESIGN.md.

P_market and the entry fill both come from a LIVE book fetch at scan time
(not a possibly-stale order_books snapshot) - this project's coherence
scanner (app/coherence/scan.py) established the same pattern for the same
reason: neither module holds a position in most of the markets it looks
at, so it can't lean on app/collectors/orderbook.py's narrow "markets we
already hold" snapshot scope. Since the book is always freshly fetched,
market-price staleness isn't the freshness risk here - Market's own
metadata (liquidity/spread, from the periodic markets collector) is, so
that's what the FRESHNESS filter actually checks.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collectors.polymarket import PolymarketClient
from app.config.settings import Settings
from app.consensus_v2.paired_brier import ResolvedRecord, compute_paired_brier, kelly_gate_passes
from app.consensus_v2.probability import ClusterPosition, compute_consensus_probability
from app.db.models import (
    Market,
    PaperPortfolio,
    Position,
    PriceSnapshot,
    SignalProb,
    SignalProbTrade,
    SignalProbTradeStatus,
    WalletCluster,
)
from app.db.session import db_session
from app.optimization.scoring_category import latest_category_scores
from app.paper.fees import compute_taker_fee
from app.paper.fills import BookLevel, walk_the_book
from app.paper.resolution import ResolutionOutcome, detect_resolution
from app.risk.sizing_kelly import KellySizingConfig, KellySizingRequest, size_kelly_position

logger = logging.getLogger(__name__)

CONSENSUS_V2_PORTFOLIO_NAME = "consensus_prob"


@dataclass(frozen=True)
class BinaryMarketCandidate:
    condition_id: str
    category: str
    liquidity: Decimal | None
    spread: Decimal | None
    end_date: datetime | None
    last_synced_at: datetime
    event_slug: str | None
    yes_asset: str
    yes_outcome: str
    no_asset: str
    no_outcome: str


def load_candidate_binary_markets(
    session: Session, max_markets: int
) -> list[BinaryMarketCandidate]:
    """Highest-liquidity not-yet-closed markets with EXACTLY 2 outcome
    tokens - see docs/CONSENSUS_V2_DESIGN.md's "Scope: binary markets
    only" for why N-outcome markets are skipped entirely rather than
    approximated.
    """
    rows = (
        session.execute(
            select(Market)
            .where(Market.closed.is_(False))
            .order_by(Market.liquidity.desc().nulls_last())
            .limit(max_markets * 3)  # over-fetch, then filter to exactly-2-outcome in Python
        )
        .scalars()
        .all()
    )
    candidates: list[BinaryMarketCandidate] = []
    for m in rows:
        if len(m.clob_token_ids) != 2 or len(m.outcomes) != 2:
            continue
        candidates.append(
            BinaryMarketCandidate(
                condition_id=m.condition_id,
                category=m.category,
                liquidity=m.liquidity,
                spread=m.spread,
                end_date=m.end_date,
                last_synced_at=m.last_synced_at,
                event_slug=m.event_slug,
                yes_asset=m.clob_token_ids[0],
                yes_outcome=m.outcomes[0],
                no_asset=m.clob_token_ids[1],
                no_outcome=m.outcomes[1],
            )
        )
        if len(candidates) >= max_markets:
            break
    return candidates


def _wallet_cluster_map(session: Session, wallet_ids: set[int]) -> dict[int, str]:
    """wallet_id -> cluster_id, falling back to a per-wallet singleton for
    a wallet with no wallet_clusters row - same "no evidence of
    correlation defaults to independent" convention
    app/optimization/clustering.py's own isolates use.
    """
    if not wallet_ids:
        return {}
    rows = session.execute(
        select(WalletCluster.wallet_id, WalletCluster.cluster_id).where(
            WalletCluster.wallet_id.in_(wallet_ids)
        )
    ).all()
    clustered = dict(rows)
    return {wid: clustered.get(wid, f"solo:{wid}") for wid in wallet_ids}


def load_cluster_positions(
    session: Session, market: BinaryMarketCandidate
) -> list[ClusterPosition]:
    """Every OPEN position in this market's two outcome tokens, netted and
    aggregated per cluster (docs/CONSENSUS_V2_DESIGN.md section 1:
    "Aggregation unit is the cluster, not the wallet") - a cluster holding
    both YES and NO nets to its actual directional exposure, and a
    perfectly hedged cluster (net zero) contributes no opinion at all.
    score_i is the cluster's MAX member TraderCategoryScore for this
    market's category (the flagged, confirmed decision).
    """
    rows = session.execute(
        select(Position.wallet_id, Position.asset, Position.size, Position.cur_price).where(
            Position.condition_id == market.condition_id,
            Position.is_open.is_(True),
            Position.asset.in_([market.yes_asset, market.no_asset]),
        )
    ).all()
    if not rows:
        return []

    wallet_ids = {r.wallet_id for r in rows}
    cluster_of = _wallet_cluster_map(session, wallet_ids)
    scores = latest_category_scores(session, wallet_ids)

    yes_usd: dict[str, Decimal] = {}
    no_usd: dict[str, Decimal] = {}
    score_by_cluster: dict[str, Decimal] = {}
    for row in rows:
        cluster_id = cluster_of[row.wallet_id]
        usd = row.size * row.cur_price
        bucket = yes_usd if row.asset == market.yes_asset else no_usd
        bucket[cluster_id] = bucket.get(cluster_id, Decimal(0)) + usd
        wallet_score = scores.get((row.wallet_id, market.category), Decimal(0))
        score_by_cluster[cluster_id] = max(
            score_by_cluster.get(cluster_id, Decimal(0)), wallet_score
        )

    positions: list[ClusterPosition] = []
    for cluster_id in set(yes_usd) | set(no_usd):
        net = yes_usd.get(cluster_id, Decimal(0)) - no_usd.get(cluster_id, Decimal(0))
        if net == 0:
            continue
        positions.append(
            ClusterPosition(
                cluster_id=cluster_id,
                position_usd=abs(net),
                sign=1 if net > 0 else -1,
                score=score_by_cluster.get(cluster_id, Decimal(0)),
            )
        )
    return positions


def _mid_price(book) -> Decimal | None:
    if not book.bids or not book.asks:
        return None
    return (book.bids[0].price + book.asks[0].price) / Decimal(2)


async def _scan_one_market(
    client: PolymarketClient, market: BinaryMarketCandidate, settings: Settings, now: datetime
) -> tuple[SignalProb, list[BookLevel]] | None:
    """None if this market doesn't clear every filter. Returns the
    not-yet-persisted SignalProb row plus the live ask-side book (so the
    caller can enter a fill from the exact same book this signal was
    computed against, no second fetch).
    """
    if market.liquidity is None or market.liquidity < settings.signal_min_liquidity_usd:
        return None
    if market.spread is None or market.spread > settings.signal_max_spread:
        return None
    if now - market.last_synced_at > timedelta(
        seconds=settings.consensus_prob_max_snapshot_age_seconds
    ):
        return None

    with_positions = await asyncio.to_thread(_load_positions_blocking, market)
    if not with_positions:
        return None

    result = compute_consensus_probability(
        with_positions,
        settings.consensus_prob_max_cluster_weight_fraction,
        settings.consensus_prob_confidence_n_clusters_target,
        settings.consensus_prob_confidence_notional_target,
    )
    if result is None:
        return None

    book = await client.get_book(market.yes_asset)
    if book is None:
        return None
    p_market = _mid_price(book)
    if p_market is None:
        return None

    divergence = result.p_consensus - p_market
    if abs(divergence) < settings.consensus_prob_min_divergence:
        return None
    if result.confidence < settings.consensus_prob_min_confidence:
        return None

    signal = SignalProb(
        condition_id=market.condition_id,
        asset=market.yes_asset,
        outcome=market.yes_outcome,
        event_slug=market.event_slug,
        p_consensus=result.p_consensus,
        p_market=p_market,
        divergence=divergence,
        confidence=result.confidence,
        n_clusters=result.n_clusters,
        total_weight_usd=result.total_weight_usd,
        liquidity=market.liquidity,
        spread=market.spread,
        created_at=now,
    )
    ask_levels = [BookLevel(price=lvl.price, size=lvl.size) for lvl in book.asks]
    return signal, ask_levels


def _load_positions_blocking(market: BinaryMarketCandidate) -> list[ClusterPosition]:
    with db_session() as session:
        return load_cluster_positions(session, market)


def get_or_create_consensus_v2_portfolio(session: Session, settings: Settings) -> PaperPortfolio:
    """Idempotent bootstrap - same convention as
    app/coherence/scan.py's get_or_create_coherence_portfolio.
    is_active=False: this portfolio's trades come from this module
    directly, never from the count-based signal-driven paper engine cycle
    (docs/CONSENSUS_V2_DESIGN.md's flagged decision #1 - this is a
    parallel pipeline).
    """
    portfolio = session.execute(
        select(PaperPortfolio).where(PaperPortfolio.name == CONSENSUS_V2_PORTFOLIO_NAME)
    ).scalar_one_or_none()
    if portfolio is not None:
        return portfolio
    portfolio = PaperPortfolio(
        name=CONSENSUS_V2_PORTFOLIO_NAME,
        params={},
        starting_bankroll=settings.consensus_prob_starting_bankroll,
        current_bankroll=settings.consensus_prob_starting_bankroll,
        is_active=False,
    )
    session.add(portfolio)
    session.flush()
    return portfolio


def _has_open_trade(session: Session, portfolio_id: int, condition_id: str) -> bool:
    """Dedup gate: a state-driven scan would otherwise re-signal (and
    re-enter) the same still-diverging market every single cycle - unlike
    the count-based engine, which is naturally rate-limited by actual new
    trading events. One open position per market at a time.
    """
    return (
        session.execute(
            select(SignalProbTrade.id).where(
                SignalProbTrade.portfolio_id == portfolio_id,
                SignalProbTrade.condition_id == condition_id,
                SignalProbTrade.status == SignalProbTradeStatus.OPEN,
            )
        ).first()
        is not None
    )


def _current_paired_brier_gate(session: Session, settings: Settings) -> bool:
    rows = session.execute(
        select(
            SignalProb.condition_id,
            Market.event_cluster_id,
            SignalProb.p_consensus,
            SignalProb.p_market,
            SignalProb.outcome_value,
        )
        .join(Market, Market.condition_id == SignalProb.condition_id, isouter=True)
        .where(SignalProb.resolved_at.is_not(None))
    ).all()
    records = [
        ResolvedRecord(
            condition_id=r.condition_id,
            event_cluster_id=r.event_cluster_id,
            p_consensus=r.p_consensus,
            p_market=r.p_market,
            outcome=r.outcome_value,
        )
        for r in rows
    ]
    result = compute_paired_brier(records)
    return kelly_gate_passes(
        result,
        settings.consensus_prob_kelly_min_effective_n,
        settings.consensus_prob_kelly_min_t_stat,
    )


def _enter_signal_blocking(
    settings: Settings, signal: SignalProb, ask_levels: list[BookLevel], now: datetime
) -> bool:
    with db_session() as session:
        portfolio = get_or_create_consensus_v2_portfolio(session, settings)
        if _has_open_trade(session, portfolio.id, signal.condition_id):
            return False

        session.add(signal)
        session.flush()

        # Only the actionable direction (divergence positive - consensus
        # thinks YES is more likely than the market does) is entered as a
        # long YES position; a negative divergence would require shorting
        # YES (buying NO instead) - supported the same way, just on the
        # complementary side. Kept to the YES leg only in this pass for
        # simplicity; buying NO on a negative divergence is a natural,
        # low-risk extension once this simpler path is validated.
        if signal.divergence <= 0:
            return False

        # Kelly gate (docs/CONSENSUS_V2_DESIGN.md section 3): P_consensus
        # only replaces Kelly's usual empirical calibration-bucket p_hat
        # once the paired Brier result proves it's worth trusting - until
        # then, every consensus_prob signal sizes on fixed fraction, same
        # "prove it before trusting it" shape every other portfolio's
        # Kelly fallback already follows.
        reference_price = ask_levels[0].price if ask_levels else None
        target_notional: Decimal | None = None
        if reference_price is not None and _current_paired_brier_gate(session, settings):
            kelly_result = size_kelly_position(
                KellySizingRequest(
                    current_bankroll=portfolio.current_bankroll,
                    p_hat=signal.p_consensus,
                    fill_price=reference_price,
                ),
                KellySizingConfig(
                    kelly_fraction=settings.risk_kelly_fraction,
                    fee_pct=settings.consensus_prob_fee_rate_default,
                    max_position_pct=settings.risk_max_position_pct,
                ),
            )
            target_notional = kelly_result.target_notional
        if target_notional is None:
            target_notional = (
                portfolio.current_bankroll * settings.consensus_prob_fixed_fraction_pct
            )
        if target_notional <= 0:
            return False
        avg_price = walk_the_book(
            ask_levels, target_notional, settings.paper_max_book_depth_fraction
        )
        if avg_price is None or avg_price <= 0:
            return False
        size = target_notional / avg_price
        if size <= 0:
            return False

        fee_rate = settings.consensus_prob_fee_rate_default
        fee = compute_taker_fee(avg_price, size, fee_rate)
        cost = size * avg_price + fee
        if cost > portfolio.current_bankroll:
            return False

        session.add(
            SignalProbTrade(
                portfolio_id=portfolio.id,
                signal_prob_id=signal.id,
                condition_id=signal.condition_id,
                asset=signal.asset,
                outcome=signal.outcome,
                status=SignalProbTradeStatus.OPEN,
                entry_price=avg_price,
                size=size,
                fee_paid=fee,
                entry_at=now,
            )
        )
        portfolio.current_bankroll -= cost
        return True


def _resolve_open_trades_blocking(settings: Settings, now: datetime) -> int:
    with db_session() as session:
        open_trades = (
            session.execute(
                select(SignalProbTrade).where(SignalProbTrade.status == SignalProbTradeStatus.OPEN)
            )
            .scalars()
            .all()
        )
        resolved = 0
        for trade in open_trades:
            market = session.execute(
                select(Market).where(Market.condition_id == trade.condition_id)
            ).scalar_one_or_none()
            closed = market.closed if market is not None else False
            latest_price = (
                session.execute(
                    select(PriceSnapshot.price)
                    .where(
                        PriceSnapshot.condition_id == trade.condition_id,
                        PriceSnapshot.asset == trade.asset,
                    )
                    .order_by(PriceSnapshot.captured_at.desc())
                    .limit(1)
                ).scalar_one_or_none()
                if closed
                else None
            )
            outcome = detect_resolution(
                closed, latest_price, settings.paper_resolution_price_threshold
            )

            exit_price: Decimal | None = None
            exit_reason: str | None = None
            if outcome in (ResolutionOutcome.WON, ResolutionOutcome.LOST):
                exit_price = Decimal("1.0") if outcome == ResolutionOutcome.WON else Decimal("0.0")
                exit_reason = "market_resolved"
            elif trade.current_price is not None:
                if trade.current_price >= trade.entry_price * (1 + settings.paper_take_profit_pct):
                    exit_price, exit_reason = trade.current_price, "take_profit"
                elif trade.current_price <= trade.entry_price * (1 - settings.paper_stop_loss_pct):
                    exit_price, exit_reason = trade.current_price, "stop_loss"
            if exit_price is None and now - trade.entry_at > timedelta(
                hours=settings.paper_exit_on_signal_expiry_hours
            ):
                exit_price = (
                    trade.current_price if trade.current_price is not None else trade.entry_price
                )
                exit_reason = "signal_expired"

            if exit_price is None:
                continue

            exit_fee = (
                Decimal(0)
                if exit_reason == "market_resolved"
                else compute_taker_fee(
                    exit_price, trade.size, settings.consensus_prob_fee_rate_default
                )
            )
            trade.exit_price = exit_price
            trade.realized_pnl = (
                (exit_price - trade.entry_price) * trade.size - trade.fee_paid - exit_fee
            )
            trade.exit_reason = exit_reason
            trade.exit_at = now
            trade.status = SignalProbTradeStatus.CLOSED
            portfolio = session.get(PaperPortfolio, trade.portfolio_id)
            portfolio.current_bankroll += trade.size * exit_price - exit_fee
            resolved += 1

            if exit_reason == "market_resolved":
                signal = session.get(SignalProb, trade.signal_prob_id)
                if signal is not None and signal.resolved_at is None:
                    outcome_value = exit_price  # already 1.0 or 0.0
                    signal.outcome_value = outcome_value
                    signal.brier_consensus = (signal.p_consensus - outcome_value) ** 2
                    signal.brier_market = (signal.p_market - outcome_value) ** 2
                    signal.paired_diff = signal.brier_market - signal.brier_consensus
                    signal.resolved_at = now
        return resolved


async def run_consensus_v2_scan(client: PolymarketClient, settings: Settings) -> None:
    now = datetime.now(UTC)
    resolved = await asyncio.to_thread(_resolve_open_trades_blocking, settings, now)

    candidates = await asyncio.to_thread(
        _load_candidates_blocking, settings.consensus_prob_max_markets_per_cycle
    )

    scanned = 0
    signaled = 0
    entered = 0
    for market in candidates:
        scanned += 1
        outcome = await _scan_one_market(client, market, settings, now)
        if outcome is None:
            continue
        signal, ask_levels = outcome
        signaled += 1
        did_enter = await asyncio.to_thread(
            _enter_signal_blocking, settings, signal, ask_levels, now
        )
        if did_enter:
            entered += 1

    logger.info(
        "consensus_v2: scanned=%d signaled=%d entered=%d resolved=%d",
        scanned,
        signaled,
        entered,
        resolved,
    )


def _load_candidates_blocking(max_markets: int) -> list[BinaryMarketCandidate]:
    with db_session() as session:
        return load_candidate_binary_markets(session, max_markets)
