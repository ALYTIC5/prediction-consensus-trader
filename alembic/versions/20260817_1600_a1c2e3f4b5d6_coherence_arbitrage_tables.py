"""coherence arbitrage tables

Revision ID: a1c2e3f4b5d6
Revises: 562bccc6041a
Create Date: 2026-08-17 16:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1c2e3f4b5d6"
down_revision: str | None = "562bccc6041a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "coherence_opportunities",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("opportunity_key", sa.String(length=80), nullable=False),
        sa.Column("type", sa.String(length=20), nullable=False),
        sa.Column("legs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("gross_spread", sa.Numeric(precision=24, scale=6), nullable=False),
        sa.Column("size", sa.Numeric(precision=24, scale=6), nullable=True),
        sa.Column("net_profit", sa.Numeric(precision=24, scale=6), nullable=True),
        sa.Column("required_capital", sa.Numeric(precision=24, scale=6), nullable=True),
        sa.Column(
            "detected_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("captured", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_coherence_opportunities")),
    )
    op.create_index(
        op.f("ix_coherence_opportunities_opportunity_key"),
        "coherence_opportunities",
        ["opportunity_key"],
        unique=True,
    )
    op.create_index(
        "ix_coherence_opportunities_resolved_at", "coherence_opportunities", ["resolved_at"]
    )
    op.create_index(
        "ix_coherence_opportunities_type_detected_at",
        "coherence_opportunities",
        ["type", "detected_at"],
    )

    op.create_table(
        "coherence_fills",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("opportunity_id", sa.Integer(), nullable=False),
        sa.Column("portfolio_id", sa.Integer(), nullable=False),
        sa.Column("condition_id", sa.String(length=66), nullable=False),
        sa.Column("asset", sa.String(length=80), nullable=False),
        sa.Column("outcome", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("entry_price", sa.Numeric(precision=24, scale=6), nullable=False),
        sa.Column("size", sa.Numeric(precision=24, scale=6), nullable=False),
        sa.Column("fee_paid", sa.Numeric(precision=24, scale=6), nullable=False),
        sa.Column("entry_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("exit_price", sa.Numeric(precision=24, scale=6), nullable=True),
        sa.Column("realized_pnl", sa.Numeric(precision=24, scale=6), nullable=True),
        sa.Column("exit_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["opportunity_id"],
            ["coherence_opportunities.id"],
            name=op.f("fk_coherence_fills_opportunity_id_coherence_opportunities"),
        ),
        sa.ForeignKeyConstraint(
            ["portfolio_id"],
            ["paper_portfolios.id"],
            name=op.f("fk_coherence_fills_portfolio_id_paper_portfolios"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_coherence_fills")),
    )
    op.create_index(
        "ix_coherence_fills_portfolio_id_status", "coherence_fills", ["portfolio_id", "status"]
    )


def downgrade() -> None:
    op.drop_index("ix_coherence_fills_portfolio_id_status", table_name="coherence_fills")
    op.drop_table("coherence_fills")
    op.drop_index(
        "ix_coherence_opportunities_type_detected_at", table_name="coherence_opportunities"
    )
    op.drop_index("ix_coherence_opportunities_resolved_at", table_name="coherence_opportunities")
    op.drop_index(
        op.f("ix_coherence_opportunities_opportunity_key"), table_name="coherence_opportunities"
    )
    op.drop_table("coherence_opportunities")
