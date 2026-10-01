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

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from adapters.correlation import current as current_correlation_id
from adapters.db_models import ItemEnrichment
from adapters.filestore import read_body
from adapters.repository import (
    ItemFilter,
    count_matching,
    get_item,
    mark_published,
    query_items,
    timestamp,
)
from adapters.repository import (
    delete_items as repository_delete_items,
)
from adapters.repository._shared import row_to_item
from adapters.repository.vector_search import duplicates_of, related_items
from app.api_conventions import (
    api_error,
    decode_cursor,
    enrichment_page,
    idempotency_key,
    remember,
    replay_or_none,
)
from app.auth import Principal, read_required, write_required
from core.enums import PublishStatus
from core.models import Item
from core.normalize import canonicalize_url
from core.tenancy import SINGLE_TENANT

LOGGER = logging.getLogger(__name__)


router = APIRouter(prefix="/v1", tags=["core-api"])

# Fields a caller may ask for. `body` is deliberately absent: it is not a
# column, and serving it here would defeat the rule that lists stay compact.
ITEM_FIELDS = (
    "id",
    "guid",
    "source_id",
    "title",
    "url",
    "author",
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
    # Only meaningful with `full_text`: an order by relevance needs something to
    # be relevant *to*. Asking for it without a query is a 422 rather than a
    # silent fall back to the time order, because the caller would otherwise
    # read the results as ranked when they are not.
    RELEVANCE = "relevance"
    # Both paths in full, fused by rank (ADR 0035 decision 7). It needs a query
    # for the same reason `relevance` does, and a second one: the semantic path
    # has to encode the text, so without one there is nothing to be similar to.
    HYBRID = "hybrid"


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
    from adapters.timestamps import fixed_width_utc

    return fixed_width_utc(value)


def _field_map(item: Item) -> dict[str, Any]:
    """Every selectable field of an item, in its wire form."""
    return {
        "id": item.id,
        "guid": item.guid,
        "source_id": item.source_id,
        "title": item.title,
        "url": item.url,
        "author": item.author,
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
        default=None,
        max_length=256,
        description="Opaque cursor from a previous response.",
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
    url: str | None = Query(
        default=None,
        max_length=2048,
        description=(
            "Exact match on an item's canonical url. The url is normalised the "
            "way identity normalises it (scheme/host case, default ports, "
            "tracking parameters, fragment), so a shared link with a `utm_` "
            "parameter finds the stored item. A miss is an empty page, not a 404."
        ),
    ),
    full_text: str | None = Query(
        default=None,
        max_length=200,
        description=(
            "Full-text search over titles and bodies, through the FTS5 index. "
            "Pair it with sort=relevance for best-match order; without it the "
            "matches stay newest-first."
        ),
    ),
    since: str | None = Query(default=None, max_length=32),
    until: str | None = Query(default=None, max_length=32),
) -> dict[str, Any]:
    """List collected items, newest first by default.

    Two ways to search, and they answer different questions: `q` is a substring
    match on the title, and `full_text` is a real search over titles and bodies
    (ADR 0035). `full_text` with `sort=relevance` ranks by FTS5's bm25; without
    it the matches keep the time order and its keyset cursor.

    Bodies are not included at any `fields` setting; fetch them from
    `/v1/items/{id}/body`. Pending items never appear.
    """
    del _
    selected = _select(fields)
    if sort is ItemSort.RELEVANCE and not full_text:
        raise api_error(
            status_code=422,
            code="relevance_without_query",
            message="sort=relevance needs full_text to rank by.",
            suggestion=(
                "Pass full_text=<what to search for>, or ask for one of the "
                "published_at orders."
            ),
        )
    if sort is ItemSort.HYBRID and not full_text:
        raise api_error(
            status_code=422,
            code="hybrid_without_query",
            message="sort=hybrid needs full_text: it is the query both paths use.",
            suggestion=(
                "Pass full_text=<what to search for>. The lexical path matches it "
                "and the semantic path encodes it, so the two answer one question."
            ),
        )
    descending = sort is ItemSort.PUBLISHED_DESC

    parsed_cursor = _parse_cursor(cursor)
    # `and full_text` is what narrows the type for the checker; the 422 above has
    # already refused the case where it is absent.
    query_vector = (
        _embed_query(full_text) if sort is ItemSort.HYBRID and full_text else None
    )

    with request.app.state.session_factory() as session:
        filter_spec = ItemFilter(
            source_id=source_id,
            publish_status=(
                publish_status.value if publish_status is not None else None
            ),
            query=q,
            full_text=full_text,
            url=_canonical_or_refuse(url),
            since=_parse_bound(since, code="invalid_since", field="since"),
            until=_parse_bound(until, code="invalid_until", field="until"),
        )
        page = query_items(
            session,
            tenant_id=SINGLE_TENANT,
            filter=filter_spec,
            descending=descending,
            limit=limit,
            cursor=parsed_cursor,
            query_vector=query_vector,
        )
        # How many rows the filter selects in total, not how many this page
        # returned. The sources list already answers this way, and it matters
        # most for callers deciding on a destructive action: "clear this
        # source" is a different decision at 3 items than at 3000, and paging
        # to the end to find out is a request per page.
        total = count_matching(session, tenant_id=SINGLE_TENANT, filter=filter_spec)

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
        "total": total,
        "correlation_id": current_correlation_id(),
    }


