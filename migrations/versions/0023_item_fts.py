"""item_fts: the full-text index (ADR 0035 decisions 3 and 4)

Search matched the title with `LIKE '%q%'` and nothing else, so an article
whose body says 内核 was unfindable unless its title did too. This adds the FTS5
table the retrieval work builds on, fed by `core.text_search.tokenize()` --
the same function the query side uses, because an index built from one
segmentation and searched with another matches nothing and raises nothing.

Two decisions from the ADR are visible in the shape:

- **Not contentless.** `contentless_delete=1` would be needed for the delete
  path (ADR 0021 makes deletion a real operation), and contentless stores no
  column values, so every result would need a JOIN through a rowid mapping
  table. Measured cost of the simple form: 8.8 MB for 7,000 articles against
  2.5 MB -- and 6.3 MB is not worth two tables to delete in lockstep. This does
  not duplicate the corpus either: the bodies live in the CAS filestore
  (`content_ref`), not in the index, and never did.
- **`item_id UNINDEXED`.** The obvious `rowid = item.id` is impossible -- FTS5's
  rowid must be an INTEGER and item ids are 32-hex strings (measured:
  `IntegrityError: datatype mismatch`). So the id travels as an unindexed column
  and is what results join back on.

The table holds **every** item, `pending` included; visibility is filtered at
query time by joining back to `item` through the existing gate predicate. That
keeps the gate at one definition. Indexing only published items would create a
second one, and would need a row written the moment an item is promoted.

No backfill here on purpose: filling it means reading a body per item out of the
filestore, and a migration has to stay fast and reversible. `just reindex` does
that, and the scheduler tops the index up incrementally.

Revision ID: 0023
Revises: 0022
Create Date: 2026-09-28

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # `unicode61` is what splits the pre-tokenised string on the spaces
    # `tokenize()` left behind; the CJK segmentation is already done.
    op.execute(
        "CREATE VIRTUAL TABLE item_fts USING fts5("
        "item_id UNINDEXED, title_tok, body_tok, tokenize='unicode61')"
    )


def downgrade() -> None:
    op.execute("DROP TABLE item_fts")
