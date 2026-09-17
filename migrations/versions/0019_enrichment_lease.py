"""Persist enrichment execution lease expiry.

Revision ID: 0019
Revises: 0018
"""

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "item_enrichment", sa.Column("lease_expires_at", sa.Text(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("item_enrichment", "lease_expires_at")
