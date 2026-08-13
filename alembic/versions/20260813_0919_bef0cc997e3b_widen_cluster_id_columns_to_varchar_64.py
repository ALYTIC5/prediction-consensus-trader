"""widen cluster_id columns to varchar(64)

Revision ID: bef0cc997e3b
Revises: 2b1294bd28e6
Create Date: 2026-08-13 09:19:42.517424

Fixes the defect behind the collectors/bandit job's silent, permanent
failure: app/optimization/bandit.py's singleton-cluster fallback used to
write a wallet's raw address (up to 42 chars) into cluster_bandit_state.
cluster_id, a VARCHAR(16) column sized only for a real cluster_id_for()
hash - every insert overflowed it and rolled back the whole batch. The
actual fix is the fallback itself (now a bounded, deterministic hash - see
app/optimization/bandit.py's _resolve_cluster_id()); this migration widens
both columns that ever store a cluster_id so the same class of bug can't
recur even from a future caller's mistake.

Autogenerate also surfaced an unrelated pre-existing drift (a missing
ix_prices_asset_captured_at index the model declares but the live database
doesn't have) - left out of this migration since it's unrelated to the
cluster_id fix; worth its own migration separately.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "bef0cc997e3b"
down_revision: str | None = "2b1294bd28e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "cluster_bandit_state",
        "cluster_id",
        existing_type=sa.VARCHAR(length=16),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "wallet_clusters",
        "cluster_id",
        existing_type=sa.VARCHAR(length=16),
        type_=sa.String(length=64),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "wallet_clusters",
        "cluster_id",
        existing_type=sa.String(length=64),
        type_=sa.VARCHAR(length=16),
        existing_nullable=False,
    )
    op.alter_column(
        "cluster_bandit_state",
        "cluster_id",
        existing_type=sa.String(length=64),
        type_=sa.VARCHAR(length=16),
        existing_nullable=False,
    )
