"""item: a generated sort key, so the feed can be a seek instead of a scan

Every feed read is the same shape: gate-visible rows, newest first, paged. The
planner never had an index for it. `ix_item_status_published` cannot serve it
(the gate is a negation, `publish_status != 'pending'`, and the ordering is not
on that column), and the ordering key is `coalesce(published_at, EPOCH_FLOOR)`
rather than a column -- so every page was a full table scan plus a temp B-tree
(measured: 1.0ms for the first page over 3,000 items; issue #77).

`sort_key` is that expression as a VIRTUAL generated column, and the two new
indexes are on it. A generated column rather than an index on the expression
itself, because an expression index is only *matched* while the query renders
the same expression text: measured, the expression index served the page in
0.13ms and the same query with the floor rendered as a bound parameter fell back
to the scan-plus-temp-B-tree (3.0ms). A silent fallback is the failure mode this
project keeps designing against, and indexing a column name cannot have it.

VIRTUAL on purpose: the value is derivable from `published_at`, so the column
takes no space in the row; the index materialises it, which is the only place it
is needed. `published_at` stays nullable -- "this item has no date" is a fact,
and the floor exists only to give those rows a stable position in the feed.

ANALYZE at the end: the planner decides between these indexes and a scan from
statistics, and a table that has just grown a column has none for it.

Revision ID: 0022
Revises: 0021
Create Date: 2026-09-28

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Inlined rather than imported from adapters.timestamps: a migration is a
# historical record, and its meaning must not change if that constant ever
# changes (AGENTS.md: migrations inline their enum values for the same reason).
EPOCH_FLOOR = "0000-01-01T00:00:00.000000Z"


def upgrade() -> None:
    op.add_column(
        "item",
        sa.Column(
            "sort_key",
            sa.Text(),
            sa.Computed(f"coalesce(published_at, '{EPOCH_FLOOR}')", persisted=False),
            nullable=False,
        ),
    )
    # Ascending on purpose: SQLite scans an index in reverse, so one index
    # serves `sort_key DESC, id DESC` as well as the ascending order -- and a
    # mixed-direction order would need the direction spelled out, which no
    # caller asks for.
    op.create_index("ix_item_sort_key_id", "item", ["sort_key", "id"])
    op.create_index(
        "ix_item_source_sort_key_id", "item", ["source_id", "sort_key", "id"]
    )
    op.execute("ANALYZE")


def downgrade() -> None:
    op.drop_index("ix_item_source_sort_key_id", table_name="item")
    op.drop_index("ix_item_sort_key_id", table_name="item")
    op.execute("ALTER TABLE item DROP COLUMN sort_key")
