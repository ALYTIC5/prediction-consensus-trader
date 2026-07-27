"""Overlay runtime_overrides on top of the base Settings, fresh every call.

Not cached like get_settings() - the whole point is that a tuning change
takes effect on the very next scorer/generator cycle without a restart. See
docs/PHASE8_DESIGN.md flags 3-4.
"""

import logging

from sqlalchemy import select

from app.config.adjustable import parse_and_validate
from app.config.settings import Settings, get_settings
from app.db.models import RuntimeOverride
from app.db.session import db_session

logger = logging.getLogger(__name__)


def get_effective_settings() -> Settings:
    """Base Settings, overlaid with every valid, registered runtime override."""
    base = get_settings()

    with db_session() as session:
        rows = session.execute(select(RuntimeOverride)).scalars().all()

    raw_overrides = {row.key: row.value for row in rows}
    return _apply_overrides(base, raw_overrides)


def _apply_overrides(base: Settings, raw_overrides: dict[str, str]) -> Settings:
    """Pure: validate raw (key, value) pairs and overlay the valid ones onto
    base. Split out from get_effective_settings() so this logic is
    unit-testable without a database.
    """
    valid_overrides: dict[str, object] = {}
    for key, raw_value in raw_overrides.items():
        try:
            valid_overrides[key] = parse_and_validate(key, raw_value)
        except ValueError as exc:
            logger.warning("effective settings: ignoring invalid override %s: %s", key, exc)

    return base.model_copy(update=valid_overrides)
