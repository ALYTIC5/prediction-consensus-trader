"""consensus v2 tables

Revision ID: b2d3f4a5c6e7
Revises: a1c2e3f4b5d6
Create Date: 2026-08-18 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2d3f4a5c6e7"
down_revision: str | None = "a1c2e3f4b5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "signal_prob",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("condition_id", sa.String(length=66), nullable=False),
        sa.Column("asset", sa.String(length=80), nullable=False),
        sa.Column("outcome", sa.String(length=100), nullable=False),
        sa.Column("event_slug", sa.String(length=300), nullable=True),
        sa.Column("p_consensus", sa.Numeric(precision=24, scale=6), nullable=False),
        sa.Column("p_market", sa.Numeric(precision=24, scale=6), nullable=False),
        sa.Column("divergence", sa.Numeric(precision=24, scale=6), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=24, scale=6), nullable=False),
        sa.Column("n_clusters", sa.Integer(), nullable=False),
        sa.Column("total_weight_usd", sa.Numeric(precision=24, scale=6), nullable=False),
        sa.Column("liquidity", sa.Numeric(precision=24, scale=6), nullable=True),
        sa.Column("spread", sa.Numeric(precision=24, scale=6), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("outcome_value", sa.Numeric(precision=24, scale=6), nullable=True),
        sa.Column("brier_consensus", sa.Numeric(precision=24, scale=6), nullable=True),
        sa.Column("brier_market", sa.Numeric(precision=24, scale=6), nullable=True),
        sa.Column("paired_diff", sa.Numeric(precision=24, scale=6), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_signal_prob")),
    )
    op.create_index(
        "ix_signal_prob_condition_id_created_at", "signal_prob", ["condition_id", "created_at"]
    )

    op.create_table(
        "signal_prob_trades",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("portfolio_id", sa.Integer(), nullable=False),
        sa.Column("signal_prob_id", sa.Integer(), nullable=False),
        sa.Column("condition_id", sa.String(length=66), nullable=False),
        sa.Column("asset", sa.String(length=80), nullable=False),
        sa.Column("outcome", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("entry_price", sa.Numeric(precision=24, scale=6), nullable=False),
        sa.Column("size", sa.Numeric(precision=24, scale=6), nullable=False),
        sa.Column("fee_paid", sa.Numeric(precision=24, scale=6), nullable=False),
        sa.Column("entry_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("current_price", sa.Numeric(precision=24, scale=6), nullable=True),
        sa.Column("exit_price", sa.Numeric(precision=24, scale=6), nullable=True),
        sa.Column("realized_pnl", sa.Numeric(precision=24, scale=6), nullable=True),
        sa.Column("exit_reason", sa.String(length=20), nullable=True),
        sa.Column("exit_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["portfolio_id"],
            ["paper_portfolios.id"],
            name=op.f("fk_signal_prob_trades_portfolio_id_paper_portfolios"),
        ),
        sa.ForeignKeyConstraint(
            ["signal_prob_id"],
            ["signal_prob.id"],
            name=op.f("fk_signal_prob_trades_signal_prob_id_signal_prob"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_signal_prob_trades")),
    )
    op.create_index(
        "ix_signal_prob_trades_portfolio_id_status",
        "signal_prob_trades",
        ["portfolio_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_signal_prob_trades_portfolio_id_status", table_name="signal_prob_trades")
    op.drop_table("signal_prob_trades")
    op.drop_index("ix_signal_prob_condition_id_created_at", table_name="signal_prob")
    op.drop_table("signal_prob")
