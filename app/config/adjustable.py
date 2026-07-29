"""Registry of Settings fields tunable from the dashboard - pure, no DB, no network.

See docs/PHASE8_DESIGN.md flag 5: the consensus_*/signal_* threshold fields
are live-tunable; the *_interval_seconds fields are listed for visibility but
require a restart (the scheduler reads them once at startup). Extended by
docs/PHASE5_DESIGN.md flag 9 with paper_emergency_stop (the manual kill
switch) and risk_default_sizer (the fleet-wide fallback sizer). Nothing else
appears here - DB credentials and API bases are never adjustable or
displayed, and everything else (scoring weights, lookback days,
tracked_wallets_limit, per-portfolio params, ...) is simply out of scope for
the Tuning page.
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


@dataclass(frozen=True)
class AdjustableField:
    """One Settings field the dashboard can show and, if not
    restart_required, let an operator override at runtime.

    choices: for a str-typed field restricted to an enum of legal values
    (e.g. risk_default_sizer) - None for every other type, where min/max
    already bound the legal range.
    """

    type: type
    min: Decimal | int | None
    max: Decimal | int | None
    description: str
    restart_required: bool = False
    choices: tuple[str, ...] | None = None


# Insertion order is the display order on the Tuning page - a plain dict
# already preserves it, no need for collections.OrderedDict.
ADJUSTABLE: dict[str, AdjustableField] = {
    "consensus_min_traders": AdjustableField(
        type=int,
        min=1,
        max=50,
        description="Minimum number of distinct tracked traders required to form a signal.",
    ),
    "consensus_min_weighted_score": AdjustableField(
        type=Decimal,
        min=Decimal("0"),
        max=Decimal("50"),
        description="Minimum combined trader-score weight required to form a signal.",
    ),
    "consensus_min_combined_value_usd": AdjustableField(
        type=Decimal,
        min=Decimal("0"),
        max=Decimal("1000000"),
        description="Minimum total dollar value moved by contributing trades to form a signal.",
    ),
    "consensus_freshness_hours": AdjustableField(
        type=int,
        min=1,
        max=336,
        description="How far back to look for fresh position-open/increase events.",
    ),
    "consensus_include_increases": AdjustableField(
        type=bool,
        min=None,
        max=None,
        description="Whether INCREASED events count toward consensus, not just OPENED.",
    ),
    "signal_min_liquidity_usd": AdjustableField(
        type=Decimal,
        min=Decimal("0"),
        max=Decimal("10000000"),
        description="Minimum market liquidity required before a signal can fire.",
    ),
    "signal_min_volume_24h_usd": AdjustableField(
        type=Decimal,
        min=Decimal("0"),
        max=Decimal("10000000"),
        description="Minimum 24h trading volume required before a signal can fire.",
    ),
    "signal_price_min": AdjustableField(
        type=Decimal,
        min=Decimal("0"),
        max=Decimal("1"),
        description="Lowest outcome price a signal may fire at (avoids lottery tickets).",
    ),
    "signal_price_max": AdjustableField(
        type=Decimal,
        min=Decimal("0"),
        max=Decimal("1"),
        description="Highest outcome price a signal may fire at (avoids near-certainties).",
    ),
    "signal_max_spread": AdjustableField(
        type=Decimal,
        min=Decimal("0"),
        max=Decimal("1"),
        description="Widest bid/ask spread a market may have before a signal is rejected.",
    ),
    "signal_min_hours_to_end": AdjustableField(
        type=int,
        min=0,
        max=8760,
        description="Minimum hours until market close required for a signal to fire.",
    ),
    "signal_ttl_hours": AdjustableField(
        type=int,
        min=1,
        max=8760,
        description="How long a signal stays ACTIVE before it expires on its own.",
    ),
    "leaderboard_interval_seconds": AdjustableField(
        type=int,
        min=None,
        max=None,
        description="How often the leaderboard collector runs.",
        restart_required=True,
    ),
    "positions_interval_seconds": AdjustableField(
        type=int,
        min=None,
        max=None,
        description="How often the positions collector runs.",
        restart_required=True,
    ),
    "markets_interval_seconds": AdjustableField(
        type=int,
        min=None,
        max=None,
        description="How often the markets collector runs.",
        restart_required=True,
    ),
    "consensus_interval_seconds": AdjustableField(
        type=int,
        min=None,
        max=None,
        description="How often the consensus/signal generator runs.",
        restart_required=True,
    ),
    # --- Phase 5: risk management (docs/PHASE5_DESIGN.md flag 9) ---
    "paper_emergency_stop": AdjustableField(
        type=bool,
        min=None,
        max=None,
        description=(
            "Manual kill switch - denies ALL new paper entries across every "
            "portfolio immediately when true."
        ),
    ),
    "risk_default_sizer": AdjustableField(
        type=str,
        min=None,
        max=None,
        choices=("FIXED", "TIERED", "KELLY"),
        description=(
            "Fleet-wide fallback sizer for portfolios whose params don't set their own paper_sizer."
        ),
    ),
}


def parse_and_validate(key: str, raw_value: str) -> Any:
    """Parse a raw override string against the registry, or raise ValueError
    with a human-readable message.

    Used both by the (future) tuning POST handler before writing
    runtime_overrides, and defensively by app/config/effective.py on every
    read - the message is written to be shown to a human either way.
    """
    field = ADJUSTABLE.get(key)
    if field is None:
        raise ValueError(f"{key!r} is not an adjustable setting")

    value = _parse_value(key, raw_value, field.type)

    if field.choices is not None and value not in field.choices:
        raise ValueError(f"{key} must be one of {field.choices}, got {value!r}")
    if field.min is not None and value < field.min:
        raise ValueError(f"{key} must be >= {field.min}, got {value}")
    if field.max is not None and value > field.max:
        raise ValueError(f"{key} must be <= {field.max}, got {value}")

    return value


def _parse_value(key: str, raw_value: str, expected_type: type) -> Any:
    if expected_type is bool:
        lowered = raw_value.strip().lower()
        if lowered in ("true", "1", "yes"):
            return True
        if lowered in ("false", "0", "no"):
            return False
        raise ValueError(f"{key} must be a boolean, got {raw_value!r}")
    if expected_type is int:
        try:
            return int(raw_value)
        except ValueError as exc:
            raise ValueError(f"{key} must be an integer, got {raw_value!r}") from exc
    if expected_type is Decimal:
        try:
            return Decimal(raw_value)
        except InvalidOperation as exc:
            raise ValueError(f"{key} must be a number, got {raw_value!r}") from exc
    if expected_type is str:
        return raw_value.strip()
    raise ValueError(f"{key} has an unsupported type {expected_type!r}")
