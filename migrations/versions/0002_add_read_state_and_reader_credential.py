"""add read_state and reader_credential

Adds the two tables the Reader API needs (ADR 0004 promotes C4 to P1):

- read_state: per-item consumption state written back by clients
- reader_credential: the Reader API login, as a scrypt password hash

Both are fact sources per docs/domain/data-model.md, so they belong in the
backup set alongside Item and AuditLog.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-03

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the Reader API tables."""
    op.create_table(
        "read_state",
        sa.Column("item_id", sa.String(length=64), nullable=False),
        sa.Column("is_read", sa.Boolean(), nullable=False),
        sa.Column("is_starred", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["item.id"],
            name=op.f("fk_read_state_item_id_item"),
            ondelete="CASCADE",
        ),
        # Composite key: one row per item per tenant. Making it the primary key
        # means an upsert is a real upsert, so two clients reporting state for
        # the same item cannot create duplicate rows.
        sa.PrimaryKeyConstraint("item_id", "tenant_id", name=op.f("pk_read_state")),
    )

    op.create_table(
        "reader_credential",
        sa.Column("username", sa.String(length=128), nullable=False),
        # scrypt hash, self-describing: "<n>$<r>$<p>$<salt>$<hash>". The
        # parameters travel with the hash so they can be raised later without
        # invalidating existing rows.
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("username", name=op.f("pk_reader_credential")),
    )


def downgrade() -> None:
    """Drop both tables.

    ReadState is a fact source, so this destroys real user state. It exists
    because migrations must be reversible, not because dropping these in
    production is a reasonable thing to do.
    """
    op.drop_table("reader_credential")
    op.drop_table("read_state")
