"""add retry bookkeeping to item_enrichment (P3: concurrency + fallback)

Transient provider failures (network, rate-limit) go back to queued with
attempt_count incremented and a backoff timestamp; non-transient failures
stop in `failed`. attempt_count also bounds the retries (MAX_ATTEMPTS).
`model` records which model actually produced the artifact - multi-model
fallback makes that worth pinning.

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-09

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "item_enrichment",
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "item_enrichment", sa.Column("next_attempt_at", sa.Text(), nullable=True)
    )
    op.add_column(
        "item_enrichment", sa.Column("model", sa.String(64), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("item_enrichment", "model")
    op.drop_column("item_enrichment", "next_attempt_at")
    op.drop_column("item_enrichment", "attempt_count")
