from decimal import Decimal

from app.discovery.stats import GradeRecord, compute_wallet_niche_stat


def test_all_wins_gives_high_wilson_low_and_positive_roi() -> None:
    records = [GradeRecord(won=True, stake=Decimal(10), price=Decimal("0.5")) for _ in range(20)]
    result = compute_wallet_niche_stat(records)
    assert result.resolved_n == 20
    assert result.wins == 20
    assert result.wilson_low > Decimal("0.8")
    assert result.roi is not None
    assert result.roi > 0


def test_all_losses_gives_roi_of_minus_one() -> None:
    records = [GradeRecord(won=False, stake=Decimal(10), price=Decimal("0.5")) for _ in range(20)]
    result = compute_wallet_niche_stat(records)
    assert result.wins == 0
    assert result.roi == Decimal(-1)


def test_empty_records_gives_wide_open_interval_and_no_roi() -> None:
    result = compute_wallet_niche_stat([])
    assert result.resolved_n == 0
    assert result.wilson_low == Decimal("0")
    assert result.wilson_high == Decimal("1")
    assert result.roi is None


def test_win_at_low_price_pays_more_than_win_at_high_price() -> None:
    """A won bet at price 0.2 (long-shot) pays (1-0.2)/0.2 = 4x stake; a won
    bet at price 0.8 (favorite) pays only (1-0.8)/0.8 = 0.25x stake - the
    payout asymmetry a probability-priced market always has.
    """
    longshot = compute_wallet_niche_stat(
        [GradeRecord(won=True, stake=Decimal(10), price=Decimal("0.2"))]
    )
    favorite = compute_wallet_niche_stat(
        [GradeRecord(won=True, stake=Decimal(10), price=Decimal("0.8"))]
    )
    assert longshot.roi is not None
    assert favorite.roi is not None
    assert longshot.roi > favorite.roi


def test_mixed_wins_and_losses_nets_correctly() -> None:
    records = [
        GradeRecord(won=True, stake=Decimal(10), price=Decimal("0.5")),  # +10
        GradeRecord(won=False, stake=Decimal(10), price=Decimal("0.5")),  # -10
    ]
    result = compute_wallet_niche_stat(records)
    assert result.roi == Decimal(0)
