"""add per-source processing config (P3: chain + target language)

A source may require derived artifacts before its items open the gate.
`pipeline_chain` names the ordered specs (JSON list of
{plugin_id, kind, lang, required}); `target_lang` is the language products
default to. Both nullable: NULL means no chain, i.e. P1 behaviour where the
gate opens immediately.

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-09

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("source", sa.Column("pipeline_chain", sa.Text(), nullable=True))
    op.add_column("source", sa.Column("target_lang", sa.String(16), nullable=True))


def downgrade() -> None:
    op.drop_column("source", "target_lang")
    op.drop_column("source", "pipeline_chain")
