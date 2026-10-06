"""item: index the page the reads now ask for (origin_id, sort_key, id)

Stage B of ADR 0046's split moved every source-scoped item read from
`item.source_id` to `item.origin_id`. That left migration 0022's
`ix_item_source_sort_key_id` serving a column nothing filters on, so a
source-scoped page fell back to `ix_item_origin_id` plus a temporary b-tree for
the ORDER BY -- the exact shape issue #77 removed, measured:

    SEARCH item USING INDEX ix_item_origin_id (origin_id=?) | USE TEMP B-TREE
FOR ORDER BY

So the index moves with the read. `ix_item_origin_id` (0026's own index on the
new column) goes too: with the composite in place it can only add write cost.
`ix_item_source_sort_key_id` stays until the migration that drops the column it
is on -- dropping it earlier would leave a page that still reads it, if such a
reader ever existed, with no index at all.

ANALYZE at the end, for 0022's reason: the planner chooses between these
indexes from statistics, and the index set has just changed.

Revision ID: 0027
Revises: 0026
Create Date: 2026-10-06

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0027"
down_revision: str | Sequence[str] | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Give the origin-scoped page the composite index its reads need."""
    op.create_index(
        "ix_item_origin_sort_key_id", "item", ["origin_id", "sort_key", "id"]
    )
    op.drop_index("ix_item_origin_id", table_name="item")
    op.execute("ANALYZE")


def downgrade() -> None:
    """Back to the single-column index stage A added."""
    op.create_index("ix_item_origin_id", "item", ["origin_id"])
    op.drop_index("ix_item_origin_sort_key_id", table_name="item")
