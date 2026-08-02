"""Unit tests for app/utils/pruning.py's pure config mapping - no DB, no
network. See the retention-pruning fix for the Railway Postgres volume
incident.
"""

from app.config.settings import Settings
from app.utils.pruning import PruneTarget, prune_targets_from_settings


def _settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "postgres_password": "secret",
        "redis_url": "redis://localhost:6379/0",
    }
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


def test_prune_targets_map_the_three_tables_to_their_own_timestamp_column():
    settings = _settings()
    targets = prune_targets_from_settings(settings)
    by_table = {t.table: t for t in targets}

    assert by_table["prices"].timestamp_column == "captured_at"
    assert by_table["market_history"].timestamp_column == "captured_at"
    assert by_table["position_history"].timestamp_column == "detected_at"


def test_prune_targets_use_each_tables_own_configured_retention():
    settings = _settings(
        prices_retention_days=7,
        market_history_retention_days=21,
        position_history_retention_days=45,
    )
    targets = {t.table: t for t in prune_targets_from_settings(settings)}

    assert targets["prices"] == PruneTarget("prices", "captured_at", 7)
    assert targets["market_history"] == PruneTarget("market_history", "captured_at", 21)
    assert targets["position_history"] == PruneTarget("position_history", "detected_at", 45)


def test_prune_targets_default_retention_matches_settings_defaults():
    settings = _settings()
    targets = {t.table: t for t in prune_targets_from_settings(settings)}

    assert targets["prices"].retention_days == 14
    assert targets["market_history"].retention_days == 30
    assert targets["position_history"].retention_days == 60
