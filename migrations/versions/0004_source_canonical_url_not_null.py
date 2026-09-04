"""make source.canonical_url NOT NULL

The uniqueness constraint is `uq_source_url (tenant_id, canonical_url, type)`.
In SQL, NULLs are distinct from one another, so any number of rows with a NULL
canonical_url could share the same URL and type: the constraint that is
supposed to prevent duplicate sources simply did not apply to them.

canonical_url is the dedupe key, so a row without one is not a source with a
missing field — it is a source the constraint cannot see.

Backfill rather than delete. Rows that cannot be resolved get a
`legacy://<id>` synthetic key, which keeps them inside the constraint and
keeps their published items. Deleting here would cascade to item and
read_state, and a migration that destroys published content cannot be re-run
safely anywhere.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-04

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Backfill canonical_url, then require it."""
    # Rows with a url but no canonical_url: copy it across. This is the raw
    # url rather than a canonicalised one, because canonicalisation strips
    # tracking parameters and sorts query strings — application logic that a
    # migration must not reimplement, since a divergence between the two would
    # silently split a source into duplicates. Such rows are corrected the
    # next time the application writes them.
    op.execute(
        """
        UPDATE source
           SET canonical_url = url
         WHERE canonical_url IS NULL
           AND url IS NOT NULL
        """
    )

    # Rows with neither: no URL exists to derive from, so give them a key that
    # is unique per row and visibly synthetic. The alternative — deleting them
    # — would cascade to their published items and their read states.
    op.execute(
        """
        UPDATE source
           SET canonical_url = 'legacy://' || id
         WHERE canonical_url IS NULL
        """
    )

    with op.batch_alter_table("source") as batch:
        batch.alter_column(
            "canonical_url",
            existing_type=sa.Text(),
            nullable=False,
        )


def downgrade() -> None:
    """Allow NULL again.

    The backfilled values are left in place: reverting a constraint should not
    rewrite data, and `legacy://` keys are still better than NULLs because
    they keep rows inside the uniqueness constraint.
    """
    with op.batch_alter_table("source") as batch:
        batch.alter_column(
            "canonical_url",
            existing_type=sa.Text(),
            nullable=True,
        )
