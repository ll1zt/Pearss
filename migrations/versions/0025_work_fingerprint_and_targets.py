"""work: the command's targets and its value fingerprint (ADR 0046)

    work.targets_json, work.fingerprint, uq_work_live_fingerprint

0024 created the ledger. This adds the two things that make a *command*
recognisable as one:

- **`targets_json`**: the command's input, a field of its own rather than a key
  inside `params_json` -- it is what the work is about, and `counts` derives
  `total` from it instead of from a caller-supplied number.
- **`fingerprint` + a partial unique index over the live statuses**: one *in
  flight* work per command value. Partial on purpose -- once a work is
  terminal, submitting the same command again is a new request (a caller may
  genuinely want a fresh run of the same batch), and a plain unique constraint
  would forbid that forever. The shape mirrors `uq_job_run_active_per_job`
  (0008), which arbitrates the same kind of race for collection runs.

`fingerprint` is nullable because a work created before this migration has
none, and SQL treats NULLs as distinct -- so such rows cannot collide in the
index.

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-01

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0025"
down_revision: str | Sequence[str] | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LIVE = "status IN ('queued', 'running')"


def upgrade() -> None:
    """Add the command's input and its digest, and the live-work index."""
    op.add_column(
        "work",
        sa.Column("targets_json", sa.Text(), nullable=False, server_default="[]"),
    )
    op.add_column("work", sa.Column("fingerprint", sa.String(length=64), nullable=True))
    op.create_index(
        "uq_work_live_fingerprint",
        "work",
        ["fingerprint"],
        unique=True,
        sqlite_where=sa.text(_LIVE),
        postgresql_where=sa.text(_LIVE),
    )


def downgrade() -> None:
    """Reverse the index and the two columns."""
    op.drop_index("uq_work_live_fingerprint", table_name="work")
    op.drop_column("work", "fingerprint")
    op.drop_column("work", "targets_json")
