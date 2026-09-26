"""chain dataflow: a product may be another product's input (ADR 0030)

A chain is a pipeline, but every spec read the Item's own stored body, so
`[fulltext, translate]` translated the feed's 50-character summary while the
extracted full text sat beside it -- three layers in the reader that did not
correspond to each other (issue #34).

A row now records which earlier product fed its input:

- `upstream_plugin_id`  the plugin whose product was this spec's input
- `upstream_digest`     that product's identity (sha256 over its bytes)

Both NULL is the pre-existing behavior and stays meaningful: the spec read the
Item's own content, which is exactly what every `[translate]`-only chain still
does. NULL is a first-class state here, not a missing value.

A pure ADD COLUMN on purpose. The unique constraint is deliberately NOT
widened: `input_hash` identifies the Item content version a product belongs
to, and the table's contract (see db_models.ItemEnrichment) is that
re-processing the same content version *replaces* the row rather than
accumulating copies. An upstream revision is therefore a change to an existing
row's recorded dependency, not a new product identity -- so the schema needs
no table rebuild, the upgrade is idempotent in effect, and the downgrade is
exactly reversible with no data loss.

Revision ID: 0021
Revises: 0020
Create Date: 2026-09-26

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "item_enrichment",
        sa.Column("upstream_plugin_id", sa.String(64), nullable=True),
    )
    op.add_column(
        "item_enrichment",
        sa.Column("upstream_digest", sa.String(64), nullable=True),
    )
    # Resolving "which product does this spec consume" runs once per claimed
    # row; the lookup is by (item, upstream_plugin) within one content version.
    op.create_index(
        "ix_item_enrichment_upstream",
        "item_enrichment",
        ["item_id", "upstream_plugin_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_item_enrichment_upstream", table_name="item_enrichment")
    op.drop_column("item_enrichment", "upstream_digest")
    op.drop_column("item_enrichment", "upstream_plugin_id")
