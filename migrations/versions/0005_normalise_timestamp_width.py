"""normalise stored timestamps to fixed width

The application now writes RFC 3339 UTC with exactly six fractional digits.
Rows written before that change have no fractional part at all, and a column
mixing the two forms sorts wrongly: '.' (0x2E) is less than 'Z' (0x5A), so
`...T05:00:00.000000Z` orders *before* `...T05:00:00Z` despite being the same
instant, and any ordering that compares them as text is wrong.

Leaving the column mixed would reintroduce exactly the defect the format change
was made to fix, so existing values are widened rather than left alone.

The rewrite is idempotent: rows that already carry a '.' are skipped, so
re-running it changes nothing.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-04

"""

import re
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Every text column holding an instant, as (table, column).
TIMESTAMP_COLUMNS: tuple[tuple[str, str], ...] = (
    ("source", "created_at"),
    ("source", "updated_at"),
    ("raw_fetch", "fetched_at"),
    ("item", "published_at"),
    ("item", "published_to_feed_at"),
    ("item", "created_at"),
    ("item", "updated_at"),
    ("job_run", "started_at"),
    ("job_run", "finished_at"),
    ("read_state", "updated_at"),
    ("audit_log", "occurred_at"),
    ("idempotency_record", "created_at"),
    ("idempotency_record", "expires_at"),
)


# SQL identifiers cannot be bound as parameters, so they are interpolated.
# The names come from TIMESTAMP_COLUMNS above rather than from any caller, but
# checked anyway: a typo there would otherwise interpolate into the statement
# unvalidated, and this is the one place in the codebase that builds SQL from
# strings.
_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")


def _widen(table: str, column: str) -> None:
    """Append `.000000` to a `...Z` timestamp that has no fractional part."""
    for name in (table, column):
        if not _IDENTIFIER.fullmatch(name):
            raise ValueError(f"refusing to widen an unexpected identifier: {name!r}")
    op.execute(
        f"""
        UPDATE "{table}"
           SET "{column}" = substr("{column}", 1, length("{column}") - 1)
                                 || '.000000Z'
         WHERE "{column}" IS NOT NULL
           AND "{column}" LIKE '%Z'
           AND "{column}" NOT LIKE '%.%'
        """
    )


def upgrade() -> None:
    """Rewrite existing timestamps in place."""
    for table, column in TIMESTAMP_COLUMNS:
        _widen(table, column)


def downgrade() -> None:
    """Leave the widened values in place.

    Reverting the format would put the column back into the mixed state this
    migration exists to remove, and a wider timestamp is still correct — so
    downgrading restores the schema without re-narrowing the data.
    """
