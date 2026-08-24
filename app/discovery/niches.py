"""Market -> niche classification, deliberately finer-grained than
app/collectors/categories.py's five top-level buckets.

That module collapses "sports" into one bucket for the paper engine's
event-clustering fallback, which is the right call for its purpose - but it
means an MMA specialist and an NBA specialist look identical to every piece
of code that only knows "Sports". This module exists to undo exactly that
collapse for wallet discovery, per the user's own framing: "for sub-sports
the tags won't distinguish MMA from NBA."

Two-tier classification, most confident first:

1. TAG MATCH - a market's Gamma tags (fetched via
   PolymarketClient.get_markets_by_condition_ids' include_tag=true, the
   same call app/collectors/markets.py already makes and discards the
   fine-grained tags from) contain a slug this module recognizes. Slugs
   marked VERIFIED below were fetched and inspected live against the real
   API on 2026-08-24 (ufc, nba, basketball, nba-finals, bitcoin,
   crypto-prices, soccer, ucl, champions-league, nhl, ice-hockey, esports,
   valorant, games). Slugs marked INFERRED follow the exact same
   lowercase-hyphenated exact-name convention the verified slugs
   demonstrate (nfl, mlb, boxing, tennis, golf, american-football,
   ethereum) but were not directly confirmed live in this session - a
   market carrying one is classified with MEDIUM confidence, not HIGH,
   until spot-checked.

2. TITLE KEYWORD FALLBACK - for a market whose tags say only "Sports" (or
   nothing niche-specific) but whose title clearly names a sport Polymarket
   didn't tag distinctly. Built for MMA specifically per the user's
   example ("UFC, weight classes, fighter names") since Polymarket's own
   "ufc" tag doesn't cover every MMA promotion (Bellator, ONE
   Championship, PFL). LOW confidence: self-reported pattern match, no
   ground truth, deliberately narrow (weight-class names are a strong MMA
   signal; a bare fighter name is not attempted, too high a false-positive
   risk against boxing).

Anything matching neither tier gets niche=None (not force-bucketed into
"Other" the way categories.py does - a market this module can't confidently
place should never contribute to a wallet's niche stats at all, per the
user's own instruction not to fake confidence).
"""

import re
from dataclasses import dataclass
from enum import StrEnum


class NicheMatchMethod(StrEnum):
    TAG_VERIFIED = "TAG_VERIFIED"
    TAG_INFERRED = "TAG_INFERRED"
    KEYWORD = "KEYWORD"


@dataclass(frozen=True)
class NicheMatch:
    niche: str
    method: NicheMatchMethod
    matched_on: str


# slug -> (niche, is_verified). One slug maps to exactly one niche - a
# market can carry several matching tags (e.g. both "nba" and "basketball"
# on the same market, seen live); the first hit in tag order wins, and
# since both map to the same niche here, order doesn't change the result
# for any pair currently in this table.
_NICHE_BY_TAG_SLUG: dict[str, tuple[str, bool]] = {
    "ufc": ("MMA", True),
    "mma": ("MMA", False),
    "bellator": ("MMA", False),
    "nba": ("NBA", True),
    "basketball": ("NBA", True),
    "nba-finals": ("NBA", True),
    "nfl": ("NFL", False),
    "american-football": ("NFL", False),
    "mlb": ("MLB", False),
    "baseball": ("MLB", False),
    "nhl": ("NHL", True),
    "ice-hockey": ("NHL", True),
    "soccer": ("Soccer", True),
    "ucl": ("Soccer", True),
    "champions-league": ("Soccer", True),
    "premier-league": ("Soccer", False),
    "boxing": ("Boxing", False),
    "tennis": ("Tennis", False),
    "golf": ("Golf", False),
    "esports": ("Esports", True),
    "valorant": ("Esports", True),
    "league-of-legends": ("Esports", False),
    "csgo": ("Esports", False),
    "counter-strike": ("Esports", False),
    "dota2": ("Esports", False),
    "bitcoin": ("Crypto-BTC", True),
    "ethereum": ("Crypto-ETH", False),
    "elections": ("Politics-Elections", False),
    "geopolitics": ("Politics-Geopolitics", False),
}

# Weight-class names are a strong, low-noise MMA signal on their own
# (rarely appear in a boxing or generic-sports title); "UFC"/"Bellator"/
# "ONE Championship"/"PFL" name the promotion directly. Case-insensitive.
_MMA_TITLE_PATTERN = re.compile(
    r"\b(UFC|Bellator|ONE Championship|PFL|"
    r"Flyweight|Bantamweight|Featherweight|Lightweight|Welterweight|"
    r"Middleweight|Light Heavyweight|Heavyweight)\b",
    re.IGNORECASE,
)


def classify_market_niche(
    tag_slugs: list[str], title: str, generic_sports_tag_present: bool
) -> NicheMatch | None:
    """Pure classification - no DB, no API. tag_slugs is every non-hidden
    tag slug on the market (already filtered by the caller the same way
    app/collectors/categories.py filters forceHide before matching).
    generic_sports_tag_present is passed separately (rather than re-derived
    from tag_slugs) so the keyword fallback only fires for markets Polymarket
    itself already called "Sports" but didn't sub-tag - never for a market
    with no sports signal at all, which keeps the MMA keyword match from
    ever firing on an unrelated market that happens to mention a weight-class
    word in some other context.
    """
    for slug in tag_slugs:
        hit = _NICHE_BY_TAG_SLUG.get(slug)
        if hit is not None:
            niche, verified = hit
            method = NicheMatchMethod.TAG_VERIFIED if verified else NicheMatchMethod.TAG_INFERRED
            return NicheMatch(niche=niche, method=method, matched_on=slug)

    if generic_sports_tag_present:
        keyword_hit = _MMA_TITLE_PATTERN.search(title)
        if keyword_hit is not None:
            return NicheMatch(
                niche="MMA", method=NicheMatchMethod.KEYWORD, matched_on=keyword_hit.group(0)
            )

    return None
