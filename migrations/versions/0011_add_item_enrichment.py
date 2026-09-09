"""add item_enrichment (P3: processor chain products, ADR 0018 to be)

Processors (translation, summary, TTS) produce derived artifacts that never
rewrite the original Item (docs/domain/data-model.md). This table is the
fact source for those artifacts: one row per (item, processor, kind, lang,
input version), with `input_hash` pinning which content version was
processed so a content change naturally invalidates the product.

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-09

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "item_enrichment",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "item_id",
            sa.String(64),
            sa.ForeignKey("item.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("plugin_id", sa.String(64), nullable=False),
        sa.Column("plugin_version", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("lang", sa.String(16), nullable=True),
        sa.Column("payload_ref", sa.Text(), nullable=True),
        sa.Column("input_hash", sa.String(128), nullable=False),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
        ),
        sa.Column(
            "error_class",
            sa.String(32),
            nullable=True,
        ),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name="enrichmentstatus",
        ),
        # Same input processed by the same processor version is the same
        # product: retrying replaces the row instead of accumulating.
        sa.UniqueConstraint(
            "item_id",
            "plugin_id",
            "plugin_version",
            "kind",
            "lang",
            "input_hash",
            name="uq_enrichment_product",
        ),
    )
    op.create_index(
        "ix_enrichment_item_status",
        "item_enrichment",
        ["item_id", "status"],
    )
    op.create_index(
        "ix_enrichment_status",
        "item_enrichment",
        ["status"],
        sqlite_where=sa.text("status = 'queued'"),
    )


def downgrade() -> None:
    op.drop_table("item_enrichment")
