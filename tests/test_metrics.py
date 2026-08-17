"""Tests for app/paper/metrics.py - pure function tests with known sequences.

Every test uses hand-computed expected values, not properties of the
implementation itself, matching the style of test_fills.py.
"""

import itertools
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.paper.metrics import (
    CredibilityTradeRecord,
    TradeData,
    compute_credibility_gap,
    compute_portfolio_metrics,
)

NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
STARTING = Decimal("10000")

# Each call defaults to a fresh, distinct condition_id (no event_cluster_id)
# so effective_closed_count == closed_count in every pre-existing test below
# - none of them are testing event-clustering behavior, so they should be
# unaffected by it. The dedicated cluster tests further down pass an
# explicit condition_id/event_cluster_id instead.
_condition_id_counter = itertools.count()


def _closed(
    realized_pnl: Decimal,
    entry_price: Decimal = Decimal("0.50"),
    size: Decimal = Decimal("100"),
    days_offset: int = 0,
    condition_id: str | None = None,
    event_cluster_id: str | None = None,
) -> TradeData:
    return TradeData(
        status="CLOSED",
        condition_id=condition_id or f"0xcond{next(_condition_id_counter)}",
        event_cluster_id=event_cluster_id,
        entry_price=entry_price,
        size=size,
        realized_pnl=realized_pnl,
        exit_at=NOW + timedelta(days=days_offset),
    )


def _open(
    unrealized_pnl: Decimal,
    entry_price: Decimal = Decimal("0.50"),
    size: Decimal = Decimal("100"),
) -> TradeData:
    return TradeData(
        status="OPEN",
        condition_id=f"0xcond{next(_condition_id_counter)}",
        entry_price=entry_price,
        size=size,
        unrealized_pnl=unrealized_pnl,
    )


def test_basic_metrics() -> None:
    """3 closed, 1 open: hand-computed PnL, win rate, profit factor,
    drawdown all verified against known arithmetic.
    """
    trades = [
        _closed(Decimal("10"), days_offset=1),
        _closed(Decimal("-20"), days_offset=2),
        _closed(Decimal("30"), days_offset=3),
        _open(Decimal("5")),
    ]
    current_bankroll = STARTING + Decimal("20")

    result = compute_portfolio_metrics(
        trades,
        starting_bankroll=STARTING,
        current_bankroll=current_bankroll,
        min_trades_for_stats=3,
    )

    # Always-computed fields
    assert result.total_realized_pnl == Decimal("20")
    assert result.unrealized_pnl == Decimal("5")
    assert result.current_bankroll == current_bankroll
    assert result.roi_pct == Decimal("25") / STARTING

    # Ratio-shaped (above min_trades_for_stats=3)
    assert result.win_rate == Decimal("2") / Decimal("3")
    assert result.avg_win == Decimal("20")
    assert result.avg_loss == Decimal("-20")
    assert result.profit_factor == Decimal("2")

    # Counts
    assert result.closed_count == 3
    assert result.open_count == 1
    assert result.insufficient_sample_note is None


def test_max_drawdown() -> None:
    """Equity curve: 10000 -> 9960 -> 10040 -> 9980. Max drawdown is
    60/10040 (trough at 9980, peak at 10040).
    """
    trades = [
        _closed(Decimal("-40"), days_offset=1),
        _closed(Decimal("80"), days_offset=2),
        _closed(Decimal("-60"), days_offset=3),
    ]
    current_bankroll = STARTING + Decimal("-20")

    result = compute_portfolio_metrics(
        trades,
        starting_bankroll=STARTING,
        current_bankroll=current_bankroll,
        min_trades_for_stats=3,
    )

    assert result.max_drawdown_pct == Decimal("60") / Decimal("10040")


def test_small_sample_returns_none_for_ratios() -> None:
    """5 closed trades vs min_trades_for_stats=30: every ratio-shaped stat
    is None with an explanatory note.
    """
    trades = [_closed(Decimal("10"), days_offset=i) for i in range(5)]
    current_bankroll = STARTING + Decimal("50")

    result = compute_portfolio_metrics(
        trades,
        starting_bankroll=STARTING,
        current_bankroll=current_bankroll,
        min_trades_for_stats=30,
    )

    assert result.win_rate is None
    assert result.profit_factor is None
    assert result.sharpe is None
    assert result.max_drawdown_pct is None
    assert result.insufficient_sample_note is not None
    assert "n=5, effective n=5<30" in result.insufficient_sample_note


def test_no_losing_trades_profit_factor_none() -> None:
    """All winners: no gross losses, so profit_factor is undefined -> None.
    avg_loss is also None.
    """
    trades = [
        _closed(Decimal("10"), days_offset=1),
        _closed(Decimal("20"), days_offset=2),
    ]
    current_bankroll = STARTING + Decimal("30")

    result = compute_portfolio_metrics(
        trades,
        starting_bankroll=STARTING,
        current_bankroll=current_bankroll,
        min_trades_for_stats=2,
    )

    assert result.avg_loss is None
    assert result.profit_factor is None
    assert result.win_rate == Decimal("1")


