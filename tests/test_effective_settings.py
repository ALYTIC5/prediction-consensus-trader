"""Unit tests for app/config/effective.py's overlay logic - pure, no DB.

_apply_overrides is the pure half of get_effective_settings() (the other
half is a plain DB read) - split out specifically so this is testable
without a database, same pattern as every other DB/pure split in this
codebase (app/consensus/scoring.py vs scorer.py, engine.py vs generator.py).
"""

from decimal import Decimal

from app.config.effective import _apply_overrides
from app.config.settings import Settings


def _base_settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "postgres_password": "secret",
        "redis_url": "redis://localhost:6379/0",
    }
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


def test_valid_override_applies() -> None:
    base = _base_settings()
    effective = _apply_overrides(base, {"consensus_min_traders": "7"})

    assert effective.consensus_min_traders == 7
    assert base.consensus_min_traders == 3  # base object itself is untouched


def test_invalid_stored_value_is_ignored_and_base_is_retained() -> None:
    base = _base_settings()
    effective = _apply_overrides(base, {"consensus_min_traders": "not-a-number"})

    assert effective.consensus_min_traders == base.consensus_min_traders == 3


def test_unknown_key_is_ignored() -> None:
    base = _base_settings()
    effective = _apply_overrides(base, {"totally_unknown_setting": "5"})

    assert effective.consensus_min_traders == base.consensus_min_traders


def test_out_of_bounds_value_is_ignored() -> None:
    base = _base_settings()
    effective = _apply_overrides(base, {"consensus_min_traders": "0"})

    assert effective.consensus_min_traders == base.consensus_min_traders == 3


def test_multiple_overrides_applied_together() -> None:
    base = _base_settings()
    effective = _apply_overrides(
        base,
        {"consensus_min_traders": "10", "signal_price_min": "0.10"},
    )

    assert effective.consensus_min_traders == 10
    assert effective.signal_price_min == Decimal("0.10")


def test_no_overrides_returns_equivalent_settings() -> None:
    base = _base_settings()
    effective = _apply_overrides(base, {})

    assert effective.consensus_min_traders == base.consensus_min_traders
    assert effective.signal_price_min == base.signal_price_min


def test_applying_a_valid_override_changes_effective_settings() -> None:
    """Simulates the Tuning page's apply flow: app/dashboard/queries.py's
    apply_override() writes a runtime_overrides row, and the very next
    get_effective_settings() call must reflect it - this is the "takes
    effect next cycle, no restart" guarantee, exercised at the pure layer.
    """
    base = _base_settings()
    before = _apply_overrides(base, {})
    after = _apply_overrides(base, {"consensus_min_traders": "9"})

    assert before.consensus_min_traders == 3
    assert after.consensus_min_traders == 9


def test_resetting_an_override_restores_the_default() -> None:
    """Simulates the Tuning page's reset flow: app/dashboard/queries.py's
    reset_override() deletes the runtime_overrides row, which is exactly
    the same as that key being absent from the overrides dict here.
    """
    base = _base_settings()
    overridden = _apply_overrides(base, {"consensus_min_traders": "9"})
    reset = _apply_overrides(base, {})

    assert overridden.consensus_min_traders == 9
    assert reset.consensus_min_traders == base.consensus_min_traders == 3
