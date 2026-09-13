"""add item.author

Every collector already extracts the byline -- an RSS <author>, a Zhihu
display name, an X "Name (@handle)" -- and reports it as
`ItemDraft.metadata["author"]`. Nothing read it: `normalize()` never looked at
draft metadata, there was no column to write it to, and the Reader API answered
`"author": ""` for every item. The byline was captured on every fetch and
discarded on every fetch.

Not nullable, with an empty-string default rather than NULL. The empty string
is already the meaningful value here ("the origin named nobody"), it is what
the Reader API emits today for every item, and making it nullable would give
readers two spellings of one fact to handle. Existing rows take the default,
which is exactly what they were already reported as.

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-13

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("item") as batch:
        batch.add_column(
            sa.Column("author", sa.Text(), nullable=False, server_default="")
        )


def downgrade() -> None:
    with op.batch_alter_table("item") as batch:
        batch.drop_column("author")
