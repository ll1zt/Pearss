"""work / work_unit: a requested batch and its per-target ledger (ADR 0046)

    work, work_unit

The shape this solves: an action whose *output* is proportional to its input is
not an action but a job. `POST /v1/sources/{id}:fetch-items` answered 568
addresses with 568 per-address results inside one HTTP response, so a client
timeout lost the whole report while the server kept working. A job needs a
handle to hold and a ledger to read; this adds both.

Two decisions are visible in the table shapes:

- **The key is `(work_id, target)`.** One row per target per work, so replaying
  a target cannot add a second row -- and the database is what refuses it. A
  caller-side SELECT-then-INSERT loses exactly the race the constraint exists
  to prevent.
- **No counter column.** The statistics are folded from the rows
  (`core.work.work_counts`); a stored count would be a second source of truth
  for the same fact and would drift the first time one writer forgot the other.
  A test in `tests/integration/test_work_ledger.py` fails if one ever appears.

`work` is deliberately not `job` (0001): a job is *scheduled collection* for one
source, a work is a caller's request and has no schedule. Merging them would
mean a request for "fetch these 50 addresses" owning a cron expression.

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-01

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0024"
down_revision: str | Sequence[str] | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Inlined rather than imported from core.enums: a migration is a historical
# record and must produce the same schema years from now even if a taxonomy has
# gained members since.
_WORK_STATUS = ("queued", "running", "succeeded", "partial", "failed", "cancelled")
_UNIT_OUTCOME = ("ok", "skipped", "rejected", "failed")
_ERROR_CLASS = ("network", "parse", "structure_changed", "blocked", "auth")


def upgrade() -> None:
    """Create the work ledger."""
    op.create_table(
        "work",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        # Canonical JSON of the command's parameters; never raw bodies.
        sa.Column("params_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
        sa.Column("retry_of", sa.String(length=64), nullable=True),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("finished_at", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["retry_of"],
            ["work.id"],
            name=op.f("fk_work_retry_of_work"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_work")),
        sa.CheckConstraint(
            f"status IN {_WORK_STATUS}", name=op.f("ck_work_workstatus")
        ),
    )

    op.create_table(
        "work_unit",
        sa.Column("work_id", sa.String(length=64), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("error_class", sa.String(length=32), nullable=True),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("ref", sa.Text(), nullable=True),
        sa.Column("at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["work_id"],
            ["work.id"],
            name=op.f("fk_work_unit_work_id_work"),
            ondelete="CASCADE",
        ),
        # The point of the table: one row per target per work.
        sa.PrimaryKeyConstraint("work_id", "target", name=op.f("pk_work_unit")),
        sa.CheckConstraint(
            f"outcome IN {_UNIT_OUTCOME}", name=op.f("ck_work_unit_unitoutcome")
        ),
        sa.CheckConstraint(
            f"error_class IN {_ERROR_CLASS}",
            name=op.f("ck_work_unit_errorclass"),
        ),
    )
    # Two reads: "what is still queued for this work" and "what failed".
    op.create_index("ix_work_unit_work_outcome", "work_unit", ["work_id", "outcome"])


def downgrade() -> None:
    """Drop the work ledger.

    Reverse order so the foreign key never dangles, even on an engine that
    enforces it.
    """
    op.drop_index("ix_work_unit_work_outcome", table_name="work_unit")
    op.drop_table("work_unit")
    op.drop_table("work")