def _canonical_or_refuse(url: str | None) -> str | None:
    """The stored form of a caller's url, or a 422 the caller can act on.

    Normalised through the same function identity uses (`canonicalize_url`),
    because the point of the lookup is "is this address already stored?" -- and
    answering it against a differently-normalised string would say no to a row
    that is there. A url that cannot be normalised is refused rather than
    matched literally: a `mailto:` or a relative path has no stored form, and
    answering "no such item" would be technically true and useless.
    """
    if url is None:
        return None
    try:
        return canonicalize_url(url)
    except ValueError as exc:
        raise api_error(
            status_code=422,
            code="invalid_url",
            message=str(exc),
            suggestion=(
                "Pass an absolute http(s) url, e.g. the one an item's `url` "
                "field returned."
            ),
        ) from None


def _embed_query(text: str) -> list[float]:
    """The query vector for a hybrid search, or a 503 the caller can act on.

    Not a silent fall back to the lexical path: a caller who asked for hybrid and
    got keyword-only results would read them as fused, and the difference is
    exactly the recall they were asking for. The model is fetched by
    `just fetch-model`, so the suggestion is the command that fixes it.
    """
    from adapters.embedding_model import model_directory
    from core.plugins.hooks import ProcessorError
    from plugins.processors.embed.encoder import Encoder

    try:
        return Encoder(model_directory()).encode(text, as_query=True)
    except ProcessorError as exc:
        raise api_error(
            status_code=503,
            code="embedding_model_unavailable",
            message=f"the embedding model is not usable: {exc.detail}",
            suggestion=(
                "Run `just fetch-model` to install it, or use sort=relevance for "
                "the lexical path alone."
            ),
        ) from exc


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


def _parse_bound(value: str | None, *, code: str, field: str) -> str | None:
    """Normalise a `since`/`until` bound to the stored fixed-width form.

    The column is compared as text, so a bound in any other shape selects the
    wrong rows: `until=2026-01-03T00:00:00Z` (no fractional part) sorts after
    the stored `...T00:00:00.000000Z`, excluding the very instant it names,
    and an `+08:00` offset compares as garbage. Parsing and re-rendering in
    the stored form makes the comparison exact and refuses malformed input
    with a 400 instead of a quietly wrong page.
    """
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise api_error(
            status_code=400,
            code=code,
            message=f"{field} is not a valid timestamp.",
            suggestion="Send an RFC 3339 timestamp, e.g. 2026-09-06T00:00:00Z.",
        ) from None
    if moment.tzinfo is None:
        raise api_error(
            status_code=400,
            code=code,
            message=f"{field} must carry a timezone.",
            suggestion="Send an RFC 3339 timestamp with Z or an explicit offset.",
        )
    return timestamp(moment)


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


@router.get("/items/{item_id}/enrichments", operation_id="listItemEnrichments")
def list_item_enrichments(
    request: Request,
    item_id: str = Path(min_length=1, max_length=64),
    _: Principal = Depends(read_required),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None),
) -> dict[str, Any]:
    """Why *this* item is being held: its processor-chain rows, and nothing else.

    The dimension the source-scoped route lacks. An agent that already has an
    item id (from search_items) had to list its whole source's chain rows and
    filter them in context -- the shape the guide's "filter on the server" rule
    exists to avoid -- and it needed a title search first, which can come back
    empty for the very item it is holding (issue #72).

    A *pending* item is answered, unlike every other item read. The gate hides
    content, and these rows are the reason the content is held -- answering 404
    here would make the route useless for exactly the question it exists for (a
    chain that failed and left an item stuck). The source-scoped route already
    returned these rows for a source's pending items, so this is the consistent
    choice rather than a new exception; `detail` carries the processor's own
    error text, which is metadata about the attempt rather than the item.

    An id that names nothing is still 404.
    """
    del _
    offset = decode_cursor(cursor)
    with request.app.state.session_factory() as session:
        if get_item(session, item_id) is None:
            raise api_error(
                status_code=404,
                code="item_not_found",
                message=f"No item with id {item_id!r}.",
                suggestion="Search items to find the intended id.",
            )
        query = sa.select(ItemEnrichment).where(ItemEnrichment.item_id == item_id)
        if status is not None:
            query = query.where(ItemEnrichment.status == status)
        rows = list(session.execute(query).scalars().all())

    return enrichment_page(
        rows, limit=limit, offset=offset, correlation_id=current_correlation_id()
    )


