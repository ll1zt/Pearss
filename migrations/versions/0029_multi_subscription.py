"""one origin, several subscriptions: per-subscription readiness (ADR 0061)

`UNIQUE(tenant_id, origin_id)` was the 1:1 stage's rule, not a domain one. It
goes, so an origin can carry two policies -- a translated subscription and an
untranslated one, say -- and `item_subscription_ready` records what the old
item-level `publish_status` could not: *which* channel an item is ready for.

ADR 0061 decided the shape and rejected the alternatives (deriving visibility at
read time, a "primary subscription", a global gate). The short version: the
chain is subscription policy, the item is an origin fact, so readiness is a
property of the pair.

The backfill reproduces today's visible set exactly -- every item that was not
`pending` becomes ready for its origin's subscription -- so the migration does
not change what any client sees. `publish_status` keeps its own transitions
(content frozen / first publish) and stops being the visibility predicate.

The index is on `(subscription_id, item_id)` rather than leaning on the primary
key's order: every scoped read is "this subscription's ready items", and a PK
that leads with `item_id` cannot serve that as a seek.

Revision ID: 0029
Revises: 0028
Create Date: 2026-10-07

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0029"
down_revision: str | Sequence[str] | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_COLUMNS = (
    "id, tenant_id, origin_id, schedule_policy, pipeline_chain, target_lang,"
    " folder, status, trust_level, created_at, updated_at"
)

# Hand-written rather than `batch_alter_table`: SQLite has no ALTER for a UNIQUE
# constraint, so alembic rebuilds the table, and it renders the rebuild from
# SQLAlchemy's constraint set -- whose iteration order depends on the process's
# hash seed. Two interpreters that ran the same migrations then produce
# different DDL text for `subscription`, which the suite's reuse guard catches
# (a copied template must be byte-for-byte the migration). Writing the statement
# out makes the text a fact of this file. Same reason 0028 rebuilds by hand.
_CREATE_SUBSCRIPTION = (
    "CREATE TABLE subscription_new ("
    "id VARCHAR(64) NOT NULL, "
    "tenant_id VARCHAR(64) NOT NULL, "
    "origin_id VARCHAR(64) NOT NULL, "
    "schedule_policy TEXT NOT NULL, "
    "pipeline_chain TEXT, "
    "target_lang VARCHAR(16), "
    "folder TEXT DEFAULT 'default' NOT NULL, "
    "status VARCHAR(16) NOT NULL, "
    "trust_level VARCHAR(16) NOT NULL, "
    "created_at TEXT NOT NULL, "
    "updated_at TEXT NOT NULL, "
    "CONSTRAINT pk_subscription PRIMARY KEY (id), "
    "CONSTRAINT fk_subscription_origin_id_origin FOREIGN KEY(origin_id)"
    " REFERENCES origin (id) ON DELETE CASCADE, "
    "CONSTRAINT ck_subscription_sourcestatus CHECK (status IN"
    " ('active', 'paused', 'disabled')), "
    "CONSTRAINT ck_subscription_trustlevel CHECK (trust_level IN"
    " ('standard', 'trusted', 'restricted'))"
    ")"
)

# The 1:1 stage's rule, restored on downgrade.
_CREATE_SUBSCRIPTION_1TO1 = (
    _CREATE_SUBSCRIPTION[: -len(")")]
    + ", CONSTRAINT uq_subscription_origin UNIQUE (tenant_id, origin_id))"
)

_COPY_INTO_NEW = (
    f"INSERT INTO subscription_new ({_COLUMNS}) SELECT {_COLUMNS} FROM subscription"
)
_COPY_BACK = (
    f"INSERT INTO subscription ({_COLUMNS}) SELECT {_COLUMNS} FROM subscription_old"
)


def _rebuild_subscription(create: str) -> None:
    """Replace `subscription` with the shape `create` describes, keeping rows.

    The two renames are load-bearing together with the call order in
    `upgrade`/`downgrade`: SQLite rewrites a foreign key clause that points at a
    renamed table, so this must run before `item_subscription_ready` exists (or
    after it is dropped) -- otherwise its `REFERENCES subscription` would follow
    the old table to `subscription_old` and be left dangling when that is
    dropped.
    """
    op.execute(create)
    op.execute(_COPY_INTO_NEW)
    op.execute("ALTER TABLE subscription RENAME TO subscription_old")
    op.execute("ALTER TABLE subscription_new RENAME TO subscription")
    op.execute("DROP TABLE subscription_old")


def upgrade() -> None:
    """Drop the 1:1 rule and record per-subscription readiness."""
    _rebuild_subscription(_CREATE_SUBSCRIPTION)

    op.create_table(
        "item_subscription_ready",
        sa.Column("item_id", sa.String(length=64), nullable=False),
        sa.Column("subscription_id", sa.String(length=64), nullable=False),
        sa.Column("ready_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint(
            "item_id", "subscription_id", name="pk_item_subscription_ready"
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["item.id"],
            name=op.f("fk_item_subscription_ready_item_id_item"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["subscription.id"],
            name=op.f("fk_item_subscription_ready_subscription_id_subscription"),
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_item_subscription_ready_subscription_id",
        "item_subscription_ready",
        ["subscription_id", "item_id"],
    )

    # Today's visible set, reproduced row for row: not-pending means "the chain
    # (the only one there was) had completed".
    op.execute(
        "insert into item_subscription_ready (item_id, subscription_id, ready_at)"
        " select i.id, s.id, i.updated_at from item i"
        " join subscription s on s.origin_id = i.origin_id"
        " where i.publish_status != 'pending'"
    )


def downgrade() -> None:
    """Back to one subscription per origin.

    The unique constraint can only come back if no origin has two: a database
    that used the new freedom has no single-subscription form to return to, and
    the constraint failing is how that is said out loud rather than by dropping
    a subscription's policy.
    """
    op.drop_index(
        "ix_item_subscription_ready_subscription_id",
        table_name="item_subscription_ready",
    )
    op.drop_table("item_subscription_ready")
    _rebuild_subscription(_CREATE_SUBSCRIPTION_1TO1)
