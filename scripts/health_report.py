"""Read-only data health report - no schema or code changes, just SELECTs.

Sections: heartbeat (staleness per collector), volume, fresh event rate,
funnel trends, active signals, trader score distribution, and data gaps.
Plain text output, no new dependencies - same style as show_signals.py.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from app.config.settings import get_settings
from app.db.models import (
    ConsensusRun,
    LeaderboardSnapshot,
    Market,
    MarketHistory,
    Position,
    PositionHistory,
    PriceSnapshot,
    Signal,
    SignalStatus,
    TraderScore,
    Wallet,
)
from app.db.session import db_session

_WINDOW_HOURS = 16

_FUNNEL_STAGES = (
    "events",
    "groups",
    "market",
    "liquidity",
    "price",
    "spread",
    "breadth",
    "quality",
    "conviction",
)


def _format_age(delta: timedelta) -> str:
    minutes = delta.total_seconds() / 60
    if minutes < 60:
        return f"{minutes:.1f}m"
    hours, rem_minutes = divmod(minutes, 60)
    return f"{int(hours)}h{int(rem_minutes)}m"


def _print_header(title: str) -> None:
    print(f"\n=== {title} ===")


def _print_heartbeat(session, now: datetime) -> None:
    settings = get_settings()
    _print_header("HEARTBEAT")

    checks = (
        ("position_history", PositionHistory.detected_at, settings.positions_interval_seconds),
        ("market_history", MarketHistory.captured_at, settings.markets_interval_seconds),
        ("prices", PriceSnapshot.captured_at, settings.markets_interval_seconds),
        (
            "leaderboard_snapshots",
            LeaderboardSnapshot.captured_at,
            settings.leaderboard_interval_seconds,
        ),
        ("consensus_runs", ConsensusRun.executed_at, settings.consensus_interval_seconds),
    )

    rows = []
    for name, column, interval_seconds in checks:
        latest = session.execute(select(func.max(column))).scalar_one()
        if latest is None:
            rows.append((name, "no data", "no data", ""))
            continue
        age = now - latest
        stale = age.total_seconds() > 3 * interval_seconds
        rows.append(
            (
                name,
                latest.strftime("%Y-%m-%d %H:%M:%S UTC"),
                _format_age(age),
                "STALE" if stale else "ok",
            )
        )

    name_width = max(len(r[0]) for r in rows)
    ts_width = max(len(r[1]) for r in rows)
    age_width = max(len(r[2]) for r in rows)
    for name, ts, age, flag in rows:
        print(f"{name.ljust(name_width)}  {ts.ljust(ts_width)}  age={age.rjust(age_width)}  {flag}")


def _print_volume(session) -> None:
    _print_header("VOLUME")

    tables = (
        ("position_history", PositionHistory),
        ("positions", Position),
        ("market_history", MarketHistory),
        ("markets", Market),
        ("prices", PriceSnapshot),
        ("leaderboard_snapshots", LeaderboardSnapshot),
        ("wallets", Wallet),
        ("trader_scores", TraderScore),
        ("signals", Signal),
        ("consensus_runs", ConsensusRun),
    )
    counts = {
        name: session.execute(select(func.count()).select_from(model)).scalar_one()
        for name, model in tables
    }
    name_width = max(len(name) for name, _ in tables)
    for name, _ in tables:
        print(f"{name.ljust(name_width)}  {counts[name]}")

    open_positions = session.execute(
        select(func.count()).select_from(Position).where(Position.is_open.is_(True))
    ).scalar_one()
    closed_positions = counts["positions"] - open_positions
    print(f"\npositions: open={open_positions}  closed={closed_positions}")

    print("\nposition_history events by type x is_bootstrap:")
    event_rows = session.execute(
        select(
            PositionHistory.event_type,
            PositionHistory.is_bootstrap,
            func.count(),
        ).group_by(PositionHistory.event_type, PositionHistory.is_bootstrap)
    ).all()
    for event_type, is_bootstrap, count in sorted(event_rows, key=lambda r: (r[0], r[1])):
        print(f"  {event_type:<10} bootstrap={is_bootstrap!s:<5} {count}")

    open_markets = session.execute(
        select(func.count()).select_from(Market).where(Market.closed.is_(False))
    ).scalar_one()
    closed_markets = counts["markets"] - open_markets
    print(f"\nmarkets: open={open_markets}  closed={closed_markets}")


def _print_fresh_event_rate(session, now: datetime) -> None:
    _print_header(f"FRESH EVENT RATE (last {_WINDOW_HOURS}h, non-bootstrap)")

    window_start = now - timedelta(hours=_WINDOW_HOURS)
    rows = session.execute(
        select(PositionHistory.detected_at).where(
            PositionHistory.detected_at >= window_start,
            PositionHistory.is_bootstrap.is_(False),
        )
    ).all()

    buckets = [0] * _WINDOW_HOURS
    for (detected_at,) in rows:
        hours_ago = int((now - detected_at).total_seconds() // 3600)
        bucket = _WINDOW_HOURS - 1 - hours_ago
        if 0 <= bucket < _WINDOW_HOURS:
            buckets[bucket] += 1

    max_count = max(buckets) if buckets else 0
    bar_width = 40
    for i, count in enumerate(buckets):
        hour_label = f"-{_WINDOW_HOURS - i}h" if i < _WINDOW_HOURS - 1 else "now"
        bar_len = int(count / max_count * bar_width) if max_count else 0
        print(f"  {hour_label:>4}  {'#' * bar_len}{' ' * (bar_width - bar_len)}  {count}")

    total = sum(buckets)
    mean = total / _WINDOW_HOURS
    print(f"\ntotal={total}  hourly_mean={mean:.1f}")


def _print_funnel_trends(session) -> None:
    _print_header("FUNNEL TRENDS")

    runs = session.execute(select(ConsensusRun)).scalars().all()
    print(f"total runs: {len(runs)}")

    if not runs:
        return

    stage_stats = []
    for i, stage in enumerate(_FUNNEL_STAGES):
        survivors = [getattr(run, stage) for run in runs]
        mean_survivors = sum(survivors) / len(survivors)
        if i == 0:
            mean_rejected = 0.0
        else:
            prev_stage = _FUNNEL_STAGES[i - 1]
            rejected = [getattr(run, prev_stage) - getattr(run, stage) for run in runs]
            mean_rejected = sum(rejected) / len(rejected)
        stage_stats.append((stage, mean_survivors, mean_rejected))

    ranked = sorted(stage_stats[1:], key=lambda s: s[2], reverse=True)
    name_width = max(len(s[0]) for s in stage_stats)
    print(f"\n{'stage'.ljust(name_width)}  mean_survivors  mean_rejected_at_stage")
    print(f"{stage_stats[0][0].ljust(name_width)}  {stage_stats[0][1]:>14.1f}  {'-':>23}")
    for stage, mean_survivors, mean_rejected in ranked:
        print(f"{stage.ljust(name_width)}  {mean_survivors:>14.1f}  {mean_rejected:>23.1f}")

    total_new = sum(run.new_signals for run in runs)
    total_reinforced = sum(run.reinforced for run in runs)
    print(f"\ntotal new signals: {total_new}")
    print(f"total reinforced signals: {total_reinforced}")


def _print_signals(session, now: datetime) -> None:
    _print_header("SIGNALS")

    status_rows = session.execute(select(Signal.status, func.count()).group_by(Signal.status)).all()
    for status, count in status_rows:
        print(f"  {status}: {count}")

    active = (
        session.execute(
            select(Signal)
            .where(Signal.status == SignalStatus.ACTIVE)
            .order_by(Signal.weighted_score.desc())
        )
        .scalars()
        .all()
    )
    if not active:
        print("\nno ACTIVE signals.")
        return

    print("\nACTIVE signals:")
    for signal in active:
        age = _format_age(now - signal.created_at)
        print(
            f"  [{age:>6}] {signal.title[:50]:<50} outcome={signal.outcome:<10} "
            f"traders={signal.distinct_traders:<3} weighted={signal.weighted_score:.2f} "
            f"value=${signal.combined_entry_value:,.0f} "
            f"entry={signal.average_entry_price:.3f} current={signal.latest_market_price:.3f}"
        )


def _print_trader_scores(session) -> None:
    _print_header("TRADER SCORES")

    tracked_count = session.execute(
        select(func.count()).select_from(Wallet).where(Wallet.is_tracked.is_(True))
    ).scalar_one()

    latest_per_wallet = (
        select(TraderScore.wallet_id, func.max(TraderScore.captured_at).label("latest"))
        .group_by(TraderScore.wallet_id)
        .subquery()
    )
    scores = (
        session.execute(
            select(TraderScore.score)
            .join(
                latest_per_wallet,
                (TraderScore.wallet_id == latest_per_wallet.c.wallet_id)
                & (TraderScore.captured_at == latest_per_wallet.c.latest),
            )
            .order_by(TraderScore.score)
        )
        .scalars()
        .all()
    )

    print(f"tracked wallets: {tracked_count}")
    print(f"scored wallets: {len(scores)}")

    if not scores:
        return

    def _percentile(sorted_scores: list[Decimal], pct: float) -> Decimal:
        if len(sorted_scores) == 1:
            return sorted_scores[0]
        idx = pct * (len(sorted_scores) - 1)
        lo, hi = int(idx), min(int(idx) + 1, len(sorted_scores) - 1)
        frac = Decimal(str(idx - lo))
        return sorted_scores[lo] + (sorted_scores[hi] - sorted_scores[lo]) * frac

    print(
        f"min={scores[0]:.4f}  p25={_percentile(scores, 0.25):.4f}  "
        f"median={_percentile(scores, 0.5):.4f}  p75={_percentile(scores, 0.75):.4f}  "
        f"max={scores[-1]:.4f}"
    )


def _print_data_gaps(session, now: datetime) -> None:
    _print_header("DATA GAPS")

    window_start = now - timedelta(hours=_WINDOW_HOURS)
    rows = session.execute(
        select(PositionHistory.detected_at).where(PositionHistory.detected_at >= window_start)
    ).all()

    hourly_counts = [0] * _WINDOW_HOURS
    for (detected_at,) in rows:
        hours_ago = int((now - detected_at).total_seconds() // 3600)
        bucket = _WINDOW_HOURS - 1 - hours_ago
        if 0 <= bucket < _WINDOW_HOURS:
            hourly_counts[bucket] += 1

    empty_hours = [i for i, count in enumerate(hourly_counts) if count == 0]
    if empty_hours:
        labels = [f"-{_WINDOW_HOURS - i}h" for i in empty_hours]
        print(f"hours with zero position_history rows: {', '.join(labels)}")
    else:
        print("no hours with zero position_history rows - collector had no gaps.")

    tracked_wallets = (
        session.execute(select(Wallet).where(Wallet.is_tracked.is_(True))).scalars().all()
    )
    wallets_with_events = {
        row[0] for row in session.execute(select(PositionHistory.wallet_id).distinct()).all()
    }
    silent_wallets = [w for w in tracked_wallets if w.id not in wallets_with_events]
    if silent_wallets:
        print(f"\ntracked wallets with zero position_history events ({len(silent_wallets)}):")
        for wallet in silent_wallets:
            label = wallet.username or wallet.address
            print(f"  {label}")
    else:
        print("\nno tracked wallet is silent - every one has at least one event.")


def main() -> None:
    now = datetime.now(UTC)
    with db_session() as session:
        _print_heartbeat(session, now)
        _print_volume(session)
        _print_fresh_event_rate(session, now)
        _print_funnel_trends(session)
        _print_signals(session, now)
        _print_trader_scores(session)
        _print_data_gaps(session, now)


if __name__ == "__main__":
    main()