def _neighbours(
    request: Request,
    item_id: str,
    *,
    limit: int,
    fields: str | None,
    finder: Any,
    absent: tuple[str, str, str],
) -> dict[str, Any]:
    """The shared body of the two neighbour endpoints.

    They ask the same question of the same index and differ in the floor and in
    what they say when there is nothing: one returns an empty list, the other
    refuses when the item has no vector at all. Sharing the lookup keeps the
    visibility rule -- a pending item is absent, never "forbidden" -- in one place.
    """
    selected = _select(fields)
    with request.app.state.session_factory() as session:
        item = get_item(session, item_id)
        if item is None or item.publish_status is PublishStatus.PENDING:
            # Absent rather than forbidden, exactly as the body endpoint reports
            # it: "this exists but you may not see it" would leak the gate.
            raise api_error(
                status_code=404,
                code="item_not_found",
                message=f"No item with id {item_id!r}.",
                suggestion="Search items to find the intended id.",
            )
        found = finder(session, item_id=item_id, tenant_id=SINGLE_TENANT, limit=limit)
        if found is None:
            code, message, suggestion = absent
            raise api_error(
                status_code=409, code=code, message=message, suggestion=suggestion
            )
    return {
        "data": [
            # `row_to_item` first: `_field_map` speaks the domain Item, whose
            # `publish_status` is an enum, while the row carries the raw string.
            {
                name: value
                for name, value in _field_map(row_to_item(row)).items()
                if name in selected
            }
            for row, _score in found
        ],
        "correlation_id": current_correlation_id(),
    }


@router.get("/items/{item_id}/related", operation_id="listRelatedItems")
def list_related_items(
    request: Request,
    item_id: str = Path(min_length=1, max_length=64),
    limit: int = Query(default=50, ge=1, le=50),
    fields: str | None = Query(default=None),
    _: Principal = Depends(read_required),
) -> dict[str, Any]:
    """Items most like this one, by meaning rather than by words (AC-108).

    "Like" here is a vector neighbourhood, which is a different question from
    search: there is no query, so nothing is asked to match, and the answer is
    whatever is nearest above a measured floor. Without the floor this would be
    the whole corpus in similarity order -- a vector search always has a nearest
    neighbour, however unrelated -- and a list that always returns something
    teaches its caller to ignore it.
    """
    del _
    return _neighbours(
        request,
        item_id,
        limit=limit,
        fields=fields,
        finder=related_items,
        absent=(
            "item_has_no_embedding",
            "This item has no vector, so it has no neighbours.",
            (
                "Add the embed processor to the source's chain so the worker "
                "produces one, or run the enrichment backfill for items stored "
                "before the chain existed."
            ),
        ),
    )


