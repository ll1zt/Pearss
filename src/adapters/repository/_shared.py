"""Shared row-conversion helpers and value types.

`row_to_item` is the single ItemRow -> domain Item mapper: every read path goes
through it, so a column added to db_models.Item needs one edit here."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from adapters.db_models import Item as ItemRow

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

    `origin_id` is nullable in the schema until 0027 makes it NOT NULL, so this
    is where a row written around the write path shows up as an error rather
    than as an `Item` whose origin is None: every path that stores an item sets
    it (items._insert writes the legacy column and this one together).
    """
    if row.origin_id is None:  # unreachable: the write path always sets it
        raise ValueError(f"item {row.id!r} has no origin_id")
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
    """The publish gate as a SQL predicate: not pending.

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
    return ItemRow.publish_status != PublishStatus.PENDING.value
