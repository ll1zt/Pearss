"""The feed window: what the RSS output renders, and gate promotion."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, delete, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from adapters.db_models import Item as ItemRow
from adapters.db_models import ItemSubscriptionReady as ReadyRow
from adapters.db_models import Origin as OriginRow
from adapters.db_models import Subscription as SubscriptionRow
from adapters.repository._shared import (
    row_to_item,
    visible_item_rows,
    visible_item_rows_for,
)
from adapters.rss_out import FeedMeta
from adapters.timestamps import fixed_width_utc as timestamp
from core.enums import (
    ItemEvent,
    PublishStatus,
    SourceStatus,
)
from core.item_state import TransitionContext
from core.item_state import transition as item_transition
from core.models import Item
from core.normalize import origin_display_label

FEED_WINDOW: int = 200


def channel_for_subscription(
    session: Session, *, subscription_id: str, tenant_id: str
) -> FeedMeta | None:
    """The channel metadata a subscription's RSS carries, or None if unknown.

    The channel is the **subscription** (ADR 0046 D-C): what a client
    subscribes to is the policy, so the channel's identity is its id while the
    name and link describe the origin behind it -- the same display label the
    Reader's sidebar uses, so the two outputs cannot disagree about what a
    channel is called.
    """
    row = session.execute(
        select(OriginRow, SubscriptionRow)
        .join(SubscriptionRow, SubscriptionRow.origin_id == OriginRow.id)
        .where(
            SubscriptionRow.id == subscription_id,
            SubscriptionRow.tenant_id == tenant_id,
            # Unsubscribed means the feed stops being published (ADR 0063);
            # the items stay, and re-subscribing brings the same feed back.
            SubscriptionRow.status != SourceStatus.DISABLED.value,
        )
    ).first()
    if row is None:
        return None
    origin, _subscription = row
    link = origin.canonical_url or origin.url or ""
    return FeedMeta(
        title=origin_display_label(
            title=origin.title,
            url=origin.url,
            canonical_url=origin.canonical_url,
            origin_id=origin.id,
        ),
        link=link,
    )


def mark_subscription_ready(
    session: Session, *, item_id: str, subscription_id: str, at: str
) -> bool:
    """Record that this subscription's chain has completed for this item.

    ADR 0061: the pair is the readiness identity, so the write is idempotent by
    construction -- an `ON CONFLICT DO NOTHING` inside the statement rather than
    a read-then-write, which loses exactly the race two workers finishing the
    same item would produce. Returns whether a row was written, so a caller can
    tell "first time" from "already".
    """
    result = cast(
        "CursorResult[Any]",
        session.execute(
            sqlite_insert(ReadyRow)
            .values(item_id=item_id, subscription_id=subscription_id, ready_at=at)
            .on_conflict_do_nothing(index_elements=["item_id", "subscription_id"])
        ),
    )
    return bool(result.rowcount)


def mark_ready_for_chainless_subscriptions(
    session: Session, *, item: Item, at: str
) -> int:
    """Open the channel for every subscription whose chain has nothing to do.

    A subscription with no chain has nothing to wait for, so its channel opens
    the moment the item is stored. That is the case the item-level promotion
    used to cover; per ADR 0061 it is one ready row per chainless subscription.
    """
    from adapters.repository.enrichment import chain_for_source
    from adapters.repository.subscriptions import subscriptions_for_origin

    marked = 0
    for subscription in subscriptions_for_origin(
        session, origin_id=item.origin_id, tenant_id=item.tenant_id
    ):
        if chain_for_source(session, source_id=subscription.id):
            continue
        marked += mark_subscription_ready(
            session,
            item_id=item.id,
            subscription_id=subscription.id,
            at=at,
        )
    return marked


def close_gate(session: Session, *, item_id: str) -> int:
    """Make an item invisible again (ADR 0061).

    A revised body invalidates whatever the channels were showing, so every
    ready row for the item goes. Re-opening is the pipeline's job and it is
    per channel: a subscription with work to do enqueues it, a chainless one is
    put straight back. That is the old `publish_status = pending` contract
    translated, and it is deliberately unconditional -- the caller's own chain
    argument is authoritative for a single channel, so the store cannot know
    from the rows alone which channels will need to re-run.
    """
    result = session.execute(delete(ReadyRow).where(ReadyRow.item_id == item_id))
    return cast("CursorResult[Any]", result).rowcount


def items_ready_for_feed(
    session: Session, *, subscription_id: str | None = None, limit: int = FEED_WINDOW
) -> list[Item]:
    """Items the gate permits, newest first, within the feed window.

    Pending is excluded in SQL rather than in Python: the gate is a hard
    invariant, and letting a pending row reach memory at all means a future
    change could accidentally render it.

    `subscription_id` scopes the window to one channel. Unscoped, the window is
    every item ready somewhere -- which is what a single combined feed wants, and
    never what one channel should render (ADR 0061: readiness is per channel).
    """
    query = select(ItemRow)
    if subscription_id is None:
        query = query.where(visible_item_rows())
    else:
        # Scope by the origin *as well* as the channel: the channel's items are
        # its origin's, and the origin filter is what lets the planner seek
        # `ix_item_origin_sort_key_id` and walk it in order instead of driving
        # from the ready table and sorting into a temp b-tree (issue #77).
        origin_id = session.execute(
            select(SubscriptionRow.origin_id).where(
                SubscriptionRow.id == subscription_id
            )
        ).scalar_one_or_none()
        if origin_id is None:
            return []
        query = query.where(
            ItemRow.origin_id == origin_id,
            visible_item_rows_for(subscription_id),
        )
    rows = session.execute(
        query.order_by(ItemRow.published_at.desc().nullslast(), ItemRow.guid).limit(
            max(1, limit)
        )
    ).scalars()
    return [row_to_item(row) for row in rows]


def mark_published(session: Session, items: list[Item], *, at: datetime) -> int:
    """Record that items have appeared in a feed, freezing their content.

    Returns how many rows were actually flipped. Publishing is a one-way
    transition, and the conditional update means two concurrent feed renders
    cannot both claim credit for the same item.
    """
    if not items:
        return 0
    # Validate the transition through the state machine, as promote_to_ready
    # and finish_job_run do. The bulk update below is the efficient form of
    # `transition(READY, FIRST_PUBLISH)` per row, and its WHERE clause repeats
    # the machine's guard so a row that moved on in the meantime is skipped
    # rather than overwritten — but the machine is what makes an illegal
    # promotion a loud error instead of a silent schema change.
    item_transition(PublishStatus.READY, ItemEvent.FIRST_PUBLISH)
    ids = [item.id for item in items]
    stamp = timestamp(at)
    # session.execute is typed as returning Result[Any], but a DML statement
    # yields a CursorResult at runtime, which is where rowcount lives. The cast
    # is narrowed to this one statement rather than disabling the check.
    result = cast(
        "CursorResult[Any]",
        session.execute(
            update(ItemRow)
            .where(
                ItemRow.id.in_(ids),
                ItemRow.publish_status == PublishStatus.READY.value,
                ItemRow.published_to_feed_at.is_(None),
            )
            .values(
                publish_status=PublishStatus.PUBLISHED.value,
                published_to_feed_at=stamp,
                updated_at=stamp,
            )
        ),
    )
    return int(result.rowcount or 0)


def promote_to_ready(session: Session, items: list[Item]) -> int:
    """Advance pending items to ready, using the state machine.

    Goes through core's transition rather than assigning the value, so an
    illegal promotion (a published item, or one whose processing is genuinely
    incomplete) is refused instead of silently applied.

    ADR 0061: the item-level transition says the *content* is final; what makes
    the item visible is a ready row per subscription, so this also opens every
    channel whose chain has nothing to wait for. Both writes happen here because
    "promote" means both, and splitting them is how a channel would end up
    ready-by-status and invisible-by-gate.

    Returns how many items advanced.
    """
    advanced = 0
    for item in items:
        if item.publish_status is not PublishStatus.PENDING:
            continue
        result = item_transition(
            item.publish_status,
            ItemEvent.MARK_READY,
            TransitionContext(processing_complete=True),
        )
        if result.status is not PublishStatus.READY:
            continue
        session.execute(
            update(ItemRow)
            .where(
                ItemRow.id == item.id,
                ItemRow.publish_status == PublishStatus.PENDING.value,
            )
            .values(publish_status=PublishStatus.READY.value)
        )
        mark_ready_for_chainless_subscriptions(
            session, item=item, at=timestamp(datetime.now(UTC))
        )
        advanced += 1
    return advanced
