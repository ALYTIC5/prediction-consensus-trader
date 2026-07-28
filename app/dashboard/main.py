"""FastAPI app: the polybot operations console.

Runs as its own process (scripts/run_dashboard.py), never inside
app.main's scheduler - a dashboard slowdown or crash must never be able to
affect data collection, and vice versa. Local dev stays localhost-only with
no authentication, same as the original design (see docs/PHASE8_DESIGN.md).
Deployed (Railway) it's reachable from outside the container, so HTTP Basic
auth is required there - see the production guard and basic_auth middleware
below.
"""

import base64
import logging
import secrets
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config.adjustable import ADJUSTABLE
from app.config.settings import get_settings
from app.dashboard import queries
from app.dashboard.filters import relative_time, short_address, to_json

logger = logging.getLogger(__name__)

_DASHBOARD_DIR = Path(__file__).resolve().parent

_startup_settings = get_settings()
if _startup_settings.environment == "production" and not (
    _startup_settings.dashboard_user and _startup_settings.dashboard_password
):
    raise RuntimeError(
        "DASHBOARD_USER and DASHBOARD_PASSWORD must both be set when "
        "ENVIRONMENT=production - the console has no other access control, "
        "and production must never run reachable-from-outside with no auth."
    )

app = FastAPI(title="polybot console")
app.mount("/static", StaticFiles(directory=_DASHBOARD_DIR / "static"), name="static")
templates = Jinja2Templates(directory=_DASHBOARD_DIR / "templates")
templates.env.filters["relative_time"] = relative_time
templates.env.filters["short_address"] = short_address
templates.env.filters["tojson"] = to_json


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    """HTTP Basic auth on every route except /healthz.

    Enabled only when both DASHBOARD_USER and DASHBOARD_PASSWORD are set -
    off by default for local dev (matches the original localhost-only,
    no-auth design), on whenever both are configured (required in
    production by the startup guard above). secrets.compare_digest avoids
    leaking credential-length/prefix information via timing.
    """
    if request.url.path == "/healthz":
        return await call_next(request)

    settings = get_settings()
    if not (settings.dashboard_user and settings.dashboard_password):
        return await call_next(request)

    authorized = False
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth_header.removeprefix("Basic ")).decode("utf-8")
            username, _, password = decoded.partition(":")
        except Exception:  # malformed/attacker-controlled header - never a 500, just unauthorized
            username, password = "", ""
        authorized = secrets.compare_digest(
            username, settings.dashboard_user
        ) and secrets.compare_digest(password, settings.dashboard_password)

    if not authorized:
        return Response(
            status_code=401, headers={"WWW-Authenticate": 'Basic realm="polybot console"'}
        )

    return await call_next(request)


@app.get("/healthz")
def healthz() -> JSONResponse:
    """Real checks, not just "the process is up" - db/redis are actually
    pinged, so a human (or the header strip's heartbeat dot) can tell
    they're reachable right now, not just that they were configured.

    Also checks every job's heartbeat (same table this powers the Overview
    row from) and fails the check if any job is "dead" - beyond 3x its own
    interval since its last write. Without this, a process can be up,
    db/redis reachable, and still be silently useless: the consensus job
    hung for 10+ hours with the process otherwise perfectly healthy, and
    nothing here would have caught that before this check existed.
    """
    db_ok = queries.check_db_health()
    redis_ok = queries.check_redis_health()

    # Only worth asking if the DB is actually up - and this must never be
    # the thing that turns a DB outage into a 500 instead of a clean 503,
    # same reasoning as check_db_health/check_redis_health's own broad
    # excepts above.
    dead_jobs: list[str] = []
    if db_ok:
        try:
            heartbeats = queries.get_collector_heartbeats(get_settings())
            dead_jobs = [hb.name for hb in heartbeats if hb.status == "dead"]
        except Exception:
            logger.warning("healthz: heartbeat check failed", exc_info=True)
            db_ok = False

    healthy = db_ok and redis_ok and not dead_jobs
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={
            "status": "ok" if healthy else "degraded",
            "db": db_ok,
            "redis": redis_ok,
            "dead_jobs": dead_jobs,
            "time": datetime.now(UTC).isoformat(),
        },
    )


