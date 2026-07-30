"""Unit tests for app/paper/engine.py's exit-side pessimistic pricing and
the SCALP routing in check_exit - constructs plain in-memory ORM instances
(no DB session involved), same convention tests/test_generator.py uses for
app/signals/generator.py's _build_groups.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.db.models import Market, PaperTrade
from app.paper.engine import ExitConfig, ExitReason, _pessimistic_exit_price, check_exit
from app.paper.exits_scalp import ScalpExitConfig, ScalpExitReason
from app.paper.fills import FillConfig
from app.paper.resolution import ResolutionOutcome

NOW = datetime(2026, 1, 1, tzinfo=UTC)

DEFAULT_FILL_CONFIG = FillConfig(
    entry_delay_seconds=30,
    slippage_k=Decimal("0.5"),
    slippage_max=Decimal("0.15"),
    no_delayed_snapshot_penalty=Decimal("0.05"),
    max_entry_price_drift=Decimal("0.15"),
)

BASELINE_EXITS = ExitConfig(
    take_profit_pct=Decimal("0.30"), stop_loss_pct=Decimal("0.20"), exit_on_signal_expiry_hours=72
)

SCALP_EXITS = ExitConfig(
    take_profit_pct=Decimal("0.30"),
    stop_loss_pct=Decimal("0.20"),
    exit_on_signal_expiry_hours=72,
    scalp=ScalpExitConfig(
        take_profit=Decimal("0.03"),
        breakeven_tolerance=Decimal("0.005"),
        max_hold_hours=72,
    ),
)


def _trade(
    size: Decimal = Decimal("100"), current_price: Decimal | None = Decimal("0.50")
) -> PaperTrade:
    trade = PaperTrade()
    trade.size = size
    trade.current_price = current_price
    return trade


def _market(best_bid: Decimal | None, liquidity: Decimal | None) -> Market:
    market = Market()
    market.best_bid = best_bid
    market.liquidity = liquidity
    return market


# --- _pessimistic_exit_price: bid-side, never mid ---


def test_uses_best_bid_not_the_raw_current_price():
    market = _market(best_bid=Decimal("0.45"), liquidity=Decimal("10000"))
    trade = _trade(size=Decimal("10"), current_price=Decimal("0.50"))  # mid/last-trade is higher

    price = _pessimistic_exit_price(market, trade, DEFAULT_FILL_CONFIG)
    assert price is not None
    assert price < Decimal("0.50")  # never the mid
    assert price < Decimal("0.45")  # bid, minus a slippage penalty on top


def test_penalty_scales_with_size_relative_to_liquidity():
    thin_market = _market(best_bid=Decimal("0.45"), liquidity=Decimal("100"))
    deep_market = _market(best_bid=Decimal("0.45"), liquidity=Decimal("1000000"))
    trade = _trade(size=Decimal("50"))

    thin_price = _pessimistic_exit_price(thin_market, trade, DEFAULT_FILL_CONFIG)
    deep_price = _pessimistic_exit_price(deep_market, trade, DEFAULT_FILL_CONFIG)
    assert thin_price < deep_price  # bigger penalty against thinner liquidity


def test_penalty_is_clamped_at_slippage_max():
    market = _market(best_bid=Decimal("0.45"), liquidity=Decimal("1"))  # near-zero liquidity
    trade = _trade(size=Decimal("1000000"))  # huge notional relative to liquidity

    price = _pessimistic_exit_price(market, trade, DEFAULT_FILL_CONFIG)
    assert price == Decimal("0.45") - DEFAULT_FILL_CONFIG.slippage_max


def test_falls_back_to_current_price_when_no_bid_recorded():
    market = _market(best_bid=None, liquidity=Decimal("10000"))
    trade = _trade(current_price=Decimal("0.60"))

    price = _pessimistic_exit_price(market, trade, DEFAULT_FILL_CONFIG)
    assert price is not None
    assert price < Decimal("0.60")  # still a real reference minus slippage, not the raw 0.60


def test_none_when_no_market():
    assert _pessimistic_exit_price(None, _trade(), DEFAULT_FILL_CONFIG) is None


def test_none_when_no_liquidity_to_size_a_penalty_from():
    market = _market(best_bid=Decimal("0.45"), liquidity=None)
    assert _pessimistic_exit_price(market, _trade(), DEFAULT_FILL_CONFIG) is None


# --- check_exit: scalp routing ---


def test_market_resolved_still_wins_over_scalp():
    reason = check_exit(
        entry_price=Decimal("0.50"),
        current_price=Decimal("1.0"),
        pessimistic_price=Decimal("1.0"),
        entered_at=NOW - timedelta(hours=1),
        resolution=ResolutionOutcome.WON,
        signal_created_at=NOW - timedelta(hours=2),
        now=NOW,
        config=SCALP_EXITS,
    )
    assert reason == ExitReason.MARKET_RESOLVED


def test_scalp_config_bypasses_the_baseline_ladder_entirely():
    """A price move that WOULD trigger baseline's take_profit_pct (30%) but
    falls short of scalp's own 0.03 absolute threshold must NOT exit - once
    scalp is configured, the baseline percentage ladder is never consulted.
    """
    entry_price = Decimal("0.50")
    small_move = entry_price + Decimal(
        "0.01"
    )  # +2% - nowhere near baseline's 30%, and < scalp's 0.03
    reason = check_exit(
        entry_price=entry_price,
        current_price=small_move,
        pessimistic_price=small_move,
        entered_at=NOW - timedelta(hours=1),
        resolution=ResolutionOutcome.NOT_RESOLVED,
        signal_created_at=NOW - timedelta(hours=1),
        now=NOW,
        config=SCALP_EXITS,
    )
    assert reason is None


def test_scalp_config_routes_to_scalp_take_profit():
    entry_price = Decimal("0.50")
    reason = check_exit(
        entry_price=entry_price,
        current_price=entry_price + Decimal("0.03"),
        pessimistic_price=entry_price + Decimal("0.03"),
        entered_at=NOW - timedelta(hours=1),
        resolution=ResolutionOutcome.NOT_RESOLVED,
        signal_created_at=NOW - timedelta(hours=1),
        now=NOW,
        config=SCALP_EXITS,
    )
    assert reason == ScalpExitReason.TAKE_PROFIT


def test_baseline_config_unaffected_by_scalp_existing():
    """config.scalp is None for baseline - behaviour is unchanged from
    before SCALP existed.
    """
    entry_price = Decimal("0.50")
    reason = check_exit(
        entry_price=entry_price,
        current_price=entry_price * Decimal("1.30"),  # exactly at baseline's 30% take-profit
        pessimistic_price=None,
        entered_at=NOW - timedelta(hours=1),
        resolution=ResolutionOutcome.NOT_RESOLVED,
        signal_created_at=NOW - timedelta(hours=1),
        now=NOW,
        config=BASELINE_EXITS,
    )
    assert reason == ExitReason.TAKE_PROFIT
