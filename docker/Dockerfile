# syntax=docker/dockerfile:1

# --- Stage 1: builder - resolves and installs locked dependencies only ---
FROM python:3.12-slim AS builder

RUN pip install --no-cache-dir uv

WORKDIR /app

# Copy only the dependency manifests first so this layer is cached across
# builds that don't change dependencies.
COPY pyproject.toml uv.lock ./
# --frozen: fail rather than silently re-resolve if uv.lock is stale.
# --no-dev: pytest/ruff have no reason to exist in the runtime image.
RUN uv sync --frozen --no-dev

# --- Stage 2: runtime - just the venv and the app, as a non-root user ---
FROM python:3.12-slim AS runtime

RUN useradd --create-home --shell /usr/sbin/nologin polybot

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"
# The builder stage installed polybot editable before any of app/'s real
# source existed (only pyproject.toml/uv.lock were copied at that point),
# so there's no working editable-install finder to fall back on here.
# Running "python scripts/run_dashboard.py" only puts scripts/ itself on
# sys.path, not /app - PYTHONPATH is what actually makes "import app.*"
# resolve, the same way `uv run` does it locally.
ENV PYTHONPATH=/app

# Deliberately not copied: .env (secrets - injected as real env vars by
# Railway instead), tests/, docs/, docker/, .git - none of it is needed to
# run the app, and .env especially must never end up baked into an image.
COPY app/ ./app/
COPY alembic/ ./alembic/
COPY alembic.ini ./alembic.ini
COPY scripts/ ./scripts/

RUN chown -R polybot:polybot /app
USER polybot

# No `uv` in this stage (deliberately - it's a builder-only tool), so this
# calls the venv's python directly rather than "uv run", which PATH above
# already makes resolve to /app/.venv/bin/python.
CMD ["python", "scripts/run_dashboard.py"]
