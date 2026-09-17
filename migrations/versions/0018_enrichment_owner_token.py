"""add enrichment claim ownership token (A10)

Enrichment claims were a status flip without an owner: the claim UPDATE
matched any `running` row, so a sweeper that enumerated a candidate before
another worker claimed it still overwrote the fresh claim and processed the
row twice. Claims now record an owner token, the claim UPDATE requires the
pre-claim eligibility it observed, and result writes are fenced by the token
-- a late completion from a former owner is rejected instead of overwriting
the current owner's work.

Existing rows have no token; NULL is legal and means "not owned". No data
migration of statuses is needed.

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-17

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "item_enrichment",
        sa.Column("owner_token", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("item_enrichment", "owner_token")
