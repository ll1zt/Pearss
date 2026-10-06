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


def upgrade() -> None:
    """Drop the 1:1 rule and record per-subscription readiness."""
    with op.batch_alter_table("subscription") as batch_op:
        batch_op.drop_constraint("uq_subscription_origin", type_="unique")

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
    with op.batch_alter_table("subscription") as batch_op:
        batch_op.create_unique_constraint(
            "uq_subscription_origin", ["tenant_id", "origin_id"]
        )
