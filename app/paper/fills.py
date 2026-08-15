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


class FillMethod(StrEnum):
    """How a filled trade's price was actually derived - independent of
    FillReason (which is about whether/why a fill happened at all). Stored
    on paper_trades so real fills can always be told apart from estimated
    ones in analysis (see app/paper/metrics.py's credibility gap).
    """

    BOOK_WALK = "BOOK_WALK"
    ESTIMATED = "ESTIMATED"


@dataclass(frozen=True)
class BookLevel:
    """One order-book price level - see docs/API_REFERENCE.md's /book entry.
    price/size are already Decimal by the time this is constructed (the
    collector persists them as strings in order_books.levels JSONB; the
    caller parses back to Decimal, this dataclass never touches JSON
    itself).
    """

    price: Decimal
    size: Decimal


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
    max_book_depth_fraction: Decimal

    @classmethod
    def from_settings(cls, settings: Settings) -> "FillConfig":
        return cls(
            entry_delay_seconds=settings.paper_entry_delay_seconds,
            slippage_k=settings.paper_slippage_k,
            slippage_max=settings.paper_slippage_max,
            no_delayed_snapshot_penalty=settings.paper_no_delayed_snapshot_penalty,
            max_entry_price_drift=settings.paper_max_entry_price_drift,
            max_book_depth_fraction=settings.paper_max_book_depth_fraction,
        )


@dataclass(frozen=True)
class FillResult:
    """The outcome of one simulated fill attempt. fill_method is None
    whenever filled=False - a rejected order was never priced at all,
    real or estimated.
    """

    filled: bool
    fill_price: Decimal | None
    slippage_paid: Decimal | None
    reason: FillReason
    fill_method: FillMethod | None = None


def walk_the_book(
    book_side: Sequence[BookLevel], size_usd: Decimal, max_depth_fraction: Decimal
) -> Decimal | None:
    """Size-weighted average price to fill size_usd (in dollars, not
    shares) by consuming book_side's levels in the order given - the
    caller's responsibility to pass them in price-priority order (the CLOB
    API already returns asks price-ascending, bids price-descending; see
    docs/API_REFERENCE.md - never re-sorted here so this stays a pure,
    trusting consumer of whatever order it's handed).

    None (unfillable) in two cases, both meaning the same thing - don't
    trust a price this order can't honestly get:
      - size_usd exceeds max_depth_fraction of the side's total visible
        USD depth (sum of price*size across every level) - beyond that,
        the order itself would move the market enough that the visible
        levels no longer describe what it would actually pay.
      - the book (even ignoring the depth cap) doesn't have enough total
        depth to fill size_usd at all.
    """
    if size_usd <= 0 or not book_side:
        return None

    total_depth_usd = sum((level.price * level.size for level in book_side), Decimal(0))
    if total_depth_usd <= 0 or size_usd > max_depth_fraction * total_depth_usd:
        return None

    remaining = size_usd
    shares = Decimal(0)
    cost = Decimal(0)
    for level in book_side:
        level_usd = level.price * level.size
        if remaining <= level_usd:
            shares += remaining / level.price
            cost += remaining
            remaining = Decimal(0)
            break
        shares += level.size
        cost += level_usd
        remaining -= level_usd

    if remaining > 0 or shares <= 0:
        return None
    return cost / shares


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
    book: Sequence[BookLevel] | None = None,
) -> FillResult:
    """Simulate one fill attempt. See docs/PHASE4_DESIGN.md section 2 for
    the entry-delay/drift rationale (unchanged by the book-walk model
    below - see docs/API_REFERENCE.md's /book entry for that part):

    1. The earliest snapshot in `snapshots` is treated as "current" (the
       market as known at/near signal time) - its liquidity is what sizes
       the fallback slippage penalty, and its ask is the fallback fill
       reference.
    2. Look for the earliest snapshot at or after
       `detected_at + entry_delay_seconds` (and not after `now` - a pure
       function shouldn't fill using data from its own future relative to
       the instant it's asked to decide). If found, that snapshot's
       ask-or-price is the fill reference instead of the current one,
       simulating reaction latency.
    3. Reject the fill (MISSED_DRIFT) if the reference price has already
       moved beyond `max_entry_price_drift` from the signal's own price -
       the opportunity is gone, not tradeable. Unchanged by whether a book
       is available.
    4. Price the fill:
       - `book` given and walk_the_book() can fill the full order within
         the depth cap: fill at that size-weighted average price -
         FillMethod.BOOK_WALK, reason FILLED. slippage_paid is the walked
         price minus the reference price (can be negative - the book was
         better than the reference snapshot suggested; still real).
       - `book` given but walk_the_book() returns None (too thin, or the
         order would exceed the depth cap): reject as NO_LIQUIDITY. We
         have real book data saying this order can't be honestly filled -
         falling back to an estimate here would be exactly the dishonesty
         this whole model exists to remove.
       - `book` is None (no snapshot existed for this market at fill
         time): fall back to the old ask-or-price + slippage-constant
         model, FillMethod.ESTIMATED, reason FALLBACK_FILLED (or FILLED
         if a delayed snapshot was found - same distinction as before).
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

    if book is not None:
        avg_price = walk_the_book(book, request.order_notional, config.max_book_depth_fraction)
        if avg_price is None:
            return FillResult(
                filled=False, fill_price=None, slippage_paid=None, reason=FillReason.NO_LIQUIDITY
            )
        return FillResult(
            filled=True,
            fill_price=avg_price,
            slippage_paid=avg_price - reference_price,
            reason=FillReason.FILLED,
            fill_method=FillMethod.BOOK_WALK,
        )

    penalty = min(
        config.slippage_k * (request.order_notional / current.liquidity), config.slippage_max
    )
    if is_fallback:
        penalty += config.no_delayed_snapshot_penalty

    fill_price = reference_price + penalty
    reason = FillReason.FALLBACK_FILLED if is_fallback else FillReason.FILLED
    return FillResult(
        filled=True,
        fill_price=fill_price,
        slippage_paid=penalty,
        reason=reason,
        fill_method=FillMethod.ESTIMATED,
    )
