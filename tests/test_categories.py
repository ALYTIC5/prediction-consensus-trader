"""Unit tests for app/collectors/categories.py's normalize_category - pure,
no DB, no network. See docs/PHASE6_DESIGN.md workstream 3's per-category
extension.
"""

from app.collectors.categories import DEFAULT_CATEGORY, normalize_category
from app.collectors.schemas import GammaTag


def _tag(slug: str | None, force_hide: bool = False) -> GammaTag:
    return GammaTag(id="1", label=slug, slug=slug, forceHide=force_hide)


def test_known_slug_maps_to_its_category():
    assert normalize_category([_tag("sports")]) == "Sports"
    assert normalize_category([_tag("crypto")]) == "Crypto"
    assert normalize_category([_tag("pop-culture")]) == "Pop-Culture"
    assert normalize_category([_tag("politics")]) == "Politics"


def test_synonym_slugs_fold_into_the_nearest_bucket():
    assert normalize_category([_tag("esports")]) == "Sports"
    assert normalize_category([_tag("entertainment")]) == "Pop-Culture"
    assert normalize_category([_tag("elections")]) == "Politics"
    assert normalize_category([_tag("geopolitics")]) == "Politics"


def test_unrecognised_slug_falls_back_to_other():
    assert normalize_category([_tag("weather")]) == DEFAULT_CATEGORY
    assert normalize_category([_tag("business")]) == DEFAULT_CATEGORY


def test_no_tags_falls_back_to_other():
    assert normalize_category([]) == DEFAULT_CATEGORY


def test_force_hidden_tag_is_skipped():
    """A market carrying a stray, Polymarket-suppressed "Politics" tag
    alongside a genuine, visible "Culture" tag should classify as
    Pop-Culture, not Politics - the live example this project verified this
    against (a Rihanna-album-vs-GTA-VI market) carried exactly this shape.
    """
    tags = [_tag("politics", force_hide=True), _tag("pop-culture", force_hide=False)]
    assert normalize_category(tags) == "Pop-Culture"


def test_first_matching_visible_tag_wins_when_several_match():
    tags = [_tag("sports"), _tag("crypto")]
    assert normalize_category(tags) == "Sports"


def test_all_tags_hidden_falls_back_to_other():
    tags = [_tag("sports", force_hide=True), _tag("politics", force_hide=True)]
    assert normalize_category(tags) == DEFAULT_CATEGORY
