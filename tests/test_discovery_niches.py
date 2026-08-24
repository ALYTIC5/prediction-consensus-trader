from app.discovery.niches import NicheMatchMethod, classify_market_niche


def test_verified_tag_slug_wins_over_generic_sports() -> None:
    match = classify_market_niche(
        tag_slugs=["sports", "ufc", "games"],
        title="UFC Fight Night: Song Yadong vs. Umar Nurmagomedov",
        generic_sports_tag_present=True,
    )
    assert match is not None
    assert match.niche == "MMA"
    assert match.method == NicheMatchMethod.TAG_VERIFIED
    assert match.matched_on == "ufc"


def test_verified_tag_nba() -> None:
    match = classify_market_niche(
        tag_slugs=["nba-finals", "nba", "sports", "basketball"],
        title="Will Boston Celtics win the 2027 NBA Finals?",
        generic_sports_tag_present=True,
    )
    assert match is not None
    assert match.niche == "NBA"
    assert match.method == NicheMatchMethod.TAG_VERIFIED


def test_inferred_tag_slug_used_when_no_verified_slug_present() -> None:
    match = classify_market_niche(
        tag_slugs=["sports", "boxing"],
        title="Canelo Alvarez vs. Terence Crawford",
        generic_sports_tag_present=True,
    )
    assert match is not None
    assert match.niche == "Boxing"
    assert match.method == NicheMatchMethod.TAG_INFERRED


def test_mma_keyword_fallback_fires_only_with_generic_sports_tag() -> None:
    match = classify_market_niche(
        tag_slugs=["sports"],
        title="Will the Featherweight title fight go the distance?",
        generic_sports_tag_present=True,
    )
    assert match is not None
    assert match.niche == "MMA"
    assert match.method == NicheMatchMethod.KEYWORD
    assert match.matched_on.lower() == "featherweight"


def test_mma_keyword_fallback_never_fires_without_sports_signal() -> None:
    """A weight-class word appearing in some unrelated market title (e.g. a
    pop-culture market that happens to mention "Heavyweight" as a nickname)
    must not misclassify - the generic_sports_tag_present gate exists
    specifically to prevent this.
    """
    match = classify_market_niche(
        tag_slugs=["pop-culture"],
        title="Will the album be called 'Heavyweight'?",
        generic_sports_tag_present=False,
    )
    assert match is None


def test_no_match_returns_none_not_a_default_bucket() -> None:
    match = classify_market_niche(
        tag_slugs=["weather"],
        title="Will it rain in NYC tomorrow?",
        generic_sports_tag_present=False,
    )
    assert match is None


def test_hidden_tags_are_pre_filtered_by_caller() -> None:
    """This function trusts tag_slugs is already force_hide-filtered (see
    app/discovery/walk.py's _non_hidden_slugs) - a slug it recognizes still
    matches regardless of what the original tag's forceHide value was,
    since that filtering already happened upstream.
    """
    match = classify_market_niche(
        tag_slugs=["ufc"], title="UFC 300", generic_sports_tag_present=True
    )
    assert match is not None
    assert match.niche == "MMA"
