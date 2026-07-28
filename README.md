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
