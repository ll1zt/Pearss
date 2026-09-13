"""Shared row-conversion helpers and value types.

`row_to_item` is the single ItemRow -> domain Item mapper: every read path goes
through it, so a column added to db_models.Item needs one edit here."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from adapters.db_models import Item as ItemRow

# The fixed-width format has one definition (adapters.timestamps); this
# module re-exports it under the historical `timestamp` name.
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


EPOCH_FLOOR = "0000-01-01T00:00:00.000000Z"


def _optional_timestamp(moment: datetime | None) -> str | None:
    return None if moment is None else timestamp(moment)


def row_to_item(row: ItemRow) -> Item:
    """Rebuild a domain Item from a row.

    Stored timestamps are parsed back to aware datetimes so the domain object
    satisfies the same invariants as a freshly normalised one.
    """
    return Item(
        id=row.id,
        source_id=row.source_id,
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