@router.get("/items/{item_id}/duplicates", operation_id="listDuplicateItems")
def list_duplicate_items(
    request: Request,
    item_id: str = Path(min_length=1, max_length=64),
    limit: int = Query(default=50, ge=1, le=50),
    fields: str | None = Query(default=None),
    _: Principal = Depends(read_required),
) -> dict[str, Any]:
    """Items that look like another copy of this one (AC-109).

    A hint, and only a hint. The measurement behind the threshold puts a copy and
    a genuine paraphrase 0.014 apart, so no threshold can be right about that gap;
    what this endpoint can honestly say is "these two are close", and what to do
    about it is the caller's decision (ADR 0021: deletion belongs to the user).
    Nothing here merges, deletes, rewrites or re-keys anything.
    """
    del _
    return _neighbours(
        request,
        item_id,
        limit=limit,
        fields=fields,
        finder=duplicates_of,
        absent=(
            "item_has_no_embedding",
            "This item has no vector, so it cannot be compared with anything.",
            (
                "Add the embed processor to the source's chain so the worker "
                "produces one, or run the enrichment backfill for items stored "
                "before the chain existed."
            ),
        ),
    )


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

    Serving a body is an observable first publication (ADR 0016: with the
    public RSS render removed, this and the Reader API are the paths where a
    client actually sees content), so the item's content is frozen here — the
    immutability guarantee exists because clients cache what they were shown.
    """
    del _
    with request.app.state.session_factory() as session:
        item = get_item(session, item_id)
        if item is None or item.publish_status is PublishStatus.PENDING:
            # A pending item is reported absent rather than as forbidden:
            # saying "this exists but you may not see it" would leak the
            # gate's decisions to a caller who has no business knowing them.
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

    # Only a successful read freezes the item: a corrupt store must not be
    # mistaken for a published one.
    with request.app.state.session_factory() as session:
        mark_published(session, [item], at=datetime.now(UTC))
        session.commit()

    return Response(
        content=body,
        media_type="text/html; charset=utf-8",
        # The body is nh3-sanitised remote HTML served on the API's own
        # origin; `sandbox` makes even a sanitiser mistake inert (no script,
        # no forms, no same-origin fetches), and nosniff stops content-type
        # guessing in browsers that ignore the declared type.
        headers={
            "content-security-policy": "sandbox",
            "x-content-type-options": "nosniff",
        },
    )


# --------------------------------------------------------------------------
# Deletion (ADR 0021)
# --------------------------------------------------------------------------


class DeleteItemsRequest(BaseModel):
    """Ids to delete, and nothing else.

    A list rather than a filter. A filter reads as a convenient way to say
    "everything matching this", which is exactly the blast radius this
    operation should not have; a caller wanting a whole source cleared names
    the source endpoint, where that intent is explicit.
    """

    item_ids: list[str] = Field(min_length=1, max_length=500)


def _delete_items_payload(
    request: Request,
    item_ids: list[str],
    *,
    principal: Principal,
    endpoint: str,
    key: str,
) -> JSONResponse:
    """Delete `item_ids` and answer with what actually went.

    Shared by both routes so they cannot drift: they are one operation, and
    only the shape of the id list differs.
    """
    factory = request.app.state.session_factory
    replayed = replay_or_none(factory, actor=principal.name, endpoint=endpoint, key=key)
    if replayed is not None:
        return replayed

    with factory() as session:
        result = repository_delete_items(session, item_ids)
        session.commit()

    # After the commit, never before: see ReclaimPlan. A rollback would restore
    # the rows, and files removed inside the transaction cannot come back.
    removed = result.reclaim.reclaim()

    payload: dict[str, Any] = {
        "requested": len(item_ids),
        **result.counts,
        # What was actually unlinked, which can be less than the count of
        # reclaimable refs if a file had already gone missing.
        "files_removed": removed,
        "correlation_id": current_correlation_id(),
    }
    remember(
        factory,
        actor=principal.name,
        endpoint=endpoint,
        key=key,
        status_code=200,
        body=payload,
    )
    return JSONResponse(status_code=200, content=payload)


@router.delete("/items/{item_id}", operation_id="deleteItem")
def delete_item(
    request: Request,
    item_id: str = Path(min_length=1, max_length=64),
    principal: Principal = Depends(write_required),
    key: str = Depends(idempotency_key),
) -> JSONResponse:
    """Delete one item, permanently.

    Removes the row, its stored body, its read state and its processor
    products. Not reversible and not a hide: the content is gone, and the
    client's cached copy is the only one left (ADR 0021).

    An id naming nothing answers 200 with `items: 0`, not 404 -- see
    delete_source for why a retry must be indistinguishable from the first.
    """
    return _delete_items_payload(
        request,
        [item_id],
        principal=principal,
        endpoint=f"DELETE /v1/items/{item_id}",
        key=key,
    )


@router.post("/items:delete", operation_id="deleteItems")
def delete_items(
    request: Request,
    body: DeleteItemsRequest,
    principal: Principal = Depends(write_required),
    key: str = Depends(idempotency_key),
) -> JSONResponse:
    """Delete several items in one call.

    POST rather than DELETE-with-body: DELETE bodies are unevenly supported by
    clients and proxies, and an id list can be long enough to matter. The
    `:delete` suffix matches the other verb-style routes.
    """
    return _delete_items_payload(
        request,
        body.item_ids,
        principal=principal,
        endpoint="POST /v1/items:delete",
        key=key,
    )
