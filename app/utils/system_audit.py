"""Shared system-audit checks: verifies code presence AND live data/recency
evidence for every subsystem, exactly as the manual audit did by hand.

Every check here is one of two kinds:
  - a "code" check: calls the actual pure function (diffing/consensus/fills/
    risk) with synthetic input and asserts the real behavior it's supposed
    to have, so a regression that silently guts the logic gets caught even
    if the table it writes to still looks fine.
  - a "data" check: queries production for row counts, timestamps, and
    breakdowns, so a job that "succeeds" every run without ever writing
    anything useful (the consensus job once hung silently for 10+ hours)
    doesn't get mistaken for healthy.

Used by both scripts/system_audit.py (the CLI, which also runs the two
HTTP-based checks against a live dashboard) and the dashboard's own
/system-health page (DB-only - a page checking its own HTTP reachability by
calling itself is circular, so that half stays CLI-only; see run_checks()).

Read-only: every DB access below is a SELECT. Never runs migrations, never
writes an override, never touches runtime_overrides or any trading table.
Safe against production.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import func, select

from app.collectors.diffing import diff_positions
from app.collectors.schemas import PositionEntry
from app.config.settings import Settings
from app.consensus.engine import (
    CandidateGroup,
    ConsensusConfig,
    ContributorEvent,
    MarketState,
    Rejection,
    RejectionReason,
    SignalDraft,
    evaluate_group,
)
from app.db.models import (
    ClusterBanditState,
    ConsensusRun,
    JobHeartbeat,
    LeaderboardSnapshot,
    MarketHistory,
    PaperPortfolio,
    PaperTrade,
    PaperTradeStatus,
    PositionHistory,
    PriceSnapshot,
    RiskEvent,
    ScoutForwardTrade,
    ScoutStageTransition,
    ScoutValidationWindow,
    Signal,
    SignalCLV,
    SignalStatus,
    TraderCategoryScore,
    TraderPipeline,
    WalletCluster,
    WalletCrowdedness,
)
from app.db.session import db_session
from app.paper.fills import (
    BookLevel,
    FillConfig,
    FillMethod,
    FillRequest,
    MarketSnapshot,
    compute_fill,
    walk_the_book,
)
from app.risk.rules import (
    PortfolioState,
    ProposedTrade,
    RiskAction,
    RiskRule,
    RiskRuleConfig,
    rule_max_open_positions,
)

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]

# fresh: age <= 1x interval. stale: <= 3x. dead: beyond that, or never
# written - same thresholds app/dashboard/queries.py's Heartbeat/JobHealth
# use, so this module and the dashboard's other pages never disagree about
# what "stale" means for the same row.
_FRESH_MULTIPLIER = 1
_STALE_MULTIPLIER = 3

_SMOKE_NOW = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class CheckResult:
    section: str
    name: str
    status: str  # PASS | WARN | FAIL
    evidence: str


def _age_status(age_seconds: float | None, interval_seconds: int) -> str:
    if age_seconds is None:
        return "FAIL"
    if age_seconds <= interval_seconds * _FRESH_MULTIPLIER:
        return "PASS"
    if age_seconds <= interval_seconds * _STALE_MULTIPLIER:
        return "WARN"
    return "FAIL"


def _fmt_age(now: datetime, ts: datetime | None) -> str:
    if ts is None:
        return "never"
    age = (now - ts).total_seconds()
    if age < 120:
        return f"{age:.0f}s ago"
    if age < 7200:
        return f"{age / 60:.1f}m ago"
    return f"{age / 3600:.1f}h ago"


# --- SCHEMA ---------------------------------------------------------------


def check_schema() -> CheckResult:
    """DB alembic revision vs code head. This exact drift - a service
    deployed against a database whose migrations were never applied -
    silently killed the Scout for 10 days: every job tick raised
    UndefinedTable, was logged, and retried on schedule forever,
    indistinguishable from a healthy idle service. Checked first, flagged
    loudest, for that reason.
    """
    section = "SCHEMA"
    try:
        cfg = Config(str(_REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(_REPO_ROOT / "alembic"))
        script = ScriptDirectory.from_config(cfg)
        code_heads = set(script.get_heads())

        with db_session() as session:
            db_heads = set(MigrationContext.configure(session.connection()).get_current_heads())

        if db_heads == code_heads:
            return CheckResult(
                section, "alembic revision", "PASS", f"db and code both at {sorted(db_heads)}"
            )

        missing = [
            rev.revision for rev in script.iterate_revisions(list(code_heads), list(db_heads))
        ]
        return CheckResult(
            section,
            "alembic revision",
            "FAIL",
            f"*** SCHEMA DRIFT *** db is at {sorted(db_heads) or 'no revisions applied'}, "
            f"code expects {sorted(code_heads)}. Missing migrations: {missing}. "
            f"Run: uv run alembic upgrade head",
        )
    except Exception as exc:
        return CheckResult(
            section, "alembic revision", "FAIL", f"could not compare revisions: {exc!r}"
        )


# --- COLLECTORS -------------------------------------------------------------


def _sample_position_entry(asset: str, size: str) -> PositionEntry:
    return PositionEntry(
        proxy_wallet="0xw",
        asset=asset,
        condition_id="0xc",
        size=Decimal(size),
        avg_price=Decimal("0.5"),
        initial_value=Decimal("5"),
        current_value=Decimal("5"),
        cash_pnl=Decimal("0"),
        percent_pnl=Decimal("0"),
        cur_price=Decimal("0.5"),
        redeemable=False,
        title="t",
        event_slug="e",
        outcome="Yes",
        outcome_index=0,
        negative_risk=False,
    )


def check_collectors_code() -> CheckResult:
    """diff_positions() smoke test - OPENED/INCREASED/DECREASED/CLOSED all
    correctly derived from a synthetic before/after snapshot, plus an
    unchanged asset correctly producing no event at all.
    """
    section = "COLLECTORS"
    try:
        previous = {
            "unchanged": Decimal("10"),
            "increased": Decimal("5"),
            "decreased": Decimal("20"),
            "closed": Decimal("7"),
        }
        current = [
            _sample_position_entry("new_asset", "3"),
            _sample_position_entry("unchanged", "10"),
            _sample_position_entry("increased", "8"),
            _sample_position_entry("decreased", "12"),
        ]
        events = diff_positions(previous, current)
        types = {e.asset: e.event_type for e in events}
        expected = {
            "new_asset": "OPENED",
            "increased": "INCREASED",
            "decreased": "DECREASED",
            "closed": "CLOSED",
        }
        if types != expected or "unchanged" in types:
            return CheckResult(
                section,
                "diff_positions()",
                "FAIL",
                f"expected {expected} and no event for 'unchanged', got {types}",
            )
        return CheckResult(
            section,
            "diff_positions()",
            "PASS",
            f"OPENED/INCREASED/DECREASED/CLOSED all correctly derived, "
            f"unchanged asset silent: {types}",
        )
    except Exception as exc:
        return CheckResult(section, "diff_positions()", "FAIL", f"raised {exc!r}")


def check_collectors_data(settings: Settings) -> list[CheckResult]:
    section = "COLLECTORS"
    now = datetime.now(UTC)
    try:
        with db_session() as session:
            leaderboard_latest = session.execute(
                select(func.max(LeaderboardSnapshot.captured_at))
            ).scalar_one()
            positions_latest = session.execute(
                select(func.max(PositionHistory.detected_at))
            ).scalar_one()
            markets_latest = session.execute(
                select(func.max(MarketHistory.captured_at))
            ).scalar_one()
    except Exception as exc:
        return [CheckResult(section, "collector tables", "FAIL", f"could not query: {exc!r}")]

    rows = (
        ("leaderboard_snapshots", leaderboard_latest, settings.leaderboard_interval_seconds),
        ("position_history", positions_latest, settings.positions_interval_seconds),
        ("market_history", markets_latest, settings.markets_interval_seconds),
    )
    out = []
    for name, latest, interval in rows:
        age = (now - latest).total_seconds() if latest else None
        out.append(
            CheckResult(
                section,
                f"{name} freshness",
                _age_status(age, interval),
                f"latest={_fmt_age(now, latest)} (interval={interval}s)",
            )
        )
    return out


# --- CONSENSUS --------------------------------------------------------------


def check_consensus_code() -> CheckResult:
    """evaluate_group() smoke test - the 8-stage filter chain intact: a
    strong candidate (4 independent wallets, well above every threshold)
    clears every filter; a single-wallet candidate is rejected at BREADTH
    specifically, not some other stage.
    """
    section = "CONSENSUS"
    try:
        config = ConsensusConfig(
            consensus_freshness_hours=48,
            consensus_include_increases=True,
            consensus_min_traders=3,
            consensus_min_weighted_score=Decimal("1.0"),
            consensus_min_combined_value_usd=Decimal("500"),
            signal_min_liquidity_usd=Decimal("5000"),
            signal_min_volume_24h_usd=Decimal("1000"),
            signal_price_min=Decimal("0.05"),
            signal_price_max=Decimal("0.95"),
            signal_max_spread=Decimal("0.05"),
            signal_min_hours_to_end=1,
        )
        market = MarketState(
            exists=True,
            closed=False,
            accepting_orders=True,
            end_date=_SMOKE_NOW + timedelta(days=5),
            liquidity=Decimal("50000"),
            volume_24h=Decimal("10000"),
            spread=Decimal("0.01"),
            current_price=Decimal("0.5"),
        )

        def event(address: str) -> ContributorEvent:
            return ContributorEvent(
                address=address,
                username=None,
                weight=Decimal("1.0"),
                event_type="OPENED",
                acted_size=Decimal("1000"),
                entry_price=Decimal("0.5"),
                detected_at=_SMOKE_NOW,
            )

        strong = CandidateGroup(
            condition_id="0xstrong",
            asset="a",
            outcome="Yes",
            title="t",
            event_slug="e",
            events=[event(f"0x{i}") for i in range(4)],
        )
        result = evaluate_group(strong, market, config, _SMOKE_NOW, cluster_of={})
        if not isinstance(result, SignalDraft):
            return CheckResult(
                section,
                "evaluate_group()",
                "FAIL",
                f"strong candidate should clear all 8 filters, got {result}",
            )

        thin = CandidateGroup(
            condition_id="0xthin",
            asset="a",
            outcome="Yes",
            title="t",
            event_slug="e",
            events=[event("0x1")],
        )
        result2 = evaluate_group(thin, market, config, _SMOKE_NOW, cluster_of={})
        if not (isinstance(result2, Rejection) and result2.reason == RejectionReason.BREADTH):
            return CheckResult(
                section,
                "evaluate_group()",
                "FAIL",
                f"single-wallet candidate should fail at BREADTH, got {result2}",
            )

        return CheckResult(
            section,
            "evaluate_group()",
            "PASS",
            "8-stage filter chain intact: strong candidate -> SignalDraft, "
            "thin candidate -> BREADTH rejection",
        )
    except Exception as exc:
        return CheckResult(section, "evaluate_group()", "FAIL", f"raised {exc!r}")


def check_consensus_data(settings: Settings) -> list[CheckResult]:
    section = "CONSENSUS"
    now = datetime.now(UTC)
    try:
        with db_session() as session:
            latest_run = session.execute(select(func.max(ConsensusRun.executed_at))).scalar_one()
            status_counts = dict(
                session.execute(select(Signal.status, func.count()).group_by(Signal.status)).all()
            )
            stale_active = session.execute(
                select(func.count())
                .select_from(Signal)
                .where(Signal.status == SignalStatus.ACTIVE, Signal.expires_at < now)
            ).scalar_one()
    except Exception as exc:
        return [
            CheckResult(section, "consensus_runs / signals", "FAIL", f"could not query: {exc!r}")
        ]

    return [
        CheckResult(
            section,
            "consensus_runs freshness",
            _age_status(
                (now - latest_run).total_seconds() if latest_run else None,
                settings.consensus_interval_seconds,
            ),
            f"latest={_fmt_age(now, latest_run)} (interval={settings.consensus_interval_seconds}s)",
        ),
        CheckResult(
            section,
            "signals by status",
            "PASS" if status_counts else "WARN",
            ", ".join(f"{k}={v}" for k, v in sorted(status_counts.items())) or "no signals exist",
        ),
        CheckResult(
            section,
            "TTL expiry correctness",
            "PASS" if stale_active == 0 else "FAIL",
            f"{stale_active} ACTIVE signal(s) already past expires_at"
            if stale_active
            else "0 ACTIVE signals past their expires_at",
        ),
    ]


# --- PAPER TRADING -----------------------------------------------------------


def _fill_config() -> FillConfig:
    return FillConfig(
        entry_delay_seconds=30,
        slippage_k=Decimal("0.5"),
        slippage_max=Decimal("0.15"),
        no_delayed_snapshot_penalty=Decimal("0.05"),
        max_entry_price_drift=Decimal("0.15"),
        max_book_depth_fraction=Decimal("0.20"),
    )


def check_paper_code() -> list[CheckResult]:
    """compute_fill() smoke tests across both fill paths:
      - no book available: falls back to ask + slippage, never mid or
        better than ask (the original guarantee).
      - a book is available: walks it for the real size-weighted price,
        tagged FillMethod.BOOK_WALK, and an order too large for the book
        (beyond the depth cap) is honestly rejected as NO_LIQUIDITY rather
        than silently estimated.
    This is the model that determines whether every paper P&L number in
    this project is honest.
    """
    section = "PAPER"
    config = _fill_config()
    ask, bid, mid = Decimal("0.60"), Decimal("0.50"), Decimal("0.55")
    current = MarketSnapshot(
        captured_at=_SMOKE_NOW, ask=ask, bid=bid, liquidity=Decimal("1000000000"), price=mid
    )
    delayed = MarketSnapshot(
        captured_at=_SMOKE_NOW + timedelta(seconds=30),
        ask=ask,
        bid=bid,
        liquidity=None,
        price=mid,
    )
    now = _SMOKE_NOW + timedelta(seconds=30)
    results: list[CheckResult] = []

    # --- no book: estimated fallback, unchanged guarantee ---
    try:
        request = FillRequest(
            signal_price=ask, order_notional=Decimal("10"), detected_at=_SMOKE_NOW
        )
        result = compute_fill(request, [current, delayed], config, now=now)
        if not result.filled or result.fill_price is None:
            results.append(
                CheckResult(
                    section,
                    "compute_fill() estimated",
                    "FAIL",
                    f"expected a fill, got reason={result.reason}",
                )
            )
        elif result.fill_price < ask or result.fill_price <= mid:
            results.append(
                CheckResult(
                    section,
                    "compute_fill() estimated",
                    "FAIL",
                    f"fill_price {result.fill_price} not ask-based (ask={ask} mid={mid})",
                )
            )
        elif result.fill_method != FillMethod.ESTIMATED:
            results.append(
                CheckResult(
                    section,
                    "compute_fill() estimated",
                    "FAIL",
                    f"expected fill_method=ESTIMATED with no book, got {result.fill_method}",
                )
            )
        else:
            results.append(
                CheckResult(
                    section,
                    "compute_fill() estimated",
                    "PASS",
                    f"fill_price={result.fill_price} ask-based (+slippage), never mid={mid}, "
                    f"fill_method={result.fill_method}",
                )
            )
    except Exception as exc:
        results.append(CheckResult(section, "compute_fill() estimated", "FAIL", f"raised {exc!r}"))

    # --- book available: real size-weighted walk ---
    try:
        # Thin top level so a $9 order genuinely spills into the second one
        # (proves multi-level walking, not just a single-level lookup), and
        # a deep enough total book that $9 still clears the 20% depth cap.
        book = [
            BookLevel(price=Decimal("0.60"), size=Decimal("5")),
            BookLevel(price=Decimal("0.61"), size=Decimal("1000")),
        ]
        # Compares against walk_the_book()'s own output directly rather than
        # a hand-expanded number, so this proves compute_fill() actually
        # delegates to it (not just returns *a* price) without duplicating
        # its arithmetic here.
        request = FillRequest(signal_price=ask, order_notional=Decimal("9"), detected_at=_SMOKE_NOW)
        result = compute_fill(request, [current, delayed], config, now=now, book=book)
        expected = walk_the_book(book, Decimal("9"), config.max_book_depth_fraction)
        if not result.filled or result.fill_method != FillMethod.BOOK_WALK:
            results.append(
                CheckResult(
                    section,
                    "compute_fill() book-walk",
                    "FAIL",
                    f"expected a BOOK_WALK fill, got filled={result.filled} "
                    f"method={result.fill_method}",
                )
            )
        elif result.fill_price != expected:
            results.append(
                CheckResult(
                    section,
                    "compute_fill() book-walk",
                    "FAIL",
                    f"fill_price {result.fill_price} != walk_the_book() {expected}",
                )
            )
        else:
            results.append(
                CheckResult(
                    section,
                    "compute_fill() book-walk",
                    "PASS",
                    f"fill_price={result.fill_price} matches walk_the_book() exactly, "
                    f"fill_method={result.fill_method}",
                )
            )
    except Exception as exc:
        results.append(CheckResult(section, "compute_fill() book-walk", "FAIL", f"raised {exc!r}"))

    # --- book too thin: honest rejection, never a silent estimate ---
    try:
        thin_book = [BookLevel(price=Decimal("0.60"), size=Decimal("1"))]
        request = FillRequest(
            signal_price=ask, order_notional=Decimal("1000"), detected_at=_SMOKE_NOW
        )
        result = compute_fill(request, [current, delayed], config, now=now, book=thin_book)
        if result.filled or result.reason.value != "NO_LIQUIDITY":
            results.append(
                CheckResult(
                    section,
                    "compute_fill() thin book",
                    "FAIL",
                    f"expected NO_LIQUIDITY rejection, got filled={result.filled} "
                    f"reason={result.reason}",
                )
            )
        else:
            results.append(
                CheckResult(
                    section,
                    "compute_fill() thin book",
                    "PASS",
                    "an order too large for the visible book is rejected, not silently estimated",
                )
            )
    except Exception as exc:
        results.append(CheckResult(section, "compute_fill() thin book", "FAIL", f"raised {exc!r}"))

    return results


def check_paper_data() -> list[CheckResult]:
    section = "PAPER"
    try:
        with db_session() as session:
            active_portfolios = session.execute(
                select(func.count())
                .select_from(PaperPortfolio)
                .where(PaperPortfolio.is_active.is_(True))
            ).scalar_one()
            closed_count = session.execute(
                select(func.count())
                .select_from(PaperTrade)
                .where(PaperTrade.status == PaperTradeStatus.CLOSED)
            ).scalar_one()
            exit_reasons = dict(
                session.execute(
                    select(PaperTrade.exit_reason, func.count())
                    .where(PaperTrade.status == PaperTradeStatus.CLOSED)
                    .group_by(PaperTrade.exit_reason)
                ).all()
            )
            filled_total = session.execute(
                select(func.count())
                .select_from(PaperTrade)
                .where(PaperTrade.status.in_([PaperTradeStatus.OPEN, PaperTradeStatus.CLOSED]))
            ).scalar_one()
            nonzero_slippage = session.execute(
                select(func.count())
                .select_from(PaperTrade)
                .where(
                    PaperTrade.status.in_([PaperTradeStatus.OPEN, PaperTradeStatus.CLOSED]),
                    PaperTrade.slippage_paid != Decimal("0"),
                )
            ).scalar_one()
    except Exception as exc:
        return [CheckResult(section, "paper trading tables", "FAIL", f"could not query: {exc!r}")]

    out = [
        CheckResult(
            section,
            "active portfolios",
            "PASS" if active_portfolios > 0 else "FAIL",
            f"{active_portfolios} active portfolio(s)",
        ),
        CheckResult(
            section,
            "closed trades",
            "PASS" if closed_count > 0 else "WARN",
            f"{closed_count} closed; exit reasons: "
            + (", ".join(f"{k}={v}" for k, v in sorted(exit_reasons.items())) or "none"),
        ),
    ]
    if filled_total == 0:
        out.append(
            CheckResult(
                section, "slippage non-zero", "WARN", "0 filled trades yet - nothing to check"
            )
        )
    else:
        status = "PASS" if nonzero_slippage > 0 else "FAIL"
        out.append(
            CheckResult(
                section,
                "slippage non-zero",
                status,
                f"{nonzero_slippage}/{filled_total} filled trades carry non-zero slippage_paid"
                + ("" if nonzero_slippage > 0 else " - fills may be bypassing the slippage model"),
            )
        )
    return out


# --- RISK ---------------------------------------------------------------


def check_risk_code() -> CheckResult:
    """rule_max_open_positions() smoke test - denies exactly at the cap,
    allows exactly under it. Proves the risk-rule layer's own logic is
    intact independent of whether any rule has ever fired in production.
    """
    section = "RISK"
    try:
        config = RiskRuleConfig(
            max_position_pct=Decimal("0.10"),
            max_position_size_enabled=True,
            max_exposure_pct=Decimal("0.60"),
            max_total_exposure_enabled=True,
            max_market_exposure_pct=Decimal("0.20"),
            max_market_exposure_enabled=True,
            max_correlated_exposure_pct=Decimal("0.30"),
            max_correlated_exposure_enabled=True,
            daily_stop_loss_pct=Decimal("0.10"),
            daily_stop_loss_enabled=True,
            max_open_positions=10,
            max_open_positions_enabled=True,
            min_bankroll_pct=Decimal("0.20"),
            min_bankroll_halt_enabled=True,
            emergency_stop_active=False,
        )
        trade = ProposedTrade(condition_id="0xc", event_slug="e")

        at_cap = PortfolioState(
            current_bankroll=Decimal("1000"),
            starting_bankroll=Decimal("1000"),
            day_start_bankroll=Decimal("1000"),
            realized_pnl_today=Decimal("0"),
            open_positions=(),
            open_count=10,
        )
        denied = rule_max_open_positions(at_cap, trade, config)
        if denied.action != RiskAction.DENY:
            return CheckResult(
                section,
                "rule_max_open_positions()",
                "FAIL",
                f"expected DENY at cap, got {denied.action}",
            )

        under_cap = PortfolioState(
            current_bankroll=Decimal("1000"),
            starting_bankroll=Decimal("1000"),
            day_start_bankroll=Decimal("1000"),
            realized_pnl_today=Decimal("0"),
            open_positions=(),
            open_count=9,
        )
        allowed = rule_max_open_positions(under_cap, trade, config)
        if allowed.action != RiskAction.ALLOW:
            return CheckResult(
                section,
                "rule_max_open_positions()",
                "FAIL",
                f"expected ALLOW under cap, got {allowed.action}",
            )

        return CheckResult(
            section,
            "rule_max_open_positions()",
            "PASS",
            "denies at cap (10/10), allows under cap (9/10)",
        )
    except Exception as exc:
        return CheckResult(section, "rule_max_open_positions()", "FAIL", f"raised {exc!r}")


def check_risk_data() -> list[CheckResult]:
    section = "RISK"
    try:
        with db_session() as session:
            counts = dict(
                session.execute(select(RiskEvent.rule, func.count()).group_by(RiskEvent.rule)).all()
            )
    except Exception as exc:
        return [CheckResult(section, "risk_events", "FAIL", f"could not query: {exc!r}")]

    out = []
    for rule in RiskRule:
        n = counts.get(rule.value, 0)
        if n > 0:
            out.append(
                CheckResult(section, f"risk_events[{rule.value}]", "PASS", f"{n} event(s) ever")
            )
        else:
            out.append(
                CheckResult(
                    section,
                    f"risk_events[{rule.value}]",
                    "WARN",
                    "zero events ever - verify app/risk/manager.py actually calls this rule "
                    "(app/paper/engine.py's pre_check/apply), or confirm this cap has genuinely "
                    "never been breached",
                )
            )
    return out


# --- PHASE 6 / SCOUT / DIAGNOSTICS -------------------------------------------


def _table_check(
    section: str, name: str, session, model, ts_column, warn_if_empty: str | None = None
) -> CheckResult:
    now = datetime.now(UTC)
    count = session.execute(select(func.count()).select_from(model)).scalar_one()
    latest = session.execute(select(func.max(ts_column))).scalar_one()
    if count == 0:
        return CheckResult(
            section, name, "WARN", warn_if_empty or "0 rows - table exists but is empty"
        )
    return CheckResult(section, name, "PASS", f"{count} row(s), latest {_fmt_age(now, latest)}")


def check_phase6_scout_diagnostics() -> list[CheckResult]:
    section = "PHASE6/SCOUT"
    try:
        with db_session() as session:
            out = [
                _table_check(
                    section, "wallet_clusters", session, WalletCluster, WalletCluster.computed_at
                ),
                _table_check(section, "signal_clv", session, SignalCLV, SignalCLV.computed_at),
            ]

            bandit_job = session.execute(
                select(JobHeartbeat).where(
                    JobHeartbeat.service == "collectors", JobHeartbeat.job_name == "bandit"
                )
            ).scalar_one_or_none()
            if bandit_job is not None and bandit_job.last_status == "failed":
                out.append(
                    CheckResult(
                        section,
                        "cluster_bandit_state",
                        "FAIL",
                        f"collectors/bandit job is failing: {bandit_job.last_error}",
                    )
                )
            else:
                out.append(
                    _table_check(
                        section,
                        "cluster_bandit_state",
                        session,
                        ClusterBanditState,
                        ClusterBanditState.updated_at,
                    )
                )

            out.append(
                _table_check(
                    section,
                    "trader_category_scores",
                    session,
                    TraderCategoryScore,
                    TraderCategoryScore.computed_at,
                )
            )
            out.append(
                _table_check(
                    section,
                    "wallet_crowdedness",
                    session,
                    WalletCrowdedness,
                    WalletCrowdedness.computed_at,
                )
            )
            out.append(
                _table_check(
                    section, "trader_pipeline", session, TraderPipeline, TraderPipeline.updated_at
                )
            )
            out.append(
                _table_check(
                    section,
                    "scout_forward_trades",
                    session,
                    ScoutForwardTrade,
                    ScoutForwardTrade.computed_at,
                )
            )
            out.append(
                _table_check(
                    section,
                    "scout_validation_windows",
                    session,
                    ScoutValidationWindow,
                    ScoutValidationWindow.computed_at,
                    warn_if_empty="0 rows - expected if no candidate has been tracked long enough "
                    "to close a validation window yet",
                )
            )
            out.append(
                _table_check(
                    section,
                    "scout_stage_transitions",
                    session,
                    ScoutStageTransition,
                    ScoutStageTransition.transitioned_at,
                )
            )

            missed_total = session.execute(
                select(func.count())
                .select_from(PaperTrade)
                .where(PaperTrade.status == PaperTradeStatus.MISSED)
            ).scalar_one()
            missed_with_shadow = session.execute(
                select(func.count())
                .select_from(PaperTrade)
                .where(
                    PaperTrade.status == PaperTradeStatus.MISSED,
                    PaperTrade.current_price.isnot(None),
                )
            ).scalar_one()
            if missed_total == 0:
                out.append(
                    CheckResult(
                        section,
                        "diagnostics (missed-trade shadows)",
                        "WARN",
                        "no MISSED trades yet to evaluate",
                    )
                )
            elif missed_with_shadow == 0:
                out.append(
                    CheckResult(
                        section,
                        "diagnostics (missed-trade shadows)",
                        "WARN",
                        f"0/{missed_total} MISSED trades carry a shadow current_price - "
                        "no counterfactual tracking exists for missed trades",
                    )
                )
            else:
                out.append(
                    CheckResult(
                        section,
                        "diagnostics (missed-trade shadows)",
                        "PASS",
                        f"{missed_with_shadow}/{missed_total} MISSED trades carry "
                        f"a shadow current_price",
                    )
                )
            return out
    except Exception as exc:
        return [
            CheckResult(
                section, "phase6/scout/diagnostics tables", "FAIL", f"could not query: {exc!r}"
            )
        ]


# --- OPS (DB-based only - see module docstring for why the HTTP checks live
# in scripts/system_audit.py instead) --------------------------------------


def check_job_heartbeats() -> list[CheckResult]:
    """Every scheduled job across every service - fresh/stale/dead per its
    own recorded interval. Catches a job that is flatly failing directly;
    a job that ticks "ok" every cycle while quietly accomplishing nothing
    downstream is still caught by the section-specific data checks above.
    """
    section = "OPS"
    now = datetime.now(UTC)
    try:
        with db_session() as session:
            jobs = (
                session.execute(
                    select(JobHeartbeat).order_by(JobHeartbeat.service, JobHeartbeat.job_name)
                )
                .scalars()
                .all()
            )
    except Exception as exc:
        return [CheckResult(section, "job_heartbeats", "FAIL", f"could not query: {exc!r}")]

    if not jobs:
        return [
            CheckResult(section, "job_heartbeats", "WARN", "no jobs have reported a heartbeat yet")
        ]

    out = []
    for job in jobs:
        age = (now - job.last_success_at).total_seconds() if job.last_success_at else None
        status = _age_status(age, job.interval_seconds)
        detail = f"last_status={job.last_status} last_success={_fmt_age(now, job.last_success_at)}"
        if job.last_status == "failed" and job.last_error:
            detail += f" error={job.last_error[:120]}"
        out.append(CheckResult(section, f"job[{job.service}/{job.job_name}]", status, detail))
    return out


def check_retention_pruner(settings: Settings) -> list[CheckResult]:
    """Beyond "did the job run" (already covered by job_heartbeats): did it
    actually delete anything? A pruner that runs "ok" every day but whose
    DELETE silently matches zero rows would look identical to a healthy one
    in job_heartbeats alone - this checks the retention promise held, not
    just that the job executed.
    """
    section = "OPS"
    now = datetime.now(UTC)
    checks = (
        ("prices", PriceSnapshot.captured_at, settings.prices_retention_days),
        ("market_history", MarketHistory.captured_at, settings.market_history_retention_days),
        ("position_history", PositionHistory.detected_at, settings.position_history_retention_days),
    )
    out = []
    try:
        with db_session() as session:
            for table, column, retention_days in checks:
                oldest = session.execute(select(func.min(column))).scalar_one()
                if oldest is None:
                    out.append(
                        CheckResult(
                            section,
                            f"retention[{table}]",
                            "WARN",
                            "table is empty, nothing to check",
                        )
                    )
                    continue
                oldest_age_days = (now - oldest).total_seconds() / 86400
                # Some slack above the exact cutoff: the pruner runs once a
                # day, not continuously, so "up to ~1 extra day old" is
                # still healthy, not a sign pruning stopped working.
                if oldest_age_days <= retention_days + 2:
                    out.append(
                        CheckResult(
                            section,
                            f"retention[{table}]",
                            "PASS",
                            f"oldest row is {oldest_age_days:.1f}d old "
                            f"(retention={retention_days}d) - pruning is deleting",
                        )
                    )
                else:
                    out.append(
                        CheckResult(
                            section,
                            f"retention[{table}]",
                            "WARN",
                            f"oldest row is {oldest_age_days:.1f}d old, "
                            f"well past retention={retention_days}d - "
                            "pruner may not actually be deleting from this table",
                        )
                    )
        return out
    except Exception as exc:
        return [CheckResult(section, "retention pruner", "FAIL", f"could not query: {exc!r}")]


def run_checks(settings: Settings) -> list[CheckResult]:
    """Every DB-based / pure-function check, in report order. Excludes the
    two HTTP checks (/healthz, dashboard routes) that only make sense run
    from outside the dashboard process itself - see scripts/system_audit.py.
    """
    out: list[CheckResult] = [check_schema()]
    out.append(check_collectors_code())
    out.extend(check_collectors_data(settings))
    out.append(check_consensus_code())
    out.extend(check_consensus_data(settings))
    out.extend(check_paper_code())
    out.extend(check_paper_data())
    out.append(check_risk_code())
    out.extend(check_risk_data())
    out.extend(check_phase6_scout_diagnostics())
    out.extend(check_job_heartbeats())
    out.extend(check_retention_pruner(settings))
    return out
