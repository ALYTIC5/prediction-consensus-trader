"""Print ACTIVE consensus signals as an aligned text table.

Read-only reporting script. No new dependencies - plain string formatting,
no table-rendering library.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.db.models import Market, Signal, SignalStatus
from app.db.session import db_session

_HEADERS = (
    "AGE",
    "TITLE",
    "OUTCOME",
    "TRADERS",
    "WEIGHTED",
    "VALUE",
    "ENTRY",
    "CURRENT",
    "LIQUIDITY",
    "EXPIRES",
)
_RIGHT_ALIGN = {"TRADERS", "WEIGHTED", "VALUE", "ENTRY", "CURRENT", "LIQUIDITY", "EXPIRES"}
_TITLE_MAX_LEN = 40


def _format_duration(seconds: float) -> str:
    """e.g. 1740.0 -> "29m", 90000.0 -> "1d1h". Negative clamps to "0m"."""
    if seconds < 0:
        return "0m"
    total_minutes = int(seconds // 60)
    days, remaining_minutes = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(remaining_minutes, 60)
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes}m"
    return f"{minutes}m"


def _truncate(text: str, max_len: int) -> str:
    return text if len(text) <= max_len else text[: max_len - 3] + "..."


def _build_row(signal: Signal, market: Market | None, now: datetime) -> tuple[str, ...]:
    liquidity = market.liquidity if market and market.liquidity is not None else None
    return (
        _format_duration((now - signal.created_at).total_seconds()),
        _truncate(signal.title, _TITLE_MAX_LEN),
        signal.outcome,
        str(signal.distinct_traders),
        f"{signal.weighted_score:.2f}",
        f"${signal.combined_entry_value:,.0f}",
        f"{signal.average_entry_price:.3f}",
        f"{signal.latest_market_price:.3f}",
        f"${liquidity:,.0f}" if liquidity is not None else "-",
        _format_duration((signal.expires_at - now).total_seconds()),
    )


def _print_table(rows: list[tuple[str, ...]]) -> None:
    widths = [max(len(_HEADERS[i]), *(len(row[i]) for row in rows)) for i in range(len(_HEADERS))]

    def _format_line(cells: tuple[str, ...]) -> str:
        parts = []
        for i, cell in enumerate(cells):
            width = widths[i]
            parts.append(cell.rjust(width) if _HEADERS[i] in _RIGHT_ALIGN else cell.ljust(width))
        return "  ".join(parts)

    print(_format_line(_HEADERS))
    print(_format_line(tuple("-" * w for w in widths)))
    for row in rows:
        print(_format_line(row))


def main() -> None:
    now = datetime.now(UTC)

    with db_session() as session:
        signals = (
            session.execute(
                select(Signal)
                .where(Signal.status == SignalStatus.ACTIVE)
                .order_by(Signal.weighted_score.desc(), Signal.combined_entry_value.desc())
            )
            .scalars()
            .all()
        )

        condition_ids = {signal.condition_id for signal in signals}
        markets: dict[str, Market] = {}
        if condition_ids:
            markets = {
                market.condition_id: market
                for market in session.execute(
                    select(Market).where(Market.condition_id.in_(condition_ids))
                ).scalars()
            }

        expired_24h = session.execute(
            select(func.count())
            .select_from(Signal)
            .where(
                Signal.status == SignalStatus.EXPIRED,
                Signal.updated_at >= now - timedelta(hours=24),
            )
        ).scalar_one()

    if not signals:
        print("No active signals.")
    else:
        rows = [_build_row(signal, markets.get(signal.condition_id), now) for signal in signals]
        _print_table(rows)

    print(f"\n{expired_24h} signal(s) expired in the last 24h.")


if __name__ == "__main__":
    main()
