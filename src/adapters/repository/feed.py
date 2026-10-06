"""The feed window: what the RSS output renders, and gate promotion."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.orm import Session

from adapters.db_models import Item as ItemRow
from adapters.db_models import Origin as OriginRow
from adapters.db_models import Subscription as SubscriptionRow
from adapters.repository._shared import row_to_item, visible_item_rows
from adapters.rss_out import FeedMeta
from adapters.timestamps import fixed_width_utc as timestamp
from core.enums import (
    ItemEvent,
    PublishStatus,
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


def items_ready_for_feed(
    session: Session, *, origin_id: str | None = None, limit: int = FEED_WINDOW
) -> list[Item]:
    """Items the gate permits, newest first, within the feed window.

    Pending is excluded in SQL rather than in Python: the gate is a hard
    invariant, and letting a pending row reach memory at all means a future
    change could accidentally render it.

    `origin_id` scopes the window to one channel's origin. Unscoped, the window
    is every origin's items -- which is what a single combined feed wants, and
    never what one subscription's channel should render.
    """
    query = select(ItemRow).where(visible_item_rows())
    if origin_id is not None:
        query = query.where(ItemRow.origin_id == origin_id)
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
        advanced += 1
    return advanced
