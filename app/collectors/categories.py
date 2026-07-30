"""Market category normalization from Gamma tags.

Verified live against the Gamma API, not guessed: GET /markets?
condition_ids=...&include_tag=true (adding include_tag=true to the EXISTING
/markets call the collector already makes - no new endpoint) returns a
"tags" array per market with real (id, label, slug) triples. A clean
top-level `category` string field does exist on Polymarket's side, but only
on the standalone GET /events endpoint, which the collector does not
otherwise call - the tags-based approach below needs zero new API surface,
matching this project's established minimal-new-surface convention (e.g.
networkx-only over networkx + python-louvain in Workstream 1).

A market's tags mix genuine top-level categories with noise. One live
example (a market about a Rihanna album vs. GTA VI) carried "Politics" (id
2, forceHide=true), "Culture" (pop-culture, id 596, visible), "All" (id
100215, generic/navigational), and "GTA VI" (id 102109, market-specific)
all at once - a market that is not actually about politics. forceHide=true
tags are excluded before matching (Polymarket itself suppresses them from
its own UI, so a stray "Politics" tag on an unrelated market shouldn't
win); the first remaining tag whose slug matches a known top-level category
wins.

Slug -> normalized category, each slug verified via GET /tags/slug/{slug}
against the live API:
- sports, esports -> Sports (esports doesn't get its own bucket among the
  requested five, so it's folded into the nearest one)
- crypto -> Crypto
- pop-culture, entertainment -> Pop-Culture
- politics, elections, geopolitics -> Politics (elections/geopolitics are
  politics-adjacent enough to fold in rather than invent a sixth bucket)
- business, science, tech, world, economy, finance, weather (all confirmed
  real slugs) -> Other, since none of the requested five buckets is a
  natural home for them
"""

from app.collectors.schemas import GammaTag

_CATEGORY_BY_SLUG: dict[str, str] = {
    "sports": "Sports",
    "esports": "Sports",
    "crypto": "Crypto",
    "pop-culture": "Pop-Culture",
    "entertainment": "Pop-Culture",
    "politics": "Politics",
    "elections": "Politics",
    "geopolitics": "Politics",
}

DEFAULT_CATEGORY = "Other"


def normalize_category(tags: list[GammaTag]) -> str:
    """The first non-hidden tag whose slug maps to a known top-level
    category; DEFAULT_CATEGORY if none match (or the market has no tags at
    all - some markets genuinely carry no top-level tag).
    """
    for tag in tags:
        if tag.force_hide or tag.slug is None:
            continue
        category = _CATEGORY_BY_SLUG.get(tag.slug)
        if category is not None:
            return category
    return DEFAULT_CATEGORY
