"""order books and paper_trades fill_method

Revision ID: a861ae5f413c
Revises: bef0cc997e3b
Create Date: 2026-08-14 08:48:07.197699

order_books backs app/paper/fills.py's walk_the_book() - one row per
(condition_id, asset, side) snapshot, levels as JSONB. fill_method on
paper_trades records whether a fill used a real book walk (BOOK_WALK) or
fell back to ask+slippage (ESTIMATED) - see app/paper/engine.py.

Autogenerate again surfaced the same unrelated pre-existing drift noted in
bef0cc997e3b (missing ix_prices_asset_captured_at) - left out here too,
still unrelated to this change, still pending its own migration.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a861ae5f413c"
down_revision: str | None = "bef0cc997e3b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "order_books",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("condition_id", sa.String(length=66), nullable=False),
        sa.Column("asset", sa.String(length=80), nullable=False),
        sa.Column("side", sa.String(length=4), nullable=False),
        sa.Column("levels", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "captured_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_order_books")),
    )
    op.create_index(
        "ix_order_books_condition_id_asset_captured_at",
        "order_books",
        ["condition_id", "asset", "captured_at"],
        unique=False,
    )
    op.add_column("paper_trades", sa.Column("fill_method", sa.String(length=10), nullable=True))


def downgrade() -> None:
    op.drop_column("paper_trades", "fill_method")
    op.drop_index("ix_order_books_condition_id_asset_captured_at", table_name="order_books")
    op.drop_table("order_books")
