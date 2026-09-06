"""allow only one active run per job

`collect_now` opens a run, claims it, collects, and closes it. Two concurrent
callers -- a manual trigger and a scheduler tick, or two processes against one
database -- must not both collect the same source: the origin receives double
requests, and the run history records two attempts for one decision.

The application-level claim (a conditional update on the run's own row) cannot
provide this: each caller creates its own run id, so both claims succeed. The
exclusion has to live in the schema. A unique index over `job_id` restricted to
the active statuses makes the second concurrent insert fail outright, which the
caller reports as "already running" and stands down.

The predicate is inline rather than imported: a migration is a historical
record and must not follow the enum as it evolves.

Upgrade precondition: at most one queued/running run per job. The pre-index
race could in principle have produced two; the index creation then fails loudly
instead of silently rewriting run history. On any healthy database there is at
most one, because a run only exists while its process lives.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-06

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PREDICATE = "status IN ('queued', 'running')"


def upgrade() -> None:
    op.create_index(
        "uq_job_run_active_per_job",
        "job_run",
        ["job_id"],
        unique=True,
        sqlite_where=sa.text(_PREDICATE),
        postgresql_where=sa.text(_PREDICATE),
    )


def downgrade() -> None:
    op.drop_index("uq_job_run_active_per_job", table_name="job_run")