@app.get("/")
def overview(request: Request):
    """The Overview page: heartbeat row, count strip, funnel, tape."""
    settings = get_settings()
    context = {
        "active_page": "overview",
        "environment": settings.environment,
        "heartbeats": queries.get_collector_heartbeats(settings),
        "counts": queries.get_overview_counts(),
        "latest_run": queries.get_latest_consensus_run(),
        "tape_events": queries.get_recent_tape_events(),
    }
    return templates.TemplateResponse(request, "overview.html", context)


@app.get("/fragments/tape")
def tape_fragment(request: Request):
    """htmx-polled fragment: just the tape rows, refreshed every 5s."""
    context = {"tape_events": queries.get_recent_tape_events()}
    return templates.TemplateResponse(request, "_tape.html", context)


@app.get("/signals")
def signals(request: Request):
    """The Signals page: ACTIVE table, drill-downs, history, funnel-over-time."""
    settings = get_settings()
    funnel_history = queries.get_funnel_history()
    context = {
        "active_page": "signals",
        "environment": settings.environment,
        "latest_run": queries.get_latest_consensus_run(),
        "active_signals": queries.get_active_signals(),
        "history": queries.get_recent_signal_history(),
        "funnel_history": funnel_history,
        # json.dumps can't serialize a dataclass on its own (it would fall
        # back to str(point), producing one big repr string instead of real
        # objects) - convert to plain dicts here for the chart's JSON blob.
        "funnel_history_json": [asdict(point) for point in funnel_history],
    }
    return templates.TemplateResponse(request, "signals.html", context)


@app.get("/fragments/signal/{signal_id}")
def signal_contributors_fragment(request: Request, signal_id: int):
    """htmx drill-down: one signal's full contributor evidence, from its own row."""
    signal = queries.get_signal_by_id(signal_id)
    if signal is None:
        raise HTTPException(status_code=404, detail="signal not found")
    return templates.TemplateResponse(request, "_signal_contributors.html", {"signal": signal})


@app.get("/traders")
def traders(request: Request):
    """The Traders page: latest score per wallet, sorted by score."""
    context = {
        "active_page": "traders",
        "environment": get_settings().environment,
        "traders": queries.get_trader_scores(),
    }
    return templates.TemplateResponse(request, "traders.html", context)


@app.get("/traders/{wallet_id}")
def wallet_detail(request: Request, wallet_id: int):
    """One wallet's open positions and recent event history."""
    detail = queries.get_wallet_detail(wallet_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="wallet not found")
    context = {
        "active_page": "traders",
        "environment": get_settings().environment,
        "wallet": detail,
    }
    return templates.TemplateResponse(request, "wallet_detail.html", context)


@app.get("/markets")
def markets(request: Request, status: str = "open"):
    """The Markets page: markets held by tracked wallets, open by default."""
    if status not in ("open", "closed", "all"):
        status = "open"
    context = {
        "active_page": "markets",
        "environment": get_settings().environment,
        "status": status,
        "markets": queries.get_markets(status=status),
    }
    return templates.TemplateResponse(request, "markets.html", context)


@app.get("/markets/{market_id}")
def market_detail(request: Request, market_id: int):
    """One market's outcome prices and price-history sparklines."""
    detail = queries.get_market_detail(market_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="market not found")
    context = {
        "active_page": "markets",
        "environment": get_settings().environment,
        "detail": detail,
        "sparkline_series_json": {
            asset: [asdict(point) for point in points]
            for asset, points in detail.price_series.items()
        },
    }
    return templates.TemplateResponse(request, "market_detail.html", context)


