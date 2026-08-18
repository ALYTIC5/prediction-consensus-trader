"""Coherence arbitrage scan orchestration - DB in, detector.py's pure
functions, DB out, capture into the "coherence" paper portfolio. See
docs/COHERENCE_DESIGN.md.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.coherence.detector import (
    LegQuote,
    RawViolation,
    common_fillable_size,
    detect_nested_logic_violation,
    detect_outcome_set_violation,
    walk_book_for_shares,
)
from app.collectors.polymarket import PolymarketClient
from app.config.settings import Settings
from app.db.models import (
    CoherenceFill,
    CoherenceFillStatus,
    CoherenceOpportunity,
    Market,
    PaperPortfolio,
    PriceSnapshot,
)
from app.db.session import db_session
from app.paper.fees import compute_taker_fee
from app.paper.fills import BookLevel
from app.paper.resolution import ResolutionOutcome, detect_resolution

logger = logging.getLogger(__name__)

# Reserved portfolio name - see docs/COHERENCE_DESIGN.md's data model
# section on why this portfolio stays is_active=false (the normal
# signal-driven paper engine cycle already skips inactive portfolios via
# its existing filter, so coherence trades never collide with it).
COHERENCE_PORTFOLIO_NAME = "coherence"

_ACTIONABLE_TYPES = {"YES_NO_SUM_ASK", "MULTI_OUTCOME_ASK"}


@dataclass(frozen=True)
class CandidateMarket:
    condition_id: str
    event_slug: str | None
    outcomes: list[str]
    clob_token_ids: list[str]
    question: str


def load_candidate_markets(session: Session, max_markets: int) -> list[CandidateMarket]:
    """Highest-liquidity not-yet-closed markets with at least 2 outcome
    tokens - see docs/COHERENCE_DESIGN.md for why liquidity-ranked and why
    this scans independently of anything we already hold.
    """
    rows = (
        session.execute(
            select(Market)
            .where(Market.closed.is_(False))
            .order_by(Market.liquidity.desc().nulls_last())
            .limit(max_markets)
        )
        .scalars()
        .all()
    )
    return [
        CandidateMarket(
            condition_id=m.condition_id,
            event_slug=m.event_slug,
            outcomes=m.outcomes,
            clob_token_ids=m.clob_token_ids,
            question=m.question,
        )
        for m in rows
        if len(m.clob_token_ids) >= 2
    ]


def get_or_create_coherence_portfolio(session: Session, settings: Settings) -> PaperPortfolio:
    """Idempotent bootstrap - same "found or create, never silently
    reconfigure an existing one" convention as scripts/seed_portfolios.py.
    is_active=False deliberately: this portfolio's trades come from
    app/coherence/scan.py directly, never from the signal-driven paper
    engine cycle.
    """
    portfolio = session.execute(
        select(PaperPortfolio).where(PaperPortfolio.name == COHERENCE_PORTFOLIO_NAME)
    ).scalar_one_or_none()
    if portfolio is not None:
        return portfolio
    portfolio = PaperPortfolio(
        name=COHERENCE_PORTFOLIO_NAME,
        params={},
        starting_bankroll=settings.coherence_starting_bankroll,
        current_bankroll=settings.coherence_starting_bankroll,
        is_active=False,
    )
    session.add(portfolio)
    session.flush()
    return portfolio


async def _fetch_leg(
    client: PolymarketClient, condition_id: str, asset: str, outcome: str, default_rate
) -> LegQuote | None:
    book = await client.get_book(asset)
    if book is None:
        return None
    rate = await client.get_fee_rate(asset)
    if rate is None:
        rate = default_rate
    ask_levels = [BookLevel(price=lvl.price, size=lvl.size) for lvl in book.asks]
    best_ask = book.asks[0].price if book.asks else None
    best_bid = book.bids[0].price if book.bids else None
    return LegQuote(
        condition_id=condition_id,
        asset=asset,
        outcome=outcome,
        ask_levels=ask_levels,
        best_ask=best_ask,
        best_bid=best_bid,
        fee_rate=rate,
    )


def _is_primary_leg(question: str) -> bool:
    """See docs/COHERENCE_DESIGN.md's scan 3 for why this keyword
    heuristic is explicitly weak and why its output is detect-only.
    """
    lowered = question.lower()
    return "primary" in lowered or "nomination" in lowered


async def _detect_violations(
    client: PolymarketClient, candidates: list[CandidateMarket], settings: Settings
) -> list[RawViolation]:
    legs_by_market: dict[str, list[LegQuote]] = {}
    for market in candidates:
        legs: list[LegQuote] = []
        for asset, outcome in zip(market.clob_token_ids, market.outcomes or [], strict=False):
            leg = await _fetch_leg(
                client, market.condition_id, asset, outcome, settings.coherence_fee_rate_default
            )
            if leg is not None:
                legs.append(leg)
        if len(legs) >= 2:
            legs_by_market[market.condition_id] = legs

    violations: list[RawViolation] = []

    # Scans 1 (binary, 2 legs) and 2-within-market (N>2 legs, one market's
    # own outcome tokens).
    for legs in legs_by_market.values():
        bid_type = "YES_NO_SUM_BID" if len(legs) == 2 else "MULTI_OUTCOME_BID"
        ask_type = "YES_NO_SUM_ASK" if len(legs) == 2 else "MULTI_OUTCOME_ASK"
        violations.extend(
            detect_outcome_set_violation(
                legs,
                settings.coherence_min_edge,
                settings.coherence_max_book_depth_fraction,
                bid_type=bid_type,
                ask_type=ask_type,
            )
        )

    # Scan 2-across-market: same event_slug, sibling binary markets - each
    # market's first fetched leg (its "Yes"-equivalent token) represents
    # it in the group.
    by_event: dict[str, list[LegQuote]] = {}
    for market in candidates:
        if market.event_slug is None or market.condition_id not in legs_by_market:
            continue
        by_event.setdefault(market.event_slug, []).append(legs_by_market[market.condition_id][0])
    for legs in by_event.values():
        if len(legs) < 2:
            continue
        violations.extend(
            detect_outcome_set_violation(
                legs,
                settings.coherence_min_edge,
                settings.coherence_max_book_depth_fraction,
                bid_type="MULTI_OUTCOME_BID",
                ask_type="MULTI_OUTCOME_ASK",
            )
        )

    # Scan 3: nested/conditional, same event_slug, keyword-heuristic pairing.
    by_event_markets: dict[str, list[CandidateMarket]] = {}
    for market in candidates:
        if market.event_slug is not None:
            by_event_markets.setdefault(market.event_slug, []).append(market)
    for group in by_event_markets.values():
        primaries = [m for m in group if _is_primary_leg(m.question)]
        generals = [m for m in group if not _is_primary_leg(m.question)]
        for primary in primaries:
            if primary.condition_id not in legs_by_market:
                continue
            for general in generals:
                if general.condition_id not in legs_by_market:
                    continue
                violation = detect_nested_logic_violation(
                    legs_by_market[general.condition_id][0],
                    legs_by_market[primary.condition_id][0],
                    settings.coherence_min_edge,
                )
                if violation is not None:
                    violations.append(violation)

    return violations


def _upsert_opportunities(session: Session, violations: list[RawViolation], now: datetime) -> None:
    """One row per opportunity_key, not one per scan - see
    docs/COHERENCE_DESIGN.md's data model. A violation seen again refreshes
    last_seen_at; one no longer seen this cycle gets resolved_at set once
    (never overwritten once set, so duration reads the first time it
    vanished, not the last time this function happened to run).
    """
    seen_keys = {v.opportunity_key for v in violations}
    existing = {
        row.opportunity_key: row
        for row in session.execute(
            select(CoherenceOpportunity).where(CoherenceOpportunity.resolved_at.is_(None))
        ).scalars()
    }

    for violation in violations:
        row = existing.get(violation.opportunity_key)
        if row is not None:
            row.last_seen_at = now
            row.gross_spread = violation.gross_spread
            row.size = violation.size
            row.net_profit = violation.net_profit
            row.required_capital = violation.required_capital
            continue
        session.add(
            CoherenceOpportunity(
                opportunity_key=violation.opportunity_key,
                type=violation.type,
                legs=violation.legs,
                gross_spread=violation.gross_spread,
                size=violation.size,
                net_profit=violation.net_profit,
                required_capital=violation.required_capital,
                detected_at=now,
                last_seen_at=now,
                captured=False,
            )
        )

    for key, row in existing.items():
        if key not in seen_keys:
            row.resolved_at = now


@dataclass(frozen=True)
class PricedCapture:
    """One actionable violation, fully re-priced against the CURRENT book
    (never the one detection used a moment ago - see
    docs/COHERENCE_DESIGN.md's "known limitations" on the detection/fill
    race) - pure network + arithmetic, no DB access, so it can run freely
    alongside every other async call before a single blocking DB write
    happens (see run_coherence_scan).
    """

    opportunity_key: str
    fills: list[tuple[LegQuote, Decimal, Decimal]]  # (leg, avg_price, fee)
    size: Decimal
    total_cost: Decimal


async def _price_capture(
    client: PolymarketClient, violation: RawViolation, settings: Settings
) -> PricedCapture | None:
    """Re-walk every leg fresh and return a fully priced, ready-to-write
    capture, or None if it's no longer fillable/profitable right now.
    Never touches the database - see PricedCapture's docstring.
    """
    if violation.net_profit is None or violation.net_profit <= 0 or violation.size is None:
        return None

    fresh_legs: list[LegQuote] = []
    for leg in violation.legs:
        fresh = await _fetch_leg(
            client,
            leg["condition_id"],
            leg["asset"],
            leg["outcome"],
            settings.coherence_fee_rate_default,
        )
        if fresh is None:
            return None
        fresh_legs.append(fresh)

    size = common_fillable_size(fresh_legs, settings.coherence_max_book_depth_fraction)
    if size is None or size <= 0:
        return None

    fills: list[tuple[LegQuote, Decimal, Decimal]] = []
    total_cost = Decimal(0)
    for leg in fresh_legs:
        avg_price = walk_book_for_shares(
            leg.ask_levels, size, settings.coherence_max_book_depth_fraction
        )
        if avg_price is None:
            return None
        fee = compute_taker_fee(avg_price, size, leg.fee_rate)
        fills.append((leg, avg_price, fee))
        total_cost += size * avg_price + fee

    if total_cost > settings.coherence_max_position_notional:
        return None
    if total_cost - size >= 0:
        return None  # fresh numbers no longer show a real profit

    return PricedCapture(
        opportunity_key=violation.opportunity_key, fills=fills, size=size, total_cost=total_cost
    )


def _write_captures(
    session: Session, settings: Settings, priced: list[PricedCapture], now: datetime
) -> int:
    """The only DB-writing half of capture: given already-priced captures
    (no network calls left to make), open the coherence portfolio once,
    check each one against the running bankroll in order, and write its
    fills. Blocking, called via asyncio.to_thread from run_coherence_scan -
    never called directly from async code.
    """
    portfolio = get_or_create_coherence_portfolio(session, settings)
    written = 0
    for capture in priced:
        if capture.total_cost > portfolio.current_bankroll:
            continue
        opportunity = session.execute(
            select(CoherenceOpportunity).where(
                CoherenceOpportunity.opportunity_key == capture.opportunity_key
            )
        ).scalar_one_or_none()
        if opportunity is None:
            continue
        for leg, avg_price, fee in capture.fills:
            session.add(
                CoherenceFill(
                    opportunity_id=opportunity.id,
                    portfolio_id=portfolio.id,
                    condition_id=leg.condition_id,
                    asset=leg.asset,
                    outcome=leg.outcome,
                    status=CoherenceFillStatus.OPEN,
                    entry_price=avg_price,
                    size=capture.size,
                    fee_paid=fee,
                    entry_at=now,
                )
            )
        portfolio.current_bankroll -= capture.total_cost
        opportunity.captured = True
        written += 1
    return written


def _resolve_open_fills(session: Session, settings: Settings, now: datetime) -> int:
    """Close every OPEN coherence_fills row whose market has resolved -
    see docs/COHERENCE_DESIGN.md's "Why holding to resolution is the exit"
    section: this is the ONLY exit path a coherence fill ever takes, never
    take-profit/stop-loss/expiry, and redemption is fee-free (no fee
    charged here - see app/paper/fees.py's docstring). AMBIGUOUS
    resolutions are left open to retry next cycle, same as
    app/paper/engine.py's own _run_exits.
    """
    open_fills = (
        session.execute(
            select(CoherenceFill).where(CoherenceFill.status == CoherenceFillStatus.OPEN)
        )
        .scalars()
        .all()
    )
    resolved = 0
    for fill in open_fills:
        market = session.execute(
            select(Market).where(Market.condition_id == fill.condition_id)
        ).scalar_one_or_none()
        closed = market.closed if market is not None else False
        latest_price = (
            session.execute(
                select(PriceSnapshot.price)
                .where(
                    PriceSnapshot.condition_id == fill.condition_id,
                    PriceSnapshot.asset == fill.asset,
                )
                .order_by(PriceSnapshot.captured_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            if closed
            else None
        )
        outcome = detect_resolution(closed, latest_price, settings.paper_resolution_price_threshold)
        if outcome not in (ResolutionOutcome.WON, ResolutionOutcome.LOST):
            continue
        exit_price = Decimal("1.0") if outcome == ResolutionOutcome.WON else Decimal("0.0")
        fill.exit_price = exit_price
        fill.realized_pnl = (exit_price - fill.entry_price) * fill.size - fill.fee_paid
        fill.exit_at = now
        fill.status = CoherenceFillStatus.CLOSED
        portfolio = session.get(PaperPortfolio, fill.portfolio_id)
        portfolio.current_bankroll += fill.size * exit_price
        resolved += 1
    return resolved


def _resolve_open_fills_blocking(settings: Settings, now: datetime) -> int:
    with db_session() as session:
        return _resolve_open_fills(session, settings, now)


def _load_candidates_blocking(max_markets: int) -> list[CandidateMarket]:
    with db_session() as session:
        return load_candidate_markets(session, max_markets)


def _upsert_opportunities_blocking(violations: list[RawViolation], now: datetime) -> None:
    with db_session() as session:
        _upsert_opportunities(session, violations, now)


def _write_captures_blocking(settings: Settings, priced: list[PricedCapture], now: datetime) -> int:
    with db_session() as session:
        return _write_captures(session, settings, priced, now)


async def run_coherence_scan(client: PolymarketClient, settings: Settings) -> None:
    """One full scan cycle: load candidates, detect violations (network),
    persist them, re-price and capture whatever's actionable (network),
    write captures (DB). Every blocking DB call goes through
    asyncio.to_thread, same convention as every collector in this project.
    """
    now = datetime.now(UTC)
    resolved = await asyncio.to_thread(_resolve_open_fills_blocking, settings, now)

    candidates = await asyncio.to_thread(
        _load_candidates_blocking, settings.coherence_max_markets_per_cycle
    )

    violations = await _detect_violations(client, candidates, settings)

    await asyncio.to_thread(_upsert_opportunities_blocking, violations, now)

    priced_captures: list[PricedCapture] = []
    for violation in violations:
        if violation.type not in _ACTIONABLE_TYPES:
            continue
        priced = await _price_capture(client, violation, settings)
        if priced is not None:
            priced_captures.append(priced)

    captured = await asyncio.to_thread(_write_captures_blocking, settings, priced_captures, now)

    logger.info(
        "coherence: candidates=%d violations=%d captured=%d resolved=%d",
        len(candidates),
        len(violations),
        captured,
        resolved,
    )
