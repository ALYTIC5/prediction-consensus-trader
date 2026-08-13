"""Repeatable system audit: runs every check in app/utils/system_audit.py
(schema drift, collectors, consensus, paper trading, risk, phase6/scout/
diagnostics, job heartbeats, retention pruning) plus the two checks that
only make sense from outside the dashboard process - /healthz and each
dashboard route's HTTP status - and prints a PASS/WARN/FAIL report.

Read-only: every DB access is a SELECT, and the dashboard checks are plain
GETs. Never runs migrations, never writes an override, never touches
runtime_overrides or any trading table. Safe against production.

Exit code: 0 if nothing FAILed, 1 otherwise - usable as a CI/cron gate.

Usage:
    uv run python scripts/system_audit.py
    uv run python scripts/system_audit.py --dashboard-url https://dashboard-production-xxxx.up.railway.app
    uv run python scripts/system_audit.py --skip-http   # DB-only, no network calls to the dashboard
"""

import argparse
import sys

import httpx

from app.config.settings import get_settings
from app.utils.system_audit import CheckResult, run_checks

_DASHBOARD_ROUTES = (
    "/",
    "/signals",
    "/traders",
    "/markets",
    "/events",
    "/tuning",
    "/risk",
    "/optimization",
    "/scout",
    "/paper",
    "/paper/comparison",
    "/logs",
    "/health",
    "/system-health",
)


def _print(result: CheckResult) -> None:
    print(f"[{result.status:4}] {result.section:12} {result.name} - {result.evidence}")


def check_healthz(base_url: str, auth: tuple[str, str] | None) -> CheckResult:
    section = "OPS"
    try:
        response = httpx.get(f"{base_url}/healthz", auth=auth, timeout=10)
        payload = response.json()
    except Exception as exc:
        return CheckResult(section, "/healthz", "FAIL", f"unreachable: {exc!r}")

    if payload.get("status") == "ok":
        return CheckResult(
            section,
            "/healthz",
            "PASS",
            f"status=ok db={payload.get('db')} redis={payload.get('redis')}",
        )
    dead = payload.get("dead_jobs", [])
    return CheckResult(
        section,
        "/healthz",
        "FAIL",
        f"*** DEGRADED *** db={payload.get('db')} redis={payload.get('redis')} dead_jobs={dead}",
    )


def check_dashboard_routes(base_url: str, auth: tuple[str, str] | None) -> list[CheckResult]:
    section = "OPS"
    out = []
    for path in _DASHBOARD_ROUTES:
        try:
            response = httpx.get(f"{base_url}{path}", auth=auth, timeout=10)
            status = "PASS" if response.status_code < 500 else "FAIL"
            out.append(
                CheckResult(section, f"route {path}", status, f"HTTP {response.status_code}")
            )
        except Exception as exc:
            out.append(CheckResult(section, f"route {path}", "FAIL", f"unreachable: {exc!r}"))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--dashboard-url",
        default=None,
        help="Dashboard base URL to hit for /healthz and route checks "
        "(default: settings.dashboard_base_url)",
    )
    parser.add_argument(
        "--skip-http",
        action="store_true",
        help="Skip the /healthz and dashboard-route checks (DB-only run)",
    )
    args = parser.parse_args()

    settings = get_settings()
    base_url = (args.dashboard_url or settings.dashboard_base_url).rstrip("/")
    auth = (
        (settings.dashboard_user, settings.dashboard_password)
        if settings.dashboard_user and settings.dashboard_password
        else None
    )

    results = run_checks(settings)
    if not args.skip_http:
        results.append(check_healthz(base_url, auth))
        results.extend(check_dashboard_routes(base_url, auth))

    for result in results:
        _print(result)

    passed = sum(1 for r in results if r.status == "PASS")
    warned = sum(1 for r in results if r.status == "WARN")
    failed = sum(1 for r in results if r.status == "FAIL")

    print()
    print(f"{passed} passed, {warned} warned, {failed} failed ({len(results)} checks total)")
    if failed:
        print("\nFAILures:")
        for r in results:
            if r.status == "FAIL":
                print(f"  [{r.section}] {r.name}: {r.evidence}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