@app.get("/events")
def events_page(request: Request, event_type: str = "", include_bootstrap: bool = False):
    """The Events page: filterable, auto-refreshing position_history feed."""
    context = {
        "active_page": "events",
        "environment": get_settings().environment,
        "event_type": event_type,
        "include_bootstrap": include_bootstrap,
        "events": queries.get_events(
            event_type=event_type or None, include_bootstrap=include_bootstrap
        ),
    }
    return templates.TemplateResponse(request, "events.html", context)


@app.get("/fragments/events")
def events_fragment(request: Request, event_type: str = "", include_bootstrap: bool = False):
    """htmx-polled fragment backing the Events page's filters + auto-refresh."""
    context = {
        "events": queries.get_events(
            event_type=event_type or None, include_bootstrap=include_bootstrap
        )
    }
    return templates.TemplateResponse(request, "_events_table.html", context)


@app.get("/tuning")
def tuning(request: Request):
    """The Tuning page. GET here never mutates anything - only the two POST
    routes below do, and they're the console's only mutating endpoints.
    """
    context = {
        "active_page": "tuning",
        "environment": get_settings().environment,
        "rows": queries.get_tuning_rows(),
        "audit": queries.get_override_audit(),
        "error": None,
        "confirmation": None,
        "submitted_value": None,
    }
    return templates.TemplateResponse(request, "tuning.html", context)


@app.post("/tuning/{key}/apply")
def tuning_apply(request: Request, key: str, value: str = Form(...)):
    """Validate + upsert one override, then re-render just that row."""
    field = ADJUSTABLE.get(key)
    if field is None:
        raise HTTPException(status_code=404, detail="not an adjustable setting")

    if field.restart_required:
        # Defense in depth: the UI never renders a form for these rows, but
        # a direct POST must still be rejected, not silently accepted into
        # an override the scheduler would never actually honor.
        context = {
            "row": queries.get_tuning_row(key),
            "error": "restart-required settings can't be overridden live",
            "confirmation": None,
            "submitted_value": value,
        }
        return templates.TemplateResponse(request, "_tuning_row.html", context)

    result = queries.apply_override(key, value)
    row = queries.get_tuning_row(key)
    if result.ok:
        context = {
            "row": row,
            "error": None,
            "confirmation": f"Applied - effective value is now {row.effective_value}.",
            "submitted_value": None,
        }
    else:
        context = {
            "row": row,
            "error": result.error,
            "confirmation": None,
            "submitted_value": value,
        }
    return templates.TemplateResponse(request, "_tuning_row.html", context)


@app.post("/tuning/{key}/reset")
def tuning_reset(request: Request, key: str):
    """Delete the override (if any), then re-render just that row."""
    if key not in ADJUSTABLE:
        raise HTTPException(status_code=404, detail="not an adjustable setting")
    queries.reset_override(key)
    context = {
        "row": queries.get_tuning_row(key),
        "error": None,
        "confirmation": "Reset to default.",
        "submitted_value": None,
    }
    return templates.TemplateResponse(request, "_tuning_row.html", context)


# --- Paper trading page ---


