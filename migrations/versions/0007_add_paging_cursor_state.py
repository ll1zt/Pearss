"""add paging cursor state to source

An authenticated timeline source needs to remember where its last walk ended:

- `seen_frontier`: the newest tweet id already ingested. Paging stops there
  instead of spending every cycle's budget re-reading the newest page.
- `resume_cursor`: unfinished work -- either a detected gap (content that
  appeared in no page) or the deepest point a budget ran out at.
- `resume_rounds`: how many consecutive cycles that resume has gone unresolved,
  which bounds how far a backlog can slide into the past.

Only the first is derivable from `item`. A cursor is an opaque string from the
origin and a gap position is not recoverable from stored rows, so these cannot
be recomputed after a restore -- which is why they live beside the source rather
than in a rebuildable table.

Nullable with defaults: no existing collector uses them, and RSS has no cursor
concept at all.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-05

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("source") as batch:
        batch.add_column(sa.Column("seen_frontier", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("resume_cursor", sa.Text(), nullable=True))
        batch.add_column(
            sa.Column("resume_rounds", sa.Integer(), nullable=False, server_default="0")
        )


def downgrade() -> None:
    with op.batch_alter_table("source") as batch:
        batch.drop_column("resume_rounds")
        batch.drop_column("resume_cursor")
        batch.drop_column("seen_frontier")
