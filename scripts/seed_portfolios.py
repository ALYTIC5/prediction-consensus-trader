"""Seed the three baseline paper-trading portfolios (docs/PHASE4_DESIGN.md
section 1). Idempotent - creates a portfolio only if its name doesn't
already exist; never updates an existing one's params, since re-running
this after a portfolio has accumulated trades should never silently change
the rules it traded under.
"""

from decimal import Decimal

from sqlalchemy import select

from app.db.models import PaperPortfolio
from app.db.session import db_session

_STARTING_BANKROLL = Decimal("10000")

# Every value here is a paper_* setting override - see
# docs/PHASE4_DESIGN.md section 10 for what each key controls and its
# global default. A portfolio's params only need the keys it diverges on.
_SEED_PORTFOLIOS = {
    "baseline": {},
    "strict": {
        "paper_min_traders": 5,
        "paper_min_weighted_score": "2.0",
        "paper_min_liquidity_usd": "15000",
    },
    "conservative": {
        "paper_fixed_fraction_pct": "0.01",
        "paper_take_profit_pct": "0.15",
        "paper_stop_loss_pct": "0.10",
    },
}


def main() -> None:
    with db_session() as session:
        existing_names = set(session.execute(select(PaperPortfolio.name)).scalars().all())

        for name, params in _SEED_PORTFOLIOS.items():
            if name in existing_names:
                print(f"found: {name} (already exists, left untouched)")
                continue

            session.add(
                PaperPortfolio(
                    name=name,
                    params=params,
                    starting_bankroll=_STARTING_BANKROLL,
                    current_bankroll=_STARTING_BANKROLL,
                    is_active=True,
                )
            )
            print(f"created: {name} params={params} starting_bankroll={_STARTING_BANKROLL}")


if __name__ == "__main__":
    main()
