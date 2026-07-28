"""Realistic fill simulation for paper trades - pure, no DB, no network.

See docs/PHASE4_DESIGN.md section 2. This is the module where a paper
trader lies to itself if it's allowed to: a trade that fills at the signal
price, or ignores order size against a thin market, produces numbers that
look better than any real execution ever could. Every assumption here is a
named, tunable FillConfig field - nothing is a bare literal.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from app.config.settings import Settings

# Below this, a market has no real liquidity to speak of - treating it as
# tradeable would mean either dividing by (near) zero or handing back a
# fill price that doesn't reflect any real orderbook. Not a strategy
# threshold (nobody would ever want to "tune" what counts as no market at
# all), so it's a module constant, not a Settings field.
_MIN_LIQUIDITY = Decimal("1")


class FillReason(StrEnum):
    """Why compute_fill returned what it did."""

    FILLED = "FILLED"
    MISSED_DRIFT = "MISSED_DRIFT"
    NO_LIQUIDITY = "NO_LIQUIDITY"
    FALLBACK_FILLED = "FALLBACK_FILLED"


@dataclass(frozen=True)
class MarketSnapshot:
    """Market state at one point in time.

    liquidity is only meaningfully read from whichever snapshot compute_fill
    treats as "current" (the earliest one passed in) - there's no historized
    liquidity series to draw on (the markets table carries one current
    value, not a time series), so later snapshots' liquidity is ignored even
    if a caller sets it. bid/price on a later snapshot may be the only
    fields populated at all - the prices table only ever recorded a single
    trade/mid price per (asset, captured_at), never a historical ask - so
    ask is treated as optional at every snapshot and compute_fill falls back
    to price when a snapshot has no ask of its own.
    """

    captured_at: datetime
    ask: Decimal | None
    bid: Decimal | None
    liquidity: Decimal | None
    price: Decimal | None


@dataclass(frozen=True)
class FillRequest:
    """One sizing decision asking to be turned into a simulated fill."""

    signal_price: Decimal
    order_notional: Decimal
    detected_at: datetime


@dataclass(frozen=True)
class FillConfig:
    """Every fill-model tunable, bundled once - see docs/PHASE4_DESIGN.md
    section 10 for what each one means and its global default.
    """

    entry_delay_seconds: int
    slippage_k: Decimal
    slippage_max: Decimal
    no_delayed_snapshot_penalty: Decimal
    max_entry_price_drift: Decimal

    @classmethod
    def from_settings(cls, settings: Settings) -> "FillConfig":
        return cls(
            entry_delay_seconds=settings.paper_entry_delay_seconds,
            slippage_k=settings.paper_slippage_k,
            slippage_max=settings.paper_slippage_max,
            no_delayed_snapshot_penalty=settings.paper_no_delayed_snapshot_penalty,
            max_entry_price_drift=settings.paper_max_entry_price_drift,
        )


@dataclass(frozen=True)
class FillResult:
    """The outcome of one simulated fill attempt."""

    filled: bool
    fill_price: Decimal | None
    slippage_paid: Decimal | None
    reason: FillReason


def _reference_price(snapshot: MarketSnapshot) -> Decimal | None:
    """A snapshot's ask if it has one, else its last-trade price - see
    MarketSnapshot's docstring on why later snapshots often lack an ask.
    """
    return snapshot.ask if snapshot.ask is not None else snapshot.price


def compute_fill(
    request: FillRequest,
    snapshots: Sequence[MarketSnapshot],
    config: FillConfig,
    now: datetime,
) -> FillResult:
    """Simulate one fill attempt. See docs/PHASE4_DESIGN.md section 2 for
    the full rationale; summary of the steps:

    1. The earliest snapshot in `snapshots` is treated as "current" (the
       market as known at/near signal time) - its liquidity is what sizes
       the slippage penalty, and its ask is the fallback fill reference.
    2. Look for the earliest snapshot at or after
       `detected_at + entry_delay_seconds` (and not after `now` - a pure
       function shouldn't fill using data from its own future relative to
       the instant it's asked to decide). If found, that snapshot's
       ask-or-price is the fill reference instead of the current one,
       simulating reaction latency.
    3. Reject the fill (MISSED_DRIFT) if the reference price has already
       moved beyond `max_entry_price_drift` from the signal's own price -
       the opportunity is gone, not tradeable.
    4. Otherwise fill at reference price + a slippage penalty scaled by
       order size relative to current liquidity, clamped at
       `slippage_max`. No delayed snapshot found means extra uncertainty,
       modeled as an additional flat penalty
       (`no_delayed_snapshot_penalty`) on top - reason FALLBACK_FILLED
       instead of FILLED.
    """
    current = min(snapshots, key=lambda s: s.captured_at)

    if current.liquidity is None or current.liquidity < _MIN_LIQUIDITY:
        return FillResult(
            filled=False, fill_price=None, slippage_paid=None, reason=FillReason.NO_LIQUIDITY
        )

    delay_threshold = request.detected_at + timedelta(seconds=config.entry_delay_seconds)
    delayed_candidates = [s for s in snapshots if delay_threshold <= s.captured_at <= now]
    delayed = min(delayed_candidates, key=lambda s: s.captured_at) if delayed_candidates else None

    is_fallback = delayed is None
    reference_price = _reference_price(delayed) if delayed is not None else current.ask

    if reference_price is None:
        return FillResult(
            filled=False, fill_price=None, slippage_paid=None, reason=FillReason.NO_LIQUIDITY
        )

    drift = abs(reference_price - request.signal_price) / request.signal_price
    if drift > config.max_entry_price_drift:
        return FillResult(
            filled=False, fill_price=None, slippage_paid=None, reason=FillReason.MISSED_DRIFT
        )

    penalty = min(
        config.slippage_k * (request.order_notional / current.liquidity), config.slippage_max
    )
    if is_fallback:
        penalty += config.no_delayed_snapshot_penalty

    fill_price = reference_price + penalty
    reason = FillReason.FALLBACK_FILLED if is_fallback else FillReason.FILLED
    return FillResult(filled=True, fill_price=fill_price, slippage_paid=penalty, reason=reason)
