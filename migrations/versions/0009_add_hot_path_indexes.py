"""add indexes on the hot lookup paths

Three queries run on every collection and every scheduler tick and had no
index to serve them, so each one scanned its whole table:

- `find_by_content_hash` scans `item` once per stored draft during dedupe.
- `last_conditional_state` scans `raw_fetch` for a source's most recent
  fetch on every collection, and `raw_fetch` only ever grows.
- `last_collect_at` / `consecutive_failures` / `list_job_runs` filter
  `job_run` by job on every tick for every source.

At a few thousand items this is invisible; it degrades linearly with library
size. The index names follow the metadata naming convention so the models and
this migration cannot disagree about the spelling.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-06

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEXES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("ix_item_content_hash", "item", ("content_hash",)),
    ("ix_raw_fetch_source_id_fetched_at", "raw_fetch", ("source_id", "fetched_at")),
    ("ix_job_run_job_id_started_at", "job_run", ("job_id", "started_at")),
    ("ix_job_run_job_id_finished_at", "job_run", ("job_id", "finished_at")),
)


def upgrade() -> None:
    for name, table, columns in _INDEXES:
        op.create_index(name, table, list(columns))


def downgrade() -> None:
    for name, table, _columns in reversed(_INDEXES):
        op.drop_index(name, table_name=table)
