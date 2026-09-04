"""Core API: /v1/items.

The compact query surface C2 asks for. Two rules shape it:

- **Bodies are never inlined in a list.** docs/api/style-guide.md forbids it and
  the architecture routes large content through a separate endpoint or Resource
  (`item://{id}`) so an agent's context is not spent on HTML it did not ask for.
- **The gate applies here too.** A pending item is invisible to every output,
  and this is an output.

Sort and field choices are enums so they appear in the OpenAPI document, which
is the contract the MCP surface is generated from.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel

from adapters.correlation import current as current_correlation_id
from adapters.filestore import read_body
from adapters.repository import ItemFilter, get_item, query_items
from app.auth import Principal, read_required
from app.sources_routes import api_error
from core.enums import PublishStatus
from core.models import Item

LOGGER = logging.getLogger(__name__)

TENANT = "default"

router = APIRouter(prefix="/v1", tags=["core-api"])

# Fields a caller may ask for. `body` is deliberately absent: it is not a
# column, and serving it here would defeat the rule that lists stay compact.
ITEM_FIELDS = (
    "id",
    "guid",
    "source_id",
    "title",
    "url",
    "published_at",
    "publish_status",
    "content_hash",
    "content_ref",
    "tenant_id",
    "created_at",
    "updated_at",
    "published_to_feed_at",
)

# What a list returns when the caller does not choose. Enough to identify and
# display a row; the body and the storage reference are one request away.
DEFAULT_FIELDS = ("id", "guid", "title", "url", "published_at", "publish_status")


class ItemSort(StrEnum):
    """The sortable orders, whitelisted rather than free-form.

    An arbitrary column name here would either fail at the database or, if
    interpolated, become an injection point — SQL identifiers cannot be passed
    as query parameters.
    """

    PUBLISHED_DESC = "-published_at"
    PUBLISHED_ASC = "published_at"


class ItemSummary(BaseModel):
    """One item, without its body."""

    id: str
    guid: str
    title: str
    url: str
    published_at: str | None = None
    publish_status: str

    model_config = {"json_schema_extra": {"example": {"id": "0a1b…"}}}


def _iso(value: datetime | None) -> str | None:
    """Render a domain timestamp in the same fixed-width form as storage.

    Fixed width matters here too: these strings are what a client compares and
    what a keyset cursor round-trips through, so a value that sorts correctly in
    the database but prints inconsistently here would break paging.
    """
    if value is None:
        return None
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _field_map(item: Item) -> dict[str, Any]:
    """Every selectable field of an item, in its wire form."""
    return {
        "id": item.id,
        "guid": item.guid,
        "source_id": item.source_id,
        "title": item.title,
        "url": item.url,
        "published_at": _iso(item.published_at),
        "publish_status": item.publish_status.value,
        "content_hash": item.content_hash,
        "content_ref": item.content_ref,
        "tenant_id": item.tenant_id,
        "created_at": _iso(item.created_at),
        "updated_at": _iso(item.updated_at),
        "published_to_feed_at": _iso(item.published_to_feed_at),
    }


def _select(requested: str | None) -> tuple[str, ...]:
    """Resolve the `fields` parameter, rejecting anything not offered."""
    if not requested:
        return DEFAULT_FIELDS
    chosen = tuple(part.strip() for part in requested.split(",") if part.strip())
    if not chosen:
        return DEFAULT_FIELDS
    unknown = [name for name in chosen if name not in ITEM_FIELDS]
    if unknown:
        raise api_error(
            status_code=400,
            code="unknown_fields",
            message=f"Unknown field(s): {', '.join(unknown)}.",
            suggestion=f"Choose from: {', '.join(ITEM_FIELDS)}",
            details=[{"unknown": list(unknown)}],
        )
    return chosen


@router.get("/items", operation_id="searchItems")
def read_items(
    request: Request,
    _: Principal = Depends(read_required),
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(
        default=None, description="Opaque cursor from a previous response."
    ),
    sort: ItemSort = Query(
        default=ItemSort.PUBLISHED_DESC,
        description="Sort order. Only these two are supported.",
    ),
    fields: str | None = Query(
        default=None,
        description=f"Comma-separated subset of: {', '.join(ITEM_FIELDS)}.",
    ),
    source_id: str | None = Query(default=None, max_length=64),
    publish_status: PublishStatus | None = Query(
        default=None,
        description=(
            "Narrow within what the gate allows. `pending` can never be "
            "returned: it is excluded before this filter applies."
        ),
    ),
    q: str | None = Query(
        default=None, max_length=200, description="Substring match on the title."
    ),
    since: str | None = Query(default=None, max_length=32),
    until: str | None = Query(default=None, max_length=32),
) -> dict[str, Any]:
    """List collected items, newest first by default.

    Bodies are not included at any `fields` setting; fetch them from
    `/v1/items/{id}/body`. Pending items never appear.
    """
    del _
    selected = _select(fields)
    descending = sort is ItemSort.PUBLISHED_DESC

    parsed_cursor = _parse_cursor(cursor)

    with request.app.state.session_factory() as session:
        page = query_items(
            session,
            tenant_id=TENANT,
            filter=ItemFilter(
                source_id=source_id,
                publish_status=(
                    publish_status.value if publish_status is not None else None
                ),
                query=q,
                since=since,
                until=until,
            ),
            descending=descending,
            limit=limit,
            cursor=parsed_cursor,
        )

    return {
        "data": [
            {
                name: value
                for name, value in _field_map(item).items()
                if name in selected
            }
            for item in page.items
        ],
        "next_cursor": page.next_cursor,
        "correlation_id": current_correlation_id(),
    }


def _parse_cursor(cursor: str | None) -> tuple[str, str] | None:
    """Split an opaque keyset cursor into (sort value, id)."""
    if not cursor:
        return None
    value, sep, item_id = cursor.partition("|")
    if not sep or not value or not item_id or len(item_id) > 64:
        raise api_error(
            status_code=400,
            code="invalid_cursor",
            message="The cursor is not valid.",
            suggestion="Omit the cursor to start from the beginning.",
        )
    return (value, item_id)


@router.get("/items/{item_id}", operation_id="getItem")
def read_item(
    request: Request,
    item_id: str = Path(min_length=1, max_length=64),
    _: Principal = Depends(read_required),
    fields: str | None = Query(default=None),
) -> dict[str, Any]:
    """One item, without its body."""
    del _
    selected = _select(fields) if fields else ITEM_FIELDS
    with request.app.state.session_factory() as session:
        item = get_item(session, item_id)
    if item is None or item.publish_status is PublishStatus.PENDING:
        # A pending item is reported absent rather than as forbidden: saying
        # "this exists but you may not see it" would leak the gate's decisions
        # to a caller who has no business knowing them.
        raise api_error(
            status_code=404,
            code="item_not_found",
            message=f"No item with id {item_id!r}.",
            suggestion="Search items to find the intended id.",
        )
    return {name: v for name, v in _field_map(item).items() if name in selected}


@router.get("/items/{item_id}/body", operation_id="getItemBody")
def read_item_body(
    request: Request,
    item_id: str = Path(min_length=1, max_length=64),
    _: Principal = Depends(read_required),
) -> Response:
    """The item's body, as its own request.

    Separate by design: docs/api/style-guide.md forbids inlining HTML bodies in
    list responses, and the architecture routes large content through an
    explicit reference so a caller pays for it only when it asks.
    """
    del _
    with request.app.state.session_factory() as session:
        item = get_item(session, item_id)
    if item is None or item.publish_status is PublishStatus.PENDING:
        raise api_error(
            status_code=404,
            code="item_not_found",
            message=f"No item with id {item_id!r}.",
            suggestion="Search items to find the intended id.",
        )
    if not item.content_ref:
        raise api_error(
            status_code=404,
            code="body_not_stored",
            message="This item has no stored body.",
            suggestion=(
                "The source may publish no body, or the item predates body "
                "storage. Check content_ref on the item."
            ),
        )
    body = read_body(item.content_ref)
    if not body:
        raise api_error(
            status_code=404,
            code="body_unreadable",
            message="The stored body could not be read.",
            suggestion="Check the storage directory and the item's content_ref.",
            details=[{"content_ref": item.content_ref}],
        )
    return Response(content=body, media_type="text/html; charset=utf-8")
