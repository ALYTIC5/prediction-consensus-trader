"""Portfolio performance metrics - pure, fully unit-testable.

See docs/PHASE4_DESIGN.md section 6. Every ratio-shaped statistic returns
None when the sample is too small to mean anything, with a note explaining
why, rather than a misleading number. Division-by-zero guards on profit
factor (no losses yet) and Sharpe (std dev of zero or single-trade sample)
reflect the same principle: a number that cannot be computed honestly is
not reported.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class TradeData:
    """One portfolio trade as a pure data value - no ORM, no DB session.

    CLOSED trades must have entry_price, size, realized_pnl, and exit_at.
    OPEN trades must have entry_price, size, and unrealized_pnl.
    MISSED trades are filtered out upstream and carry none of these fields.
    """

    status: str
    entry_price: Decimal | None = None
    size: Decimal | None = None
    realized_pnl: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    exit_at: datetime | None = None


@dataclass(frozen=True)
class PortfolioMetrics:
    """Complete set of portfolio performance statistics.

    Every ratio-shaped field (win_rate, profit_factor, sharpe) is None when
    the portfolio has fewer than min_trades_for_stats closed trades, with
    insufficient_sample_note explaining the gap. Non-ratio fields
    (total_realized_pnl, unrealized_pnl, current_bankroll, roi_pct,
    closed_count, open_count) are always populated regardless of sample size.
    avg_win and avg_loss are None when there are no winning or no losing
    trades respectively.
    """

    total_realized_pnl: Decimal
    unrealized_pnl: Decimal
    current_bankroll: Decimal
    roi_pct: Decimal | None
    win_rate: Decimal | None
    avg_win: Decimal | None
    avg_loss: Decimal | None
    profit_factor: Decimal | None
    max_drawdown_pct: Decimal | None
    sharpe: Decimal | None
    closed_count: int
    open_count: int
    insufficient_sample_note: str | None


def _compute_max_drawdown(
    closed: list[TradeData],
    opened: list[TradeData],
    starting_bankroll: Decimal,
) -> Decimal:
    """Largest peak-to-trough decline on the equity curve built from closed
    trades in exit order, as a percentage of the peak. The final point
    includes unrealized P&L from open trades.
    """
    sorted_closed = sorted(
        [t for t in closed if t.exit_at is not None and t.realized_pnl is not None],
        key=lambda t: t.exit_at,
    )
    unrealized = sum(
        (t.unrealized_pnl for t in opened if t.unrealized_pnl is not None),
        Decimal("0"),
    )

    equity = starting_bankroll
    peak = equity
    max_dd = Decimal("0")

    for trade in sorted_closed:
        equity += trade.realized_pnl
        if equity > peak:
            peak = equity
        if peak > Decimal("0"):
            dd = (peak - equity) / peak
            if dd > max_dd:
                max_dd = dd

    final_equity = equity + unrealized
    if final_equity > peak:
        peak = final_equity
    if peak > Decimal("0"):
        dd = (peak - final_equity) / peak
        if dd > max_dd:
            max_dd = dd

    return max_dd


def _compute_sharpe(closed: list[TradeData], closed_count: int) -> Decimal | None:
    """Per-trade Sharpe ratio annualized by the portfolio's observed
    average trades-per-year rate. None when fewer than 2 trades have valid
    returns, or when the return series has zero variance.
    """
    returns: list[Decimal] = []
    for t in closed:
        cost = (
            (t.entry_price * t.size) if t.entry_price is not None and t.size is not None else None
        )
        if t.realized_pnl is not None and cost is not None and cost > Decimal("0"):
            returns.append(t.realized_pnl / cost)

    if len(returns) < 2:
        return None

    n = len(returns)
    mean_ret = sum(returns) / Decimal(str(n))
    variance = sum((r - mean_ret) ** 2 for r in returns) / Decimal(str(n - 1))
    if variance == Decimal("0"):
        return None

    std_ret = variance.sqrt()
    if std_ret == Decimal("0"):
        return None

    # Estimate trades per year from the span of exit timestamps
    exits = sorted([t.exit_at for t in closed if t.exit_at is not None])
    if len(exits) >= 2:
        span_days = (exits[-1] - exits[0]).total_seconds() / 86400
        min_span_years = float(Decimal("1") / Decimal("365.25"))
        years = Decimal(str(max(span_days / 365.25, min_span_years)))
        trades_per_year = Decimal(str(closed_count)) / years
    else:
        trades_per_year = Decimal(str(max(closed_count, 1)))

    ann_factor = trades_per_year.sqrt()
    return ann_factor * mean_ret / std_ret


def compute_portfolio_metrics(
    trades: list[TradeData],
    starting_bankroll: Decimal,
    current_bankroll: Decimal,
    min_trades_for_stats: int = 30,
) -> PortfolioMetrics:
    """Compute every portfolio metric from a list of trades.

    Parameters
    ----------
    trades:
        Every trade for one portfolio (CLOSED, OPEN, or MISSED). MISSED
        trades contribute to nothing.
    starting_bankroll:
        The portfolio's initial capital when it started trading.
    current_bankroll:
        The portfolio's current cash balance (from the ORM, already
        reconciled by the engine after every entry/exit).
    min_trades_for_stats:
        Minimum number of CLOSED trades before ratio-shaped statistics
        (win_rate, profit_factor, sharpe) are reported. Default matches
        the global setting (30) but is parameterized for testability.

    Returns
    -------
    PortfolioMetrics with every field populated.
    """
    closed = [t for t in trades if t.status == "CLOSED"]
    opened = [t for t in trades if t.status == "OPEN"]
    closed_count = len(closed)
    open_count = len(opened)

    insufficient = closed_count < min_trades_for_stats
    insufficient_note = (
        f"insufficient sample (n={closed_count}<{min_trades_for_stats})" if insufficient else None
    )

    # --- Always-computed fields ---
    total_realized_pnl = sum(
        (t.realized_pnl for t in closed if t.realized_pnl is not None),
        Decimal("0"),
    )
    unrealized_pnl = sum(
        (t.unrealized_pnl for t in opened if t.unrealized_pnl is not None),
        Decimal("0"),
    )
    roi_pct = (
        ((current_bankroll + unrealized_pnl - starting_bankroll) / starting_bankroll)
        if starting_bankroll != Decimal("0")
        else None
    )

    # --- Win/loss classification ---
    win_trades = [t for t in closed if t.realized_pnl is not None and t.realized_pnl > Decimal("0")]
    loss_trades = [
        t for t in closed if t.realized_pnl is not None and t.realized_pnl < Decimal("0")
    ]

    avg_win = (
        (sum(t.realized_pnl for t in win_trades) / Decimal(str(len(win_trades))))
        if win_trades
        else None
    )
    avg_loss = (
        (sum(t.realized_pnl for t in loss_trades) / Decimal(str(len(loss_trades))))
        if loss_trades
        else None
    )

    # --- Ratio-shaped fields (gated by sample size) ---
    if insufficient:
        return PortfolioMetrics(
            total_realized_pnl=total_realized_pnl,
            unrealized_pnl=unrealized_pnl,
            current_bankroll=current_bankroll,
            roi_pct=roi_pct,
            win_rate=None,
            avg_win=avg_win,
            avg_loss=avg_loss,
            profit_factor=None,
            max_drawdown_pct=None,
            sharpe=None,
            closed_count=closed_count,
            open_count=open_count,
            insufficient_sample_note=insufficient_note,
        )

    win_rate = (
        Decimal(str(len(win_trades))) / Decimal(str(closed_count)) if closed_count > 0 else None
    )

    gross_wins = sum((t.realized_pnl for t in win_trades), Decimal("0"))
    gross_losses = sum((t.realized_pnl for t in loss_trades), Decimal("0"))
    profit_factor = (
        gross_wins / abs(gross_losses) if loss_trades and gross_losses != Decimal("0") else None
    )

    max_drawdown_pct = _compute_max_drawdown(closed, opened, starting_bankroll)

    sharpe = _compute_sharpe(closed, closed_count)

    return PortfolioMetrics(
        total_realized_pnl=total_realized_pnl,
        unrealized_pnl=unrealized_pnl,
        current_bankroll=current_bankroll,
        roi_pct=roi_pct,
        win_rate=win_rate,
        avg_win=avg_win,
        avg_loss=avg_loss,
        profit_factor=profit_factor,
        max_drawdown_pct=max_drawdown_pct,
        sharpe=sharpe,
        closed_count=closed_count,
        open_count=open_count,
        insufficient_sample_note=insufficient_note,
    )


@dataclass(frozen=True)
class CredibilityTradeRecord:
    """One closed trade's real fill plus the market's mid price nearest its
    entry time - the two things needed to compare what we actually got
    (book-walk-capable, see app/paper/fills.py) against what a naive
    mid-price fill model would have produced on the identical trade.
    mid_price_at_entry is None when no market_history row exists near
    enough to entry_at to trust - that trade is excluded from the
    comparison rather than guessed (same "don't report what can't be
    computed honestly" rule this whole module follows).
    """

    entry_price: Decimal
    exit_price: Decimal
    size: Decimal
    realized_pnl: Decimal
    mid_price_at_entry: Decimal | None


@dataclass(frozen=True)
class CredibilityGap:
    """The honesty check itself - see docs on compute_credibility_gap().

    gap = mid_price_pnl - book_walk_pnl: positive means a naive mid-price
    model would have reported MORE profit than what actually happened -
    i.e. past/naive results were overstated by that many dollars across
    these n trades. Negative means mid-price would have looked worse -
    still worth seeing, not just the case anyone would expect.
    """

    n: int
    book_walk_pnl: Decimal
    mid_price_pnl: Decimal
    gap: Decimal


def compute_credibility_gap(records: list[CredibilityTradeRecord]) -> CredibilityGap:
    """Same notional (size*entry_price) deployed both ways, so the two P&L
    figures are a fair apples-to-apples comparison and not an artifact of
    sizing differently at a different price: book_walk_pnl is what actually
    happened (the trade's own realized_pnl); mid_price_pnl is that same
    dollar amount re-priced at entry as if it had bought
    notional/mid_price_at_entry shares instead of notional/entry_price.
    Trades with no trustworthy mid_price_at_entry are skipped entirely,
    from both sums - never partially counted.
    """
    book_walk_pnl = Decimal(0)
    mid_price_pnl = Decimal(0)
    n = 0
    for r in records:
        if r.mid_price_at_entry is None or r.mid_price_at_entry <= 0:
            continue
        notional = r.size * r.entry_price
        book_walk_pnl += r.realized_pnl
        mid_price_pnl += notional * (r.exit_price / r.mid_price_at_entry - 1)
        n += 1
    return CredibilityGap(
        n=n,
        book_walk_pnl=book_walk_pnl,
        mid_price_pnl=mid_price_pnl,
        gap=mid_price_pnl - book_walk_pnl,
    )
