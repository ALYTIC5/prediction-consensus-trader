"""Environment verification: run this after any setup change.

Prints a pass/fail report for each precondition the app needs, in the order
a fresh clone would hit them, with an actionable hint on every failure.
Exits 1 if anything failed so it can be used as a CI/pre-flight gate.
"""

import sys
from importlib import import_module
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, hint: str = "") -> bool:
    """Record and print one check's outcome."""
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {name}")
    if not ok and hint:
        print(f"       -> {hint}")
    results.append((name, ok, hint))
    return ok


def check_python_version() -> None:
    ok = sys.version_info >= (3, 12)
    check(
        "Python 3.12+",
        ok,
        f"Found {sys.version.split()[0]}. Install Python 3.12 or newer.",
    )


def check_env_file() -> None:
    check(
        ".env exists",
        (PROJECT_ROOT / ".env").is_file(),
        "Run: cp .env.example .env, then fill in real values.",
    )


def check_required_packages() -> None:
    for module_name in ("sqlalchemy", "alembic", "psycopg", "redis", "httpx", "pydantic_settings"):
        try:
            import_module(module_name)
            check(f"import {module_name}", True)
        except ImportError as exc:
            check(f"import {module_name}", False, f"Run: uv sync. ({exc})")


def check_settings_load() -> "object | None":
    try:
        from app.config.settings import get_settings

        settings = get_settings()
    except Exception as exc:
        check(
            "settings load from .env",
            False,
            f"Settings failed to load: {exc}. Check .env has POSTGRES_PASSWORD and REDIS_URL set.",
        )
        return None

    if "CHANGE_ME" in settings.postgres_password:
        check(
            "settings load from .env",
            False,
            "POSTGRES_PASSWORD is still the placeholder from .env.example. "
            "Generate a real password and put it in .env.",
        )
        return None

    check("settings load from .env", True)
    return settings


def check_postgres_connects(settings: object) -> "object | None":
    try:
        from sqlalchemy import text

        from app.db.session import get_engine

        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT version()"))
        check("PostgreSQL connects", True)
        return engine
    except Exception as exc:
        check(
            "PostgreSQL connects",
            False,
            f"Could not connect: {exc}. Run: docker compose up -d, "
            "and check .env host/port/credentials.",
        )
        return None


def check_migrations_applied(engine: object) -> None:
    try:
        from sqlalchemy import inspect

        import app.db.models  # noqa: F401  (registers every model on Base.metadata)
        from app.db.base import Base

        existing = set(inspect(engine).get_table_names())
        expected = set(Base.metadata.tables.keys()) | {"alembic_version"}
        missing = expected - existing

        check(
            "migrations applied",
            not missing,
            f"Missing tables: {sorted(missing)}. Run: uv run alembic upgrade head.",
        )
    except Exception as exc:
        check("migrations applied", False, f"Could not inspect database: {exc}")


def check_redis_responds() -> None:
    try:
        import redis

        from app.config.settings import get_settings

        client = redis.from_url(get_settings().redis_url)
        ok = client.ping()
        check(
            "Redis responds to ping",
            bool(ok),
            "Redis did not respond to PING. Check REDIS_URL and that redis is running.",
        )
    except Exception as exc:
        check(
            "Redis responds to ping",
            False,
            f"Could not reach Redis: {exc}. Run: docker compose up -d, "
            "and check REDIS_URL in .env.",
        )


def main() -> int:
    check_python_version()
    check_env_file()
    check_required_packages()

    settings = check_settings_load()
    engine = check_postgres_connects(settings) if settings is not None else None
    if engine is not None:
        check_migrations_applied(engine)
    else:
        check(
            "migrations applied",
            False,
            "Skipped: PostgreSQL did not connect.",
        )

    check_redis_responds()

    failed = [name for name, ok, _ in results if not ok]
    print()
    if failed:
        print(f"{len(failed)} check(s) failed: {', '.join(failed)}")
        return 1

    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