def test_losing_only_profit_factor_zero() -> None:
    """All losers: profit_factor = 0 (no gross wins). avg_win is None."""
    trades = [
        _closed(Decimal("-10"), days_offset=1),
        _closed(Decimal("-20"), days_offset=2),
    ]
    current_bankroll = STARTING + Decimal("-30")

    result = compute_portfolio_metrics(
        trades,
        starting_bankroll=STARTING,
        current_bankroll=current_bankroll,
        min_trades_for_stats=2,
    )

    assert result.avg_win is None
    assert result.profit_factor == Decimal("0")
    assert result.win_rate == Decimal("0")


def test_no_closed_trades() -> None:
    """No closed trades: gated metrics are None, basic ones are 0/empty."""
    trades = [_open(Decimal("5"))]
    current_bankroll = STARTING

    result = compute_portfolio_metrics(
        trades,
        starting_bankroll=STARTING,
        current_bankroll=current_bankroll,
        min_trades_for_stats=1,
    )

    assert result.closed_count == 0
    assert result.open_count == 1
    assert result.total_realized_pnl == Decimal("0")
    assert result.unrealized_pnl == Decimal("5")
    assert result.win_rate is None
    assert result.profit_factor is None
    assert result.sharpe is None
    assert result.max_drawdown_pct is None
    assert result.insufficient_sample_note is not None


def test_single_trade_sharpe_none() -> None:
    """1 closed trade: std dev can't be computed, so sharpe is None."""
    trades = [_closed(Decimal("10"), days_offset=1)]
    current_bankroll = STARTING + Decimal("10")

    result = compute_portfolio_metrics(
        trades,
        starting_bankroll=STARTING,
        current_bankroll=current_bankroll,
        min_trades_for_stats=1,
    )

    assert result.sharpe is None


def test_sharpe_computed() -> None:
    """3 trades with returns [0.20, -0.40, 0.60]: mean=0.13333...,
    variance=0.25333..., std=0.50332, annualized by trades-per-year
    (3 trades / 2 days ≈ 547.875 trades/yr).
    """
    trades = [
        _closed(Decimal("10"), entry_price=Decimal("0.50"), size=Decimal("100"), days_offset=1),
        _closed(Decimal("-20"), entry_price=Decimal("0.50"), size=Decimal("100"), days_offset=2),
        _closed(Decimal("30"), entry_price=Decimal("0.50"), size=Decimal("100"), days_offset=3),
    ]
    current_bankroll = STARTING + Decimal("20")

    result = compute_portfolio_metrics(
        trades,
        starting_bankroll=STARTING,
        current_bankroll=current_bankroll,
        min_trades_for_stats=3,
    )

    assert result.sharpe is not None
    assert result.sharpe > Decimal("0")


def test_identical_returns_sharpe_none() -> None:
    """All trades have the same per-trade return: std dev = 0 -> None."""
    trades = [
        _closed(Decimal("10"), entry_price=Decimal("0.50"), size=Decimal("100"), days_offset=1),
        _closed(Decimal("10"), entry_price=Decimal("0.50"), size=Decimal("100"), days_offset=2),
    ]
    current_bankroll = STARTING + Decimal("20")

    result = compute_portfolio_metrics(
        trades,
        starting_bankroll=STARTING,
        current_bankroll=current_bankroll,
        min_trades_for_stats=2,
    )

    assert result.sharpe is None


def test_zero_cost_trade_skipped_in_sharpe() -> None:
    """A trade with entry_price=0 has an undefined return and is excluded
    from the Sharpe calculation. With only 1 valid return left, Sharpe is
    None (need >= 2).
    """
    trades = [
        TradeData(
            status="CLOSED",
            condition_id="0xcondzerocost",
            entry_price=Decimal("0"),
            size=Decimal("100"),
            realized_pnl=Decimal("10"),
            exit_at=NOW + timedelta(days=1),
        ),
        _closed(Decimal("20"), days_offset=2),
    ]
    current_bankroll = STARTING + Decimal("30")

    result = compute_portfolio_metrics(
        trades,
        starting_bankroll=STARTING,
        current_bankroll=current_bankroll,
        min_trades_for_stats=2,
    )

    assert result.sharpe is None


def test_all_trades_one_event_cluster_collapses_effective_n() -> None:
    """5 closed trades, all in the same event cluster: nominal n=5 but
    effective n=1 - they are one real bet, not 5 independent ones.
    """
    trades = [
        _closed(Decimal("10"), days_offset=i, event_cluster_id="event:same-election")
        for i in range(5)
    ]
    result = compute_portfolio_metrics(
        trades, starting_bankroll=STARTING, current_bankroll=STARTING + 50, min_trades_for_stats=3
    )

    assert result.closed_count == 5
    assert result.effective_closed_count == 1


