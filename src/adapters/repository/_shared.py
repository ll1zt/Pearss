"""Shared row-conversion helpers and value types.

`row_to_item` is the single ItemRow -> domain Item mapper: every read path goes
through it, so a column added to db_models.Item needs one edit here."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select

from adapters.db_models import Item as ItemRow
from adapters.db_models import ItemSubscriptionReady as ReadyRow
from adapters.db_models import Subscription as SubscriptionRow

# The fixed-width format has one definition (adapters.timestamps); this
# module re-exports it under the historical `timestamp` name.
from adapters.timestamps import EPOCH_FLOOR as _EPOCH_FLOOR
from adapters.timestamps import fixed_width_utc as timestamp
from core.enums import (
    PublishStatus,
)
from core.models import Item
from core.normalize import DedupeAction

# Explicit re-exports: the package __init__ pulls `timestamp` from here.
__all__ = ["EPOCH_FLOOR", "StoreOutcome", "dumps", "row_to_item", "timestamp"]


@dataclass(frozen=True, slots=True, kw_only=True)
class StoreOutcome:
    """What a store attempt did."""

    action: DedupeAction
    item: Item
    # Set when the decision was to skip or suppress, naming the existing row.
    matched_id: str | None = None
    reason: str = ""


# Re-exported: the value is part of the timestamp format, so it is defined
# beside the format (adapters/timestamps.py) and imported here for the
# callers that already reach it through this module.
EPOCH_FLOOR = _EPOCH_FLOOR


def _optional_timestamp(moment: datetime | None) -> str | None:
    return None if moment is None else timestamp(moment)


def row_to_item(row: ItemRow) -> Item:
    """Rebuild a domain Item from a row.

    Stored timestamps are parsed back to aware datetimes so the domain object
    satisfies the same invariants as a freshly normalised one.
    """
    return Item(
        id=row.id,
        origin_id=row.origin_id,
        guid=row.guid,
        content_hash=row.content_hash,
        title=row.title,
        url=row.url,
        author=row.author,
        published_at=_parse(row.published_at),
        content_ref=row.content_ref,
        publish_status=PublishStatus(row.publish_status),
        published_to_feed_at=_parse(row.published_to_feed_at),
        tenant_id=row.tenant_id,
        created_at=_parse(row.created_at),
        updated_at=_parse(row.updated_at),
    )


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def dumps(value: object) -> str:
    """Serialise structured metadata for a text column."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def visible_item_rows() -> Any:
    """The gate, cross-library: ready under at least one subscription.

    ADR 0061: readiness is `(item, subscription)` -- a channel shows an item
    when *that* subscription's chain has completed for it. This is the
    "visible somewhere" form, which is what a search, a reading-list or a
    starred stream means. A read that names a channel uses
    `visible_item_rows_for` instead, because that one must not show an item
    another channel happens to have finished.

    The predicate was `publish_status != 'pending'`; that column keeps its own
    transitions (content frozen / first publish) and is no longer read here.

    This exists because the gate is an invariant enforced at five read sites
    (`feed`, `items_query` twice, `reader` twice) and one write site
    (`enrichment_worker`), and "the same rule written five times" is how an
    invariant drifts. The exclusion has to happen in SQL rather than in Python:
    filtering after the fetch would let a pending row reach memory, where a
    later change could render it -- `items_ready_for_feed`'s docstring says
    exactly that.

    `core.item_state.is_visible` is the same rule in Python and is the one
    ADR 0022 names, but it cannot be used here for that reason; this is the
    persistence-layer form of it.
    """
    # EXISTS, not IN: a correlated semi-join can be evaluated *while* the
    # planner walks `ix_item_sort_key_id`, so the page keeps its ordering
    # instead of being driven by the subquery and sorted into a temp b-tree
    # (measured: IN gave `SEARCH item USING INDEX sqlite_autoindex_item_1` plus
    # `USE TEMP B-TREE FOR ORDER BY`; the whole point of issue #77 is that this
    # page is a seek).
    return select(ReadyRow.item_id).where(ReadyRow.item_id == ItemRow.id).exists()


def visible_item_rows_for(subscription_id: str) -> Any:
    """The gate for one channel: this subscription's chain completed."""
    return (
        select(ReadyRow.item_id)
        .where(
            ReadyRow.item_id == ItemRow.id,
            ReadyRow.subscription_id == subscription_id,
        )
        .exists()
    )


def visible_item_rows_for_folder(*, folder: str, tenant_id: str) -> Any:
    """The gate for a folder stream: ready under a subscription in that folder."""
    return (
        select(ReadyRow.item_id)
        .join(SubscriptionRow, SubscriptionRow.id == ReadyRow.subscription_id)
        .where(
            ReadyRow.item_id == ItemRow.id,
            SubscriptionRow.folder == folder,
            SubscriptionRow.tenant_id == tenant_id,
        )
        .exists()
    )
