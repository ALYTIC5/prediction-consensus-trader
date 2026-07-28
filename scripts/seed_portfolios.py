"""Seed paper-trading portfolios from the canonical STRATEGIES list.

Idempotent by default — creates a portfolio only if its name doesn't already
exist, never updates an existing one's params (re-running after a portfolio
has accumulated trades should never silently change the rules it traded under).

Use --force to overwrite params on existing portfolios (safe when no trades
have been made yet, or when you explicitly want to retroactively change a
strategy's definition).
"""

import argparse
from decimal import Decimal

from sqlalchemy import select

from app.db.models import PaperPortfolio
from app.db.session import db_session
from app.paper.strategies import STRATEGIES

_STARTING_BANKROLL = Decimal("100")


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed paper-trading portfolios")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite params on existing portfolios (use with care)",
    )
    args = parser.parse_args()

    with db_session() as session:
        existing = {
            row.name: row for row in session.execute(select(PaperPortfolio)).scalars().all()
        }

        for strategy in STRATEGIES:
            name = strategy.name
            params = strategy.to_params()

            if name in existing:
                portfolio = existing[name]
                if args.force:
                    portfolio.params = params
                    print(f"updated: {name}")
                else:
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
            print(f"created: {name} params={params}")


if __name__ == "__main__":
    main()
