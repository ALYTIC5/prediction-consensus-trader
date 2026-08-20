"""market resolutions table

Revision ID: d4e5f6a7b8c9
Revises: c3e4f5a6b7d8
Create Date: 2026-08-19 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4e5f6a7b8c9"
down_revision: str | None = "c3e4f5a6b7d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "market_resolutions",
        sa.Column("condition_id", sa.String(length=66), nullable=False),
        sa.Column("winning_asset", sa.String(length=80), nullable=True),
        sa.Column("winning_outcome_index", sa.Integer(), nullable=True),
        sa.Column("outcome_prices", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("uma_resolution_status", sa.String(length=32), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("condition_id", name=op.f("pk_market_resolutions")),
    )


def downgrade() -> None:
    op.drop_table("market_resolutions")
