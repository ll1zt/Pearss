"""add source.title

NNW showed each subscription as the source's hex id because nothing in the
schema could hold a name: `source` has no title column, and the feed's own
<title>, which the collectors do extract into ItemDraft.metadata, is dropped by
normalize() -- correctly, since a channel name is a property of the source, not
of one item.

Nullable, and deliberately **not backfilled**. The value is discovered from the
origin at collection time, so storing a URL-derived guess here would occupy the
column and block the real title from ever being recorded: the writer sets it
only when absent, on the assumption that an operator's rename or an origin's own
name outranks anything we inferred. The readable label for a not-yet-collected
source is derived at display time instead, in core.source_display_label().

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-05

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("source") as batch:
        batch.add_column(sa.Column("title", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("source") as batch:
        batch.drop_column("title")
