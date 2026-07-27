# polybot

Prediction-market consensus copy-trading research bot. It monitors profitable
Polymarket traders via public read-only APIs, detects markets where many of
them agree, scores that consensus, and paper-trades the signals so the
strategy can be evaluated statistically before real money is ever considered.

## Hard rules (never violate)

- READ-ONLY. This project never places, signs, or cancels orders, and never
  handles wallet private keys, seed phrases, or exchange credentials.
- The operator is in the Netherlands, where the Kansspelautoriteit has ruled
  Polymarket an unlicensed game of chance and participation is blocked. Never
  write code that bypasses geo-blocking (no VPN/proxy rotation, no header
  spoofing to defeat region checks). Reading public market data is fine.
- Never commit .env or any secret. Secrets live only in .env, which is
  git-ignored. .env.example holds placeholder values and IS committed.
- Never write TODO, FIXME, placeholder, or pseudocode. If something cannot be
  finished now, say so in chat instead of leaving a stub in the file.

## Stack

Python 3.12+ - uv (deps and venv) - SQLAlchemy 2.0 ORM - Alembic migrations -
PostgreSQL 17 and Redis 8 in Docker Compose - httpx (async) - Pydantic v2 and
pydantic-settings - tenacity (retries) - pytest - ruff (lint + format).

## Commands

- Install deps: uv sync
- Run anything: uv run <command>   (never bare python/pytest)
- Tests: uv run pytest
- Lint and format: uv run ruff check . and uv run ruff format .
- Migrations: uv run alembic revision --autogenerate -m "msg" then
  uv run alembic upgrade head
- Infrastructure: docker compose up -d
- Environment check: uv run python scripts/verify_setup.py

## Layout

app/config settings - app/db engine, session, models - app/collectors API
clients and collectors - app/consensus scoring (phase 3) - app/signals (phase
3) - app/paper (phase 4) - app/risk (phase 5) - app/execution (phase 6, gated)
- app/notifications (phase 7) - app/dashboard (phase 8) - app/scheduler jobs -
app/utils helpers - alembic/ migrations - scripts/ operational scripts -
tests/ - docker/ - docs/

## Code conventions

- Money, prices, sizes, and PnL: always Python Decimal and PostgreSQL
  NUMERIC(24,6). Never float. Never REAL/DOUBLE.
- Type hints on every function signature. Module and class docstrings explain
  WHY, not just what.
- All network I/O is async (httpx.AsyncClient). Blocking DB work called from
  async code goes through asyncio.to_thread.
- Configuration is read only in app/config/settings.py. No os.environ
  elsewhere.
- Logging via the stdlib logging module, logger = logging.getLogger(__name__).
  Never print() outside scripts/.
- Line length 100. Run ruff check and ruff format before saying a task is done.
- Every signal must be explainable from its own database row.
- Every rejected candidate is accounted for in the funnel log with a reason.
- Thresholds live in settings only - a hardcoded threshold is a bug.

## Working agreement

- Build one module at a time. After each module: run the tests, run ruff, and
  report what you did and how to verify it.
- Before writing code against any external API, verify the endpoint, its
  parameters, and its response shape against the current official docs
  (docs.polymarket.com). Never guess an endpoint or a field name. If the docs
  contradict this file, tell me instead of silently choosing.
- Pure logic (scoring, diffing, metrics) goes in functions with no DB or
  network access, so it can be unit tested directly.
- Ask before adding a dependency that is not listed under Stack.
- If a field is documented in an API's response schema but not as a query
  parameter, filter on it client-side after fetching. Never send an
  undocumented query parameter just because the field exists elsewhere in
  the API.