def test_trades_across_distinct_events_preserve_effective_n() -> None:
    """5 closed trades, each in its own distinct event cluster: effective n
    equals nominal n - genuinely independent observations aren't discounted.
    """
    trades = [
        _closed(Decimal("10"), days_offset=i, event_cluster_id=f"event:distinct-{i}")
        for i in range(5)
    ]
    result = compute_portfolio_metrics(
        trades, starting_bankroll=STARTING, current_bankroll=STARTING + 50, min_trades_for_stats=3
    )

    assert result.closed_count == 5
    assert result.effective_closed_count == 5


def test_insufficient_sample_guard_fires_on_effective_n_not_nominal() -> None:
    """40 closed trades clears a nominal min_trades_for_stats=30 bar, but
    if they're all one event cluster the effective sample is 1 - the guard
    must fire on that, not the flattering nominal count.
    """
    trades = [
        _closed(Decimal("10"), days_offset=i, event_cluster_id="event:one-tournament")
        for i in range(40)
    ]
    result = compute_portfolio_metrics(
        trades, starting_bankroll=STARTING, current_bankroll=STARTING + 400, min_trades_for_stats=30
    )

    assert result.closed_count == 40
    assert result.effective_closed_count == 1
    assert result.win_rate is None
    assert result.insufficient_sample_note is not None
    assert "n=40, effective n=1<30" in result.insufficient_sample_note


def test_zero_starting_bankroll_roi_none() -> None:
    """Division by zero guard on ROI when starting_bankroll is 0."""
    trades = [_closed(Decimal("10"), days_offset=1)]
    current_bankroll = Decimal("10")

    result = compute_portfolio_metrics(
        trades,
        starting_bankroll=Decimal("0"),
        current_bankroll=current_bankroll,
        min_trades_for_stats=1,
    )

    assert result.roi_pct is None


# --- compute_credibility_gap(): book-walk P&L vs naive mid-price P&L ---


def test_credibility_gap_hand_computed() -> None:
    """One trade: bought at a real (book-walked) entry_price of 0.60,
    sold at 0.80, size 100 -> notional=60, realized_pnl=(0.80-0.60)*100=20.
    Naive mid-price model: same $60 notional at mid=0.55 instead ->
    60/0.55 shares, pnl = 60*(0.80/0.55 - 1).
    """
    record = CredibilityTradeRecord(
        entry_price=Decimal("0.60"),
        exit_price=Decimal("0.80"),
        size=Decimal("100"),
        realized_pnl=Decimal("20"),
        mid_price_at_entry=Decimal("0.55"),
    )

    result = compute_credibility_gap([record])

    notional = Decimal("60")
    expected_mid_pnl = notional * (Decimal("0.80") / Decimal("0.55") - 1)
    assert result.n == 1
    assert result.book_walk_pnl == Decimal("20")
    assert result.mid_price_pnl == expected_mid_pnl
    assert result.gap == expected_mid_pnl - Decimal("20")
    # Buying cheaper than mid (real ask/book price 0.60 < ... wait mid 0.55 is
    # BELOW the real entry - a naive mid model would have bought cheaper and
    # so reported MORE profit: the gap should be positive here.
    assert result.gap > 0


def test_credibility_gap_trades_without_mid_price_excluded() -> None:
    """A trade with no trustworthy mid_price_at_entry contributes to
    neither sum - never partially counted.
    """
    with_mid = CredibilityTradeRecord(
        entry_price=Decimal("0.60"),
        exit_price=Decimal("0.80"),
        size=Decimal("100"),
        realized_pnl=Decimal("20"),
        mid_price_at_entry=Decimal("0.55"),
    )
    without_mid = CredibilityTradeRecord(
        entry_price=Decimal("0.40"),
        exit_price=Decimal("0.90"),
        size=Decimal("50"),
        realized_pnl=Decimal("25"),
        mid_price_at_entry=None,
    )

    result = compute_credibility_gap([with_mid, without_mid])

    assert result.n == 1
    assert result.book_walk_pnl == Decimal("20")  # only the with_mid trade


def test_credibility_gap_empty_input_is_neutral() -> None:
    result = compute_credibility_gap([])

    assert result.n == 0
    assert result.book_walk_pnl == Decimal("0")
    assert result.mid_price_pnl == Decimal("0")
    assert result.gap == Decimal("0")


def test_credibility_gap_sums_across_multiple_trades() -> None:
    records = [
        CredibilityTradeRecord(
            entry_price=Decimal("0.50"),
            exit_price=Decimal("0.60"),
            size=Decimal("100"),
            realized_pnl=Decimal("10"),
            mid_price_at_entry=Decimal("0.50"),
        ),
        CredibilityTradeRecord(
            entry_price=Decimal("0.60"),
            exit_price=Decimal("0.50"),
            size=Decimal("100"),
            realized_pnl=Decimal("-10"),
            mid_price_at_entry=Decimal("0.60"),
        ),
    ]

    result = compute_credibility_gap(records)

    assert result.n == 2
    assert result.book_walk_pnl == Decimal("0")  # +10 and -10 cancel
    # Both trades' entry_price equals their own mid_price_at_entry here, so
    # the naive model reproduces the exact same real P&L on each.
    assert result.mid_price_pnl == Decimal("0")
    assert result.gap == Decimal("0")
