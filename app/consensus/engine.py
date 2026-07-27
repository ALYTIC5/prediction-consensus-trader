"""Consensus engine - pure logic, no DB, no network.

See docs/PHASE3_DESIGN.md section 3. This module never queries
position_history itself and never sees is_bootstrap - a caller elsewhere
(app/signals/generator.py) is responsible for building CandidateGroup
objects only from fresh, non-bootstrap OPENED/INCREASED events. The engine
assumes that filtering has already happened and treats every event handed
to it as real, current activity worth evaluating.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from app.config.settings import Settings


class RejectionReason(StrEnum):
    """The eight filters evaluate_group runs, in order. A group is rejected
    on the first one it fails.
    """

    MARKET_KNOWN = "market_known"
    MARKET_OPEN = "market_open"
    LIQUIDITY = "liquidity"
    PRICE_BAND = "price_band"
    SPREAD = "spread"
    BREADTH = "breadth"
    QUALITY = "quality"
    CONVICTION = "conviction"


@dataclass(frozen=True)
class ContributorEvent:
    """One wallet's contribution to a candidate group - one OPENED or
    INCREASED position_history row, already resolved to an acted size and
    an entry price by the caller.
    """

    address: str
    username: str | None
    weight: Decimal
    event_type: str
    acted_size: Decimal
    entry_price: Decimal
    detected_at: datetime


@dataclass(frozen=True)
class CandidateGroup:
    """Every fresh, non-bootstrap event for one (condition_id, asset) pair."""

    condition_id: str
    asset: str
    outcome: str
    title: str
    event_slug: str
    events: list[ContributorEvent]


@dataclass(frozen=True)
class MarketState:
    """The market facts evaluate_group needs, as of "now". exists=False
    means no markets row was found for the group's condition_id at all -
    every other field is meaningless in that case.
    """

    exists: bool
    closed: bool
    accepting_orders: bool
    end_date: datetime | None
    liquidity: Decimal | None
    volume_24h: Decimal | None
    spread: Decimal | None
    current_price: Decimal | None


@dataclass(frozen=True)
class ConsensusConfig:
    """Every phase 3 threshold from settings, bundled once so evaluate_group
    never reads app.config.settings directly - a hardcoded threshold in this
    module is a bug (see CLAUDE.md).
    """

    consensus_freshness_hours: int
    consensus_include_increases: bool
    consensus_min_traders: int
    consensus_min_weighted_score: Decimal
    consensus_min_combined_value_usd: Decimal
    signal_min_liquidity_usd: Decimal
    signal_min_volume_24h_usd: Decimal
    signal_price_min: Decimal
    signal_price_max: Decimal
    signal_max_spread: Decimal
    signal_min_hours_to_end: int

    @classmethod
    def from_settings(cls, settings: Settings) -> "ConsensusConfig":
        return cls(
            consensus_freshness_hours=settings.consensus_freshness_hours,
            consensus_include_increases=settings.consensus_include_increases,
            consensus_min_traders=settings.consensus_min_traders,
            consensus_min_weighted_score=settings.consensus_min_weighted_score,
            consensus_min_combined_value_usd=settings.consensus_min_combined_value_usd,
            signal_min_liquidity_usd=settings.signal_min_liquidity_usd,
            signal_min_volume_24h_usd=settings.signal_min_volume_24h_usd,
            signal_price_min=settings.signal_price_min,
            signal_price_max=settings.signal_price_max,
            signal_max_spread=settings.signal_max_spread,
            signal_min_hours_to_end=settings.signal_min_hours_to_end,
        )


@dataclass(frozen=True)
class SignalDraft:
    """A candidate group that cleared every filter - everything a signal
    row needs, including the full contributor list.
    """

    condition_id: str
    asset: str
    outcome: str
    title: str
    event_slug: str
    distinct_traders: int
    weighted_score: Decimal
    combined_entry_value: Decimal
    average_entry_price: Decimal
    latest_market_price: Decimal
    contributors: list[ContributorEvent]


@dataclass(frozen=True)
class Rejection:
    """A candidate group that failed one filter. Carries every metric that
    had already been computed at the point of failure, so a rejection is as
    debuggable as an accepted signal - "why didn't this fire" should never
    require re-deriving the numbers by hand.
    """

    reason: RejectionReason
    distinct_traders: int
    weighted_score: Decimal
    combined_entry_value: Decimal
    average_entry_price: Decimal
    latest_market_price: Decimal | None


def _weighted_score(events: Sequence[ContributorEvent]) -> tuple[int, Decimal]:
    """Distinct trader count and weighted_score.

    Each wallet counts once, at the maximum weight among its own qualifying
    events in this group - a wallet that both opened and later increased
    the same position is one vote of conviction, not two.
    """
    max_weight_by_address: dict[str, Decimal] = {}
    for event in events:
        current = max_weight_by_address.get(event.address)
        if current is None or event.weight > current:
            max_weight_by_address[event.address] = event.weight
    weighted_score = sum(max_weight_by_address.values(), Decimal(0))
    return len(max_weight_by_address), weighted_score


def _combined_entry_value(events: Sequence[ContributorEvent]) -> Decimal:
    """Total dollars moved - every qualifying event counts, even a repeat
    contribution from the same wallet, since that's genuinely more capital
    committed (unlike weighted_score, which dedupes by wallet).
    """
    return sum((event.acted_size * event.entry_price for event in events), Decimal(0))


def _total_acted_size(events: Sequence[ContributorEvent]) -> Decimal:
    return sum((event.acted_size for event in events), Decimal(0))


def _average_entry_price(combined_entry_value: Decimal, total_acted_size: Decimal) -> Decimal:
    """Size-weighted average entry price, not a naive per-event mean - a
    1-share test buy and a 10,000-share position shouldn't count equally.
    """
    if total_acted_size == 0:
        return Decimal(0)
    return combined_entry_value / total_acted_size


def evaluate_group(
    group: CandidateGroup, market: MarketState, config: ConsensusConfig, now: datetime
) -> SignalDraft | Rejection:
    """Run every filter in order, returning the first rejection or a
    SignalDraft if the group clears all eight.
    """
    distinct_traders, weighted_score = _weighted_score(group.events)
    combined_entry_value = _combined_entry_value(group.events)
    average_entry_price = _average_entry_price(
        combined_entry_value, _total_acted_size(group.events)
    )

    def reject(reason: RejectionReason) -> Rejection:
        return Rejection(
            reason=reason,
            distinct_traders=distinct_traders,
            weighted_score=weighted_score,
            combined_entry_value=combined_entry_value,
            average_entry_price=average_entry_price,
            latest_market_price=market.current_price if market.exists else None,
        )

    if not market.exists:
        return reject(RejectionReason.MARKET_KNOWN)

    market_open = (
        not market.closed
        and market.accepting_orders
        and market.end_date is not None
        and market.end_date >= now + timedelta(hours=config.signal_min_hours_to_end)
    )
    if not market_open:
        return reject(RejectionReason.MARKET_OPEN)

    has_liquidity = (
        market.liquidity is not None
        and market.liquidity >= config.signal_min_liquidity_usd
        and market.volume_24h is not None
        and market.volume_24h >= config.signal_min_volume_24h_usd
    )
    if not has_liquidity:
        return reject(RejectionReason.LIQUIDITY)

    in_price_band = (
        market.current_price is not None
        and config.signal_price_min <= market.current_price <= config.signal_price_max
    )
    if not in_price_band:
        return reject(RejectionReason.PRICE_BAND)

    spread_ok = market.spread is not None and market.spread <= config.signal_max_spread
    if not spread_ok:
        return reject(RejectionReason.SPREAD)

    if distinct_traders < config.consensus_min_traders:
        return reject(RejectionReason.BREADTH)

    if weighted_score < config.consensus_min_weighted_score:
        return reject(RejectionReason.QUALITY)

    if combined_entry_value < config.consensus_min_combined_value_usd:
        return reject(RejectionReason.CONVICTION)

    return SignalDraft(
        condition_id=group.condition_id,
        asset=group.asset,
        outcome=group.outcome,
        title=group.title,
        event_slug=group.event_slug,
        distinct_traders=distinct_traders,
        weighted_score=weighted_score,
        combined_entry_value=combined_entry_value,
        average_entry_price=average_entry_price,
        latest_market_price=market.current_price,
        contributors=list(group.events),
    )
