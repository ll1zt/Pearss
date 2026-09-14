"""paging state: resume_cursor -> floor_cursor, drop resume_rounds

The interval model gave the stored cursor a new meaning. It used to be a
resume point: a walk continued *from* it and the `resume_rounds` counter aged
it out after three unresolved cycles. It is now the floor -- the oldest point
any walk reached, a stop sign reached from above and never a place to start
from. Renaming it is the point: `resume_cursor` invites exactly the reading
the model forbids.

`resume_rounds` has no successor. It existed to bound a backlog that slid
backwards every cycle; a floor does not slide, so there is nothing to age out.
No code reads or writes it (that happened in the same change), so dropping the
column loses nothing.

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-14

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("source") as batch:
        batch.alter_column("resume_cursor", new_column_name="floor_cursor")
        batch.drop_column("resume_rounds")


def downgrade() -> None:
    with op.batch_alter_table("source") as batch:
        batch.add_column(
            sa.Column("resume_rounds", sa.Integer(), nullable=False, server_default="0")
        )
        batch.alter_column("floor_cursor", new_column_name="resume_cursor")
