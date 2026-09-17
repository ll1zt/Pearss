"""Reserve idempotent writes and bind them to requests.

Revision ID: 0020
Revises: 0019

Legacy responses retain NULL fingerprint/owner; no response data is rewritten.
"""

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "idempotency_record", sa.Column("fingerprint", sa.String(64), nullable=True)
    )
    op.add_column(
        "idempotency_record", sa.Column("owner_token", sa.String(64), nullable=True)
    )


def downgrade() -> None:
    # Incomplete reservations are not responses usable by older code.
    op.execute("DELETE FROM idempotency_record WHERE status_code = 0")
    op.drop_column("idempotency_record", "owner_token")
    op.drop_column("idempotency_record", "fingerprint")
