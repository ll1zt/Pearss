"""add source.folder

Subscriptions had no grouping: the Reader API answered `subscription/list`
with a hardcoded `user/-/label/All` category for every source, so a client
sidebar was one undifferentiated list — real once 53 Zhihu subscriptions
landed in one sitting.

One folder per source, in the FreshRSS model (its own code says a feed can
only be in one category, and clients are built to that). Nullable would offer
two spellings of "no folder"; the empty default names the folder every
unclassified subscription actually sits in, and existing rows take exactly
that — which is where they were already rendered.

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-14

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("source") as batch:
        batch.add_column(
            sa.Column("folder", sa.Text(), nullable=False, server_default="default")
        )


def downgrade() -> None:
    with op.batch_alter_table("source") as batch:
        batch.drop_column("folder")
