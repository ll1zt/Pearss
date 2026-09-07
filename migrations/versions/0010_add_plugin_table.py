"""add the plugin table (P2: manifest + lifecycle state)

The registry verifies every builtin collector's manifest against its
hookimpls at startup and records the result here. This table is the
lifecycle's fact source: `status` moves enabled -> disabled by API (ADR
0017), and the checksum pins the manifest the operator enabled.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-06

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "plugin",
        sa.Column("id", sa.String(64), primary_key=True),
        # The manifest, serialised as canonical JSON (checksum computed over
        # the same form). The checksum column pins what was verified.
        sa.Column("manifest_json", sa.Text(), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
        ),
        sa.Column("trust", sa.String(16), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "status IN ('enabled', 'disabled')",
            name="pluginstatus",
        ),
        sa.CheckConstraint(
            "trust IN ('builtin', 'third-party')",
            name="plugintrust",
        ),
    )


def downgrade() -> None:
    op.drop_table("plugin")
