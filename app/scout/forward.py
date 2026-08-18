"""Stage 2: forward tracking (verification) - see docs/SCOUT_DESIGN.md.

From the moment a wallet becomes CANDIDATE, every new OPENED position it
takes is recorded and its forward CLV tracked, using the exact same
clv_value()/resolve_horizon_clv()/resolve_resolution_clv() pure formula
Workstream 2 already proved correct (app/optimization/clv.py) - reused,
not reimplemented (design flag 1/7). The one real difference: entry_price
here is the wallet's own real PositionHistory.avg_price at its actual
OPENED event, never a simulated/delayed fill the way SignalCLV's is - the
Scout is grading a wallet's real skill, not simulating a trade of its own.

close_ready_windows() is shared with app/scout/decay.py (design flag 9/10):
the exact same window-closing mechanism serves both Stage 2 (promotion,
CI-bound) and Stage 3 (decay, point-estimate) - only what each stage DOES
with a freshly-closed window differs.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config.effective import get_effective_settings
from app.config.settings import Settings
from app.db.models import (
    Market,
    PositionHistory,
    PriceSnapshot,
    ScoutForwardTrade,
    ScoutStageTransition,
    ScoutValidationWindow,
    TraderPipeline,
    TraderPipelineStage,
)
from app.db.session import db_session
from app.optimization.clv import resolve_horizon_clv, resolve_resolution_clv
from app.optimization.event_clustering import cluster_key_for, effective_sample_size
from app.paper.resolution import detect_resolution

logger = logging.getLogger(__name__)

_CONFIDENCE_Z = Decimal("1.96")


@dataclass(frozen=True)
class ForwardConfig:
    """Every Stage-2/3 tunable shared by forward.py and decay.py."""

    clv_horizon_hours: int
    validation_days: int
    min_forward_trades: int
    validation_confirmations: int
    decay_threshold: Decimal
    decay_windows: int
    resolution_price_threshold: Decimal
    clv_fill_max_per_cycle: int
    confidence_z: Decimal = _CONFIDENCE_Z

    @classmethod
    def from_settings(cls, settings: Settings) -> "ForwardConfig":
        return cls(
            clv_horizon_hours=settings.scout_clv_horizon_hours,
            validation_days=settings.scout_validation_days,
            min_forward_trades=settings.scout_min_forward_trades,
            validation_confirmations=settings.scout_validation_confirmations,
            decay_threshold=settings.scout_decay_threshold,
            decay_windows=settings.scout_decay_windows,
            clv_fill_max_per_cycle=settings.scout_clv_fill_max_per_cycle,
            # Not a scout_*-namespaced setting (design flag 8 is about the
            # Scout's own behavioral tunables, not about facts of the
            # market data itself) - app/optimization/clv.py's own
            # SignalClvConfig reuses this exact same settings field for the
            # identical reason: what counts as "resolved to 0 or 1" is a
            # market-level fact, not a per-consumer choice.
            resolution_price_threshold=settings.paper_resolution_price_threshold,
        )


@dataclass(frozen=True)
class ForwardTradeRecord:
    """One forward-tracked trade's CLV plus its clustering join key - plain
    values, not an ORM row, so clv_window_stats stays DB-free.
    """

    condition_id: str
    event_cluster_id: str | None
    clv: Decimal


@dataclass(frozen=True)
class WindowStats:
    """A window's aggregate forward-CLV stats - n, mean, and a 95% CI on
    the mean via the normal approximation (mean +/- z * stderr) - CLV
    values are continuous price deltas, not a binomial proportion, so
    wilson_interval doesn't apply here; this is the direct analogue for a
    sample mean, at the same confidence level the rest of this project
    already uses everywhere else (Wilson's own z=1.96).

    effective_n is the number of distinct event clusters the window's
    trades actually span (app/optimization/event_clustering.py) - a wallet
    that took 8 positions across one tournament is one real bet observed
    8 times, not 8 independent confirmations of its forward skill.
    window_ready_to_close and evaluate_window both gate on effective_n,
    not n.
    """

    n: int
    effective_n: int
    avg_clv: Decimal
    ci_low: Decimal
    ci_high: Decimal


class WindowVerdict:
    """Not a StrEnum - just two named constants for evaluate_window()'s
    return value, since nothing here is persisted as a string (passed is
    a plain bool on ScoutValidationWindow).
    """

    PASS = "PASS"
    FAIL = "FAIL"


def clv_window_stats(
    records: list[ForwardTradeRecord], confidence_z: Decimal = _CONFIDENCE_Z
) -> WindowStats:
    """Mean forward CLV and its 95% CI over one window's trades. n=0 scores
    a neutral (0, 0, 0) - nothing to report yet, never an error. n=1 has no
    usable variance estimate, so the CI collapses to the point estimate
    itself (deliberately wide-open, not a fabricated interval) - in
    practice this never matters, since a window never closes below
    SCOUT_MIN_FORWARD_TRADES anyway (see window_ready_to_close).
    """
    n = len(records)
    effective_n = effective_sample_size(
        [cluster_key_for(r.condition_id, r.event_cluster_id) for r in records]
    )
    if n == 0:
        return WindowStats(
            n=0, effective_n=0, avg_clv=Decimal(0), ci_low=Decimal(0), ci_high=Decimal(0)
        )
    clv_values = [r.clv for r in records]
    mean = sum(clv_values, Decimal(0)) / Decimal(n)
    if n == 1:
        return WindowStats(n=1, effective_n=effective_n, avg_clv=mean, ci_low=mean, ci_high=mean)
    variance = sum(((v - mean) ** 2 for v in clv_values), Decimal(0)) / Decimal(n - 1)
    stderr = variance.sqrt() / Decimal(n).sqrt()
    margin = confidence_z * stderr
    return WindowStats(
        n=n, effective_n=effective_n, avg_clv=mean, ci_low=mean - margin, ci_high=mean + margin
    )


def window_ready_to_close(
    window_start: datetime,
    now: datetime,
    validation_days: int,
    effective_trade_count: int,
    min_forward_trades: int,
) -> bool:
    """A window closes once BOTH its calendar span has elapsed AND it has
    enough forward trades - whichever condition is slower to satisfy sets
    the actual close time (docs/SCOUT_DESIGN.md: "keep tracking until it
    does; don't judge on a thin forward sample"). Gated on effective_trade_count
    (distinct event clusters, app/optimization/event_clustering.py), not
    the raw count - 8 forward trades in one tournament is one real
    confirmation of forward skill, not 8.
    """
    elapsed = now - window_start
    return (
        elapsed >= timedelta(days=validation_days) and effective_trade_count >= min_forward_trades
    )


def evaluate_window(stats: WindowStats) -> str:
    """Stage 2's promotion criterion: the CI LOWER bound must clear zero -
    hard to fake with a lucky trade or two, exactly as docs/SCOUT_DESIGN.md
    specifies. Stage 3's decay check does NOT use this - it reads
    avg_forward_clv directly (design flag 10).
    """
    return WindowVerdict.PASS if stats.ci_low > Decimal(0) else WindowVerdict.FAIL


def _entry_price(row: PositionHistory) -> Decimal:
    """avg_price, falling back to cur_price - same convention
    app/signals/generator.py's _entry_price() already establishes for the
    identical PositionHistory shape.
    """
    if row.avg_price and row.avg_price > 0:
        return row.avg_price
    return row.cur_price


def _nearest_price_at_or_after(
    session: Session, condition_id: str, asset: str, at: datetime
) -> Decimal | None:
    return session.execute(
        select(PriceSnapshot.price)
        .where(
            PriceSnapshot.condition_id == condition_id,
            PriceSnapshot.asset == asset,
            PriceSnapshot.captured_at >= at,
        )
        .order_by(PriceSnapshot.captured_at.asc())
        .limit(1)
    ).scalar_one_or_none()


def _latest_price(session: Session, condition_id: str, asset: str) -> Decimal | None:
    return session.execute(
        select(PriceSnapshot.price)
        .where(PriceSnapshot.condition_id == condition_id, PriceSnapshot.asset == asset)
        .order_by(PriceSnapshot.captured_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def _record_new_forward_trades(session: Session, now: datetime) -> int:
    """Every OPENED, non-bootstrap event for a CANDIDATE/WATCHLIST wallet,
    not already recorded, becomes a new scout_forward_trades row - dedup by
    position_history_id (exact), floored by the wallet's own
    candidate_since anchor so a re-entered wallet never picks up trades
    from before its current attempt began.
    """
    tracked = (
        session.execute(
            select(TraderPipeline).where(
                TraderPipeline.stage.in_(
                    [TraderPipelineStage.CANDIDATE, TraderPipelineStage.WATCHLIST]
                )
            )
        )
        .scalars()
        .all()
    )
    if not tracked:
        return 0

    wallet_ids = [row.wallet_id for row in tracked]
    since_by_wallet = {row.wallet_id: _candidate_since(row) for row in tracked}

    already_tracked_ids = set(
        session.execute(
            select(ScoutForwardTrade.position_history_id).where(
                ScoutForwardTrade.wallet_id.in_(wallet_ids)
            )
        ).scalars()
    )

    candidates = (
        session.execute(
            select(PositionHistory).where(
                PositionHistory.wallet_id.in_(wallet_ids),
                PositionHistory.event_type == "OPENED",
                PositionHistory.is_bootstrap.is_(False),
            )
        )
        .scalars()
        .all()
    )

    created = 0
    for row in candidates:
        if row.id in already_tracked_ids:
            continue
        since = since_by_wallet.get(row.wallet_id)
        if since is None or row.detected_at < since:
            continue
        session.add(
            ScoutForwardTrade(
                wallet_id=row.wallet_id,
                position_history_id=row.id,
                condition_id=row.condition_id,
                asset=row.asset,
                entry_price=_entry_price(row),
                entry_at=row.detected_at,
                computed_at=now,
            )
        )
        created += 1
    return created


def _fill_forward_clv(session: Session, config: ForwardConfig, now: datetime) -> tuple[int, int]:
    """Same two-pass fill shape app/optimization/clv.py's run_clv_updates
    uses for signal_clv - horizon once SCOUT_CLV_HORIZON_HOURS has elapsed
    and a price exists there, resolution once the market closes and
    resolves unambiguously.

    Both passes filter in SQL, not in Python over an unconditionally
    fetched "everything still missing either field" set - a market that
    never resolves promptly (the common case; most markets stay open for
    weeks) used to leave its forward trade in that set on EVERY cycle
    forever, so the working set only ever grew. That's the same unbounded-
    worklist shape that hung app/collectors/markets.py earlier - see that
    fix's commit for the identical lesson: push the filter that actually
    bounds the set into the query, don't carry the backlog in Python.
    """
    horizon_filled = resolution_filled = 0

    # Capped per cycle - as of this fix, production had a 35,802-row
    # horizon-fill backlog accumulated under the old unbounded query (see
    # this fix's commit). Even correctly SQL-filtered, a one-time backlog
    # that size still means one DB round-trip per row
    # (_nearest_price_at_or_after below) - capping keeps any single cycle
    # bounded and lets the backlog drain over several cycles instead of
    # one that runs long enough to look like the same hang again.
    horizon_cutoff = now - timedelta(hours=config.clv_horizon_hours)
    horizon_pending = (
        session.execute(
            select(ScoutForwardTrade)
            .where(
                ScoutForwardTrade.price_at_horizon.is_(None),
                ScoutForwardTrade.entry_at <= horizon_cutoff,
            )
            .order_by(ScoutForwardTrade.entry_at.asc())
            .limit(config.clv_fill_max_per_cycle)
        )
        .scalars()
        .all()
    )
    for row in horizon_pending:
        horizon_at = row.entry_at + timedelta(hours=config.clv_horizon_hours)
        price = _nearest_price_at_or_after(session, row.condition_id, row.asset, horizon_at)
        clv = resolve_horizon_clv(row.entry_price, price)
        if clv is not None:
            row.price_at_horizon = price
            row.clv_horizon = clv
            row.computed_at = now
            horizon_filled += 1

    # Only trades whose market has ACTUALLY closed - a still-open market's
    # trade is excluded by this join, not just skipped in Python, so it
    # never re-enters the working set until it's genuinely relevant.
    resolution_pending = (
        session.execute(
            select(ScoutForwardTrade)
            .join(Market, Market.condition_id == ScoutForwardTrade.condition_id)
            .where(
                ScoutForwardTrade.price_at_resolution.is_(None),
                Market.closed.is_(True),
            )
            .limit(config.clv_fill_max_per_cycle)
        )
        .scalars()
        .all()
    )
    for row in resolution_pending:
        latest = _latest_price(session, row.condition_id, row.asset)
        resolution = detect_resolution(True, latest, config.resolution_price_threshold)
        result = resolve_resolution_clv(row.entry_price, resolution)
        if result is not None:
            row.price_at_resolution = result.price_at_resolution
            row.clv_resolution = result.clv_resolution
            row.computed_at = now
            resolution_filled += 1

    return horizon_filled, resolution_filled


def _candidate_since(pipeline_row: TraderPipeline) -> datetime | None:
    raw = (pipeline_row.metrics or {}).get("candidate_since")
    return datetime.fromisoformat(raw) if raw else None


def _current_window_start(
    session: Session, wallet_id: int, pipeline_row: TraderPipeline
) -> datetime | None:
    """The open window's start: right where the last completed window
    ended, or (for a wallet's very first window) its candidate_since
    anchor - never entered_stage_at directly, since that gets bumped on
    every promotion and would silently truncate window 2+.
    """
    last_window_end = session.execute(
        select(ScoutValidationWindow.window_ended_at)
        .where(ScoutValidationWindow.wallet_id == wallet_id)
        .order_by(ScoutValidationWindow.window_ended_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if last_window_end is not None:
        return last_window_end
    return _candidate_since(pipeline_row)


def close_ready_windows(
    session: Session,
    config: ForwardConfig,
    now: datetime,
    pipeline_by_wallet: dict[int, TraderPipeline],
) -> list[ScoutValidationWindow]:
    """For every wallet in pipeline_by_wallet, close its current forward-
    tracking window if ready (docs/SCOUT_DESIGN.md flag 9/10) - shared by
    Stage 2 (called for CANDIDATE/WATCHLIST) and Stage 3 (called for
    VALIDATED/DECAYING). Only forward trades with a known clv_horizon count
    toward a window - one still pending simply doesn't count yet, the same
    "don't judge on a thin sample" principle applied to individual trades,
    not just the window total.
    """
    closed: list[ScoutValidationWindow] = []
    for wallet_id, pipeline_row in pipeline_by_wallet.items():
        window_start = _current_window_start(session, wallet_id, pipeline_row)
        if window_start is None:
            continue

        rows = session.execute(
            select(
                ScoutForwardTrade.condition_id,
                Market.event_cluster_id,
                ScoutForwardTrade.clv_horizon,
            )
            .outerjoin(Market, Market.condition_id == ScoutForwardTrade.condition_id)
            .where(
                ScoutForwardTrade.wallet_id == wallet_id,
                ScoutForwardTrade.entry_at >= window_start,
                ScoutForwardTrade.entry_at < now,
                ScoutForwardTrade.clv_horizon.is_not(None),
            )
        ).all()
        records = [
            ForwardTradeRecord(
                condition_id=row.condition_id,
                event_cluster_id=row.event_cluster_id,
                clv=row.clv_horizon,
            )
            for row in rows
        ]

        stats = clv_window_stats(records, config.confidence_z)

        if not window_ready_to_close(
            window_start, now, config.validation_days, stats.effective_n, config.min_forward_trades
        ):
            continue

        verdict = evaluate_window(stats)
        row = ScoutValidationWindow(
            wallet_id=wallet_id,
            window_started_at=window_start,
            window_ended_at=now,
            forward_trade_count=stats.n,
            effective_forward_trade_count=stats.effective_n,
            avg_forward_clv=stats.avg_clv,
            ci_low=stats.ci_low,
            ci_high=stats.ci_high,
            passed=(verdict == WindowVerdict.PASS),
        )
        session.add(row)
        closed.append(row)
    return closed


def _transition(
    session: Session,
    pipeline_row: TraderPipeline,
    new_stage: str,
    now: datetime,
    reason: str,
    **extra_metrics: object,
) -> None:
    """Mutates the current-state row AND appends the append-only
    transition-history row the dashboard drill-down reads (docs/
    SCOUT_DESIGN.md flag 9's reasoning, extended to stage history too).
    from_stage is captured before mutating pipeline_row.stage - reading it
    after would always see new_stage, since this is the same in-memory
    object.
    """
    from_stage = pipeline_row.stage
    pipeline_row.stage = new_stage
    pipeline_row.entered_stage_at = now
    pipeline_row.updated_at = now
    pipeline_row.metrics = {**(pipeline_row.metrics or {}), **extra_metrics}
    session.add(
        ScoutStageTransition(
            wallet_id=pipeline_row.wallet_id,
            from_stage=from_stage,
            to_stage=new_stage,
            reason=reason,
            transitioned_at=now,
        )
    )


def _count_passing_windows(session: Session, wallet_id: int) -> int:
    """How many confirmations this wallet is carrying. Every window for a
    wallet still in CANDIDATE/WATCHLIST is necessarily passing - any
    failing window moves it straight to REJECTED (see run_forward_tracking)
    and forward-tracking stops for it - so this is a plain count, not a
    "count until the first failure" walk.
    """
    return len(
        list(
            session.execute(
                select(ScoutValidationWindow.id).where(
                    ScoutValidationWindow.wallet_id == wallet_id,
                    ScoutValidationWindow.passed.is_(True),
                )
            ).scalars()
        )
    )


@dataclass(frozen=True)
class ForwardTrackingSummary:
    recorded: int
    horizon_filled: int
    resolution_filled: int
    promoted: int
    rejected: int


def run_forward_tracking(settings: Settings) -> ForwardTrackingSummary:
    """The full Stage 2 pass: record new forward trades, fill in CLV, close
    any ready windows for CANDIDATE/WATCHLIST wallets, and apply promotion
    or rejection. Logged at INFO on every actual stage transition - a
    window that isn't ready yet, or one that passes without triggering a
    transition (WATCHLIST still short of its confirmation count), is not
    logged as a transition because it isn't one.
    """
    config = ForwardConfig.from_settings(settings)
    now = datetime.now(UTC)

    with db_session() as session:
        recorded = _record_new_forward_trades(session, now)
        horizon_filled, resolution_filled = _fill_forward_clv(session, config, now)

        tracked = (
            session.execute(
                select(TraderPipeline).where(
                    TraderPipeline.stage.in_(
                        [TraderPipelineStage.CANDIDATE, TraderPipelineStage.WATCHLIST]
                    )
                )
            )
            .scalars()
            .all()
        )
        pipeline_by_wallet = {row.wallet_id: row for row in tracked}
        closed_windows = close_ready_windows(session, config, now, pipeline_by_wallet)

        promoted = rejected = 0
        for window in closed_windows:
            pipeline_row = pipeline_by_wallet[window.wallet_id]
            window_metrics = {
                "last_window_avg_clv": str(window.avg_forward_clv),
                "last_window_ci_low": str(window.ci_low),
                "last_window_ci_high": str(window.ci_high),
                "last_window_n": window.forward_trade_count,
            }

            from_stage = pipeline_row.stage
            window_summary = (
                f"forward CLV avg={window.avg_forward_clv:.4f} "
                f"CI=[{window.ci_low:.4f}, {window.ci_high:.4f}] n={window.forward_trade_count}"
            )

            if not window.passed:
                _transition(
                    session,
                    pipeline_row,
                    TraderPipelineStage.REJECTED,
                    now,
                    reason=f"forward CLV flat/negative: {window_summary}",
                    **window_metrics,
                )
                rejected += 1
                logger.info(
                    "scout: wallet %d %s -> REJECTED - %s",
                    window.wallet_id,
                    from_stage,
                    window_summary,
                )
                continue

            if pipeline_row.stage == TraderPipelineStage.CANDIDATE:
                _transition(
                    session,
                    pipeline_row,
                    TraderPipelineStage.WATCHLIST,
                    now,
                    reason=f"forward CLV window passed: {window_summary}",
                    **window_metrics,
                )
                promoted += 1
                logger.info(
                    "scout: wallet %d CANDIDATE -> WATCHLIST (%s)",
                    window.wallet_id,
                    window_summary,
                )
            else:
                confirmations = _count_passing_windows(session, window.wallet_id)
                if confirmations >= config.validation_confirmations:
                    _transition(
                        session,
                        pipeline_row,
                        TraderPipelineStage.VALIDATED,
                        now,
                        reason=(
                            f"confirmed {confirmations}/{config.validation_confirmations} "
                            f"consecutive passing windows: {window_summary}"
                        ),
                        **window_metrics,
                    )
                    promoted += 1
                    logger.info(
                        "scout: wallet %d WATCHLIST -> VALIDATED (%d/%d confirmations)",
                        window.wallet_id,
                        confirmations,
                        config.validation_confirmations,
                    )
                else:
                    pipeline_row.metrics = {**(pipeline_row.metrics or {}), **window_metrics}
                    pipeline_row.updated_at = now
                    logger.info(
                        "scout: wallet %d WATCHLIST window passed (%d/%d confirmations, "
                        "still accumulating)",
                        window.wallet_id,
                        confirmations,
                        config.validation_confirmations,
                    )

    summary = ForwardTrackingSummary(
        recorded=recorded,
        horizon_filled=horizon_filled,
        resolution_filled=resolution_filled,
        promoted=promoted,
        rejected=rejected,
    )
    logger.info(
        "scout: forward tracking recorded=%d horizon_filled=%d resolution_filled=%d "
        "promoted=%d rejected=%d",
        summary.recorded,
        summary.horizon_filled,
        summary.resolution_filled,
        summary.promoted,
        summary.rejected,
    )
    return summary


async def run_forward_tracking_job() -> None:
    """Scheduler entrypoint - same asyncio.to_thread + get_effective_settings
    pattern every periodic job in this project uses.
    """
    settings = get_effective_settings()
    await asyncio.to_thread(run_forward_tracking, settings)
