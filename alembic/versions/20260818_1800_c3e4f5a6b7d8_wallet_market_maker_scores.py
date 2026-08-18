"""wallet market maker scores

Revision ID: c3e4f5a6b7d8
Revises: b2d3f4a5c6e7
Create Date: 2026-08-18 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3e4f5a6b7d8"
down_revision: str | None = "b2d3f4a5c6e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "wallet_market_maker_scores",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("wallet_id", sa.Integer(), nullable=False),
        sa.Column("holding_period_component", sa.Numeric(precision=24, scale=6), nullable=False),
        sa.Column("both_sides_component", sa.Numeric(precision=24, scale=6), nullable=False),
        sa.Column("breadth_depth_component", sa.Numeric(precision=24, scale=6), nullable=False),
        sa.Column("score", sa.Numeric(precision=24, scale=6), nullable=False),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["wallet_id"],
            ["wallets.id"],
            name=op.f("fk_wallet_market_maker_scores_wallet_id_wallets"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_wallet_market_maker_scores")),
    )
    op.create_index(
        "ix_wallet_market_maker_scores_wallet_id_computed_at",
        "wallet_market_maker_scores",
        ["wallet_id", "computed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_wallet_market_maker_scores_wallet_id_computed_at",
        table_name="wallet_market_maker_scores",
    )
    op.drop_table("wallet_market_maker_scores")
