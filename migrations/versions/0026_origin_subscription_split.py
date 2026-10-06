"""origin/subscription split, stage A: two tables and two columns, no reads

ADR 0046 D-A/D-B. `source` carries facts (type/url/canonical_url/cursors) and
policy (schedule_policy/pipeline_chain/target_lang/folder/status/trust_level)
in one row. This stage creates the two tables the rest of the refactor reads
from and copies every existing row into them. It changes no read path, which is
what lets it merge and roll back on its own.

The 1:1 stage is deliberate: `origin.id` and `subscription.id` are the old
`source.id`, so every existing item id and guid -- and every `/v1/sources/{id}`
URL a client already holds -- keeps meaning what it meant. Migration 0027 is
what drops `source`; the multi-subscription migration is what removes the
one-subscription rule below.

`item.origin_id` and `raw_fetch.origin_id` arrive nullable and are backfilled
from `source_id` here; 0027 makes them NOT NULL and adds the foreign key. They
are deliberately not read yet -- stage A is copy-only, so a rollback loses
nothing that a rollback of the *next* stage would not.

Revision ID: 0026
Revises: 0025
Create Date: 2026-10-06

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0026"
down_revision: str | Sequence[str] | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Inlined rather than imported from core.enums / core.tenancy: a migration is a
# historical record and must produce the same schema years from now even if a
# taxonomy or the tenant constant has changed since (AGENTS.md).
_SOURCE_TYPE = ("rss", "web", "agent")
_SOURCE_STATUS = ("active", "paused", "disabled")
_TRUST_LEVEL = ("standard", "trusted", "restricted")
_DEFAULT_FOLDER = "default"

# The copy is expressed column by column, in a fixed order, rather than as
# `select *`: an INSERT ... SELECT that relies on positional column order would
# silently swap two columns the first time either table gained one.
_ORIGIN_FROM_SOURCE = (
    "insert into origin (id, tenant_id, type, url, canonical_url, title,"
    " parser_plugin_id, seen_frontier, floor_cursor, created_at, updated_at)"
    " select id, tenant_id, type, url, canonical_url, title, parser_plugin_id,"
    " seen_frontier, floor_cursor, created_at, updated_at from source"
)
_SUBSCRIPTION_FROM_SOURCE = (
    "insert into subscription (id, tenant_id, origin_id, schedule_policy,"
    " pipeline_chain, target_lang, folder, status, trust_level, created_at,"
    " updated_at)"
    " select id, tenant_id, id, schedule_policy, pipeline_chain, target_lang,"
    " folder, status, trust_level, created_at, updated_at from source"
)


def upgrade() -> None:
    """Create origin/subscription and copy `source` into them."""
    op.create_table(
        "origin",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        # NOT NULL because it is the dedupe key: a nullable column in a unique
        # constraint is not covered by it (SQL treats NULLs as distinct).
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("parser_plugin_id", sa.String(length=128), nullable=True),
        sa.Column("seen_frontier", sa.BigInteger(), nullable=True),
        sa.Column("floor_cursor", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_origin")),
        sa.CheckConstraint(
            f"type IN {_SOURCE_TYPE}", name=op.f("ck_origin_sourcetype")
        ),
        sa.UniqueConstraint(
            "tenant_id", "canonical_url", "type", name=op.f("uq_origin_url")
        ),
    )
    op.create_table(
        "subscription",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("origin_id", sa.String(length=64), nullable=False),
        sa.Column("schedule_policy", sa.Text(), nullable=False),
        sa.Column("pipeline_chain", sa.Text(), nullable=True),
        sa.Column("target_lang", sa.String(length=16), nullable=True),
        sa.Column("folder", sa.Text(), nullable=False, server_default=_DEFAULT_FOLDER),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("trust_level", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_subscription")),
        sa.ForeignKeyConstraint(
            ["origin_id"],
            ["origin.id"],
            name=op.f("fk_subscription_origin_id_origin"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            f"status IN {_SOURCE_STATUS}", name=op.f("ck_subscription_sourcestatus")
        ),
        sa.CheckConstraint(
            f"trust_level IN {_TRUST_LEVEL}",
            name=op.f("ck_subscription_trustlevel"),
        ),
        # Stage A only: one subscription per origin. The multi-subscription
        # migration drops it once a second subscription means something.
        sa.UniqueConstraint(
            "tenant_id", "origin_id", name=op.f("uq_subscription_origin")
        ),
    )

    # Copy before the new columns exist, so a failure leaves the old tables
    # exactly as they were rather than half-migrated.
    op.execute(_ORIGIN_FROM_SOURCE)
    op.execute(_SUBSCRIPTION_FROM_SOURCE)

    op.add_column("item", sa.Column("origin_id", sa.String(length=64), nullable=True))
    op.add_column(
        "raw_fetch", sa.Column("origin_id", sa.String(length=64), nullable=True)
    )
    op.execute("update item set origin_id = source_id")
    op.execute("update raw_fetch set origin_id = source_id")
    op.create_index("ix_item_origin_id", "item", ["origin_id"])
    op.create_index("ix_raw_fetch_origin_id", "raw_fetch", ["origin_id"])


def downgrade() -> None:
    """Drop the two columns and the two tables; `source` was never touched."""
    op.drop_index("ix_raw_fetch_origin_id", table_name="raw_fetch")
    op.drop_index("ix_item_origin_id", table_name="item")
    # Raw ALTER TABLE, not `batch_alter_table`: batch recreates the table from
    # reflected DDL, and SQLite's reflection of `item.sort_key` (0001's VIRTUAL
    # generated column) comes back attached to the *next* column -- the batch
    # then renders `origin_id VARCHAR(64) VIRTUAL` and the CREATE fails. 0022
    # drops its generated column the same way, and DROP COLUMN has been
    # supported since SQLite 3.35.
    op.execute("ALTER TABLE raw_fetch DROP COLUMN origin_id")
    op.execute("ALTER TABLE item DROP COLUMN origin_id")
    op.drop_table("subscription")
    op.drop_table("origin")
