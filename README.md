# polybot

Prediction-market consensus copy-trading research bot. It monitors profitable
Polymarket traders via public read-only APIs, detects markets where many of
them agree, scores that consensus, and paper-trades the signals so the
strategy can be evaluated statistically before real money is ever considered.

## Read-only, no trading, legal note

This project is **read-only**. It never places, signs, or cancels orders, and
never handles wallet private keys, seed phrases, or exchange credentials. The
operator is in the Netherlands, where the Kansspelautoriteit has ruled
Polymarket an unlicensed game of chance and participation is blocked. This
codebase never bypasses geo-blocking (no VPN/proxy rotation, no header
spoofing). It only reads public market data.

## Quickstart

```bash
uv sync
cp .env.example .env   # fill in real values, never commit .env
docker compose up -d
uv run alembic upgrade head
uv run python scripts/verify_setup.py
```

## Operations console

```bash
uv run python scripts/run_dashboard.py
```

Binds to `127.0.0.1:8000` only - never `0.0.0.0`. There's no authentication,
which is acceptable only because it's unreachable off this machine; remote
access waits for a later phase and real auth. Pages: Overview, Signals,
Traders, Markets, Events, Tuning, Logs. Design notes in
`docs/PHASE8_DESIGN.md`.

The Tuning page's overrides are written to the `runtime_overrides` table in
Postgres, never to `.env` - they're picked up fresh on the next scoring/
signal-generation cycle, no restart, and disappear if you reset them or
truncate the table. `.env` stays the one source of truth for everything
that actually needs a restart.

## System audit

```bash
uv run python scripts/system_audit.py
```

Read-only, safe to run against production: verifies every subsystem
(collectors, consensus, paper trading, risk, phase 6/scout/diagnostics, ops)
by checking BOTH that the code is intact (pure-function smoke tests against
the real diffing/consensus/fills/risk logic) AND that it has actually
produced recent data - not just that a job "succeeded." Flags schema drift
(DB alembic revision vs code head) loudest of all - that exact drift once
kept the Scout silently dead for 10 days. Prints PASS/WARN/FAIL per check
with evidence inline, exits non-zero if anything FAILed.

Points at `127.0.0.1:8000` by default. Add `--dashboard-url <url>` to audit
a deployed dashboard instead (set `DASHBOARD_USER`/`DASHBOARD_PASSWORD` in
`.env` first if it's auth-gated), or `--skip-http` for a DB-only run with no
network calls to the dashboard at all. The same DB-based checks also render
as a page in the console itself - see System Health in the nav.

## Folders

| Folder               | Purpose                                                |
|-----------------------|---------------------------------------------------------|
| `app/config`          | Settings (env vars read here, nowhere else)             |
| `app/db`              | SQLAlchemy engine, session, models                       |
| `app/collectors`      | Public API clients and data collectors                   |
| `app/consensus`       | Trader scoring and the pure consensus-filtering engine    |
| `app/signals`         | Orchestrates the consensus engine into persisted signals  |
| `app/paper`           | Paper-trading engine (phase 4)                           |
| `app/risk`            | Risk and position-sizing rules (phase 5)                 |
| `app/execution`       | Real order execution, gated off (phase 6)                |
| `app/notifications`   | Alerting (phase 7)                                       |
| `app/dashboard`       | Ops console (FastAPI/Jinja2/htmx) - see `docs/PHASE8_DESIGN.md` |
| `app/scheduler`       | Scheduled jobs                                           |
| `app/utils`           | Shared helpers                                           |
| `alembic/`            | Database migrations                                      |
| `scripts/`            | Operational scripts (e.g. `verify_setup.py`)             |
| `tests/`              | Test suite                                               |
| `docs/`               | Project documentation                                    |