@app.get("/paper")
def paper_page(request: Request):
    """The Paper page: comparison strip, portfolio selector, equity curve,
    full metrics, and trades table for the selected portfolio.
    """
    settings = get_settings()
    portfolios = queries.list_portfolios()
    comparison = queries.get_comparison_data() if portfolios else []

    selected_id = request.query_params.get("portfolio")
    if selected_id:
        selected_id = int(selected_id)
    else:
        selected_id = next(
            (p.id for p in portfolios if p.is_active),
            portfolios[0].id if portfolios else None,
        )

    if selected_id is not None:
        metrics = queries.portfolio_metrics_from_db(selected_id)
        trades = queries.list_portfolio_trades(selected_id)
        equity_curve = queries.equity_curve_points(selected_id)
        missed_count = queries.get_missed_count(selected_id)
    else:
        metrics = None
        trades = []
        equity_curve = []
        missed_count = 0

    equity_labels = [p.exit_at.strftime("%b %d %H:%M") for p in equity_curve]
    equity_data = [float(p.equity) for p in equity_curve]
    equity_chart_json = {
        "labels": equity_labels,
        "datasets": [
            {
                "label": "Equity",
                "data": equity_data,
                "borderColor": "#5CF2C7",
                "backgroundColor": "rgba(92, 242, 199, 0.1)",
                "fill": True,
                "tension": 0,
                "pointRadius": 0,
                "borderWidth": 1.5,
            }
        ],
    }

    context = {
        "active_page": "paper",
        "environment": settings.environment,
        "portfolios": portfolios,
        "comparison": comparison,
        "selected_id": selected_id,
        "metrics": metrics,
        "trades": trades,
        "missed_count": missed_count,
        "equity_curve": equity_curve,
        "equity_chart_json": equity_chart_json,
        "paper_min_trades_for_stats": settings.paper_min_trades_for_stats,
    }
    return templates.TemplateResponse(request, "paper.html", context)


@app.get("/fragments/paper-content/{portfolio_id}")
def paper_content_fragment(request: Request, portfolio_id: int):
    """htmx fragment: one portfolio's metrics, equity chart data, and
    trades table - swapped in when the user selects a different portfolio.
    """
    metrics = queries.portfolio_metrics_from_db(portfolio_id)
    trades = queries.list_portfolio_trades(portfolio_id)
    equity_curve = queries.equity_curve_points(portfolio_id)
    missed_count = queries.get_missed_count(portfolio_id)

    equity_labels = [p.exit_at.strftime("%b %d %H:%M") for p in equity_curve]
    equity_data = [float(p.equity) for p in equity_curve]
    equity_chart_json = {
        "labels": equity_labels,
        "datasets": [
            {
                "label": "Equity",
                "data": equity_data,
                "borderColor": "#5CF2C7",
                "backgroundColor": "rgba(92, 242, 199, 0.1)",
                "fill": True,
                "tension": 0,
                "pointRadius": 0,
                "borderWidth": 1.5,
            }
        ],
    }

    context = {
        "portfolio_id": portfolio_id,
        "metrics": metrics,
        "trades": trades,
        "missed_count": missed_count,
        "equity_chart_json": equity_chart_json,
    }
    return templates.TemplateResponse(request, "_paper_content.html", context)


@app.get("/fragments/paper-trades/{portfolio_id}")
def paper_trades_fragment(request: Request, portfolio_id: int, status: str = "all"):
    """htmx fragment: filtered trades table for one portfolio."""
    trades = queries.list_portfolio_trades(portfolio_id, status_filter=status)
    context = {"trades": trades, "status_filter": status}
    return templates.TemplateResponse(request, "_paper_trades.html", context)


@app.get("/fragments/paper-overview")
def paper_overview_fragment(request: Request):
    """htmx fragment: Paper PnL panel for the Overview page, showing one
    compact row per portfolio.
    """
    portfolios = queries.list_portfolios()
    overview_rows: list[dict] = []
    for p in portfolios:
        metrics = queries.portfolio_metrics_from_db(p.id)
        missed = queries.get_missed_count(p.id)
        overview_rows.append(
            {
                "name": p.name,
                "metrics": metrics,
                "missed_count": missed,
            }
        )
    context = {"rows": overview_rows}
    return templates.TemplateResponse(request, "_paper_overview.html", context)


@app.get("/logs")
def logs_page(request: Request):
    """The Logs page: tails logs/polybot.log."""
    context = {
        "active_page": "logs",
        "environment": get_settings().environment,
        "lines": queries.get_log_tail(),
    }
    return templates.TemplateResponse(request, "logs.html", context)


@app.get("/fragments/logs")
def logs_fragment(request: Request):
    """htmx-polled fragment: the log tail, refreshed every 5s (pausable)."""
    context = {"lines": queries.get_log_tail()}
    return templates.TemplateResponse(request, "_logs.html", context)
