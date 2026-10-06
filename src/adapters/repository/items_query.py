"""Item queries: keyset pagination over the gate-visible set."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from adapters.db_models import Item as ItemRow
from adapters.repository._shared import EPOCH_FLOOR, row_to_item, visible_item_rows
from adapters.repository.hybrid_search import fused_item_ids
from adapters.repository.search_index import count_matches, search_rows
from adapters.timestamps import fixed_width_utc as timestamp
from core.models import Item


@dataclass(frozen=True, slots=True, kw_only=True)
class ItemFilter:
    """Constraints on an item query.

    `publish_status` narrows within what the gate already allows; it can never
    widen it, because pending rows are excluded in SQL regardless.
    """

    origin_id: str | None = None
    publish_status: str | None = None
    query: str | None = None
    # Full-text over the title *and* the body, through the FTS5 index. Separate
    # from `query` on purpose: `query` is a title substring match that callers
    # already depend on, and this is a different question (see issue #80, ADR
    # 0035 decision 2).
    full_text: str | None = None
    # Exact match on the item's stored `url`, which is already the canonical
    # form (`normalize` stores `canonicalize_url(draft.canonical_url)`), so a
    # caller passes a canonicalised address and the comparison is literal.
    # Separate from `query`/`full_text` because "which item is this address?"
    # is an identity question: neither a title substring nor a body search can
    # answer it (measured: the address-to-item lookup had no answer at all).
    url: str | None = None
    since: str | None = None
    until: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ItemPage:
    """One page plus the cursor for the next, if any."""

    items: list[Item]
    next_cursor: str | None


def escape_like(term: str) -> str:
    """Escape LIKE wildcards so a search term matches literally.

    Without this, a query of `%` matches every row and `_` matches any single
    character — surprising but harmless — while a term like `100%_off` becomes
    an unintended pattern.
    """
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def query_items(
    session: Session,
    *,
    tenant_id: str,
    filter: ItemFilter,
    descending: bool,
    limit: int,
    cursor: tuple[str, str] | None = None,
    query_vector: Sequence[float] | None = None,
) -> ItemPage:
    """Keyset-pagated item query.

    Keyset rather than offset because new items arrive at the front of a
    newest-first ordering. An offset cursor walks a shifting window: page two
    silently skips whatever was inserted ahead of it, and can repeat rows too.
    Keyset compares against the last seen (sort value, id), so inserts elsewhere
    cannot move the boundary.

    `cursor` is the (sort_value, id) of the final row of the previous page.
    """
    # The generated column, not the expression: an indexed expression is only
    # matched while its text is identical, and the sort key has to keep using
    # the index it is named for (see db_models.Item.sort_key).
    if filter.full_text and query_vector is not None:
        # Two paths, fused by rank (ADR 0035 decision 7). The order is neither
        # the time order nor a single index's, so like the relevance branch it
        # cannot be expressed as one more WHERE on the keyset query below.
        return _fused_page(
            session,
            tenant_id=tenant_id,
            filter=filter,
            text=filter.full_text,
            query_vector=query_vector,
            limit=limit,
            cursor=cursor,
        )

    if filter.full_text:
        # A different query shape, not a different condition: the order is
        # relevance and the rows come from the index, so it cannot be expressed
        # as one more WHERE on the keyset query below.
        # A separate name from the keyset branch's `rows`: the two hold
        # different shapes, and reusing one name is how a type checker (and a
        # reader) loses track of which branch they are in.
        matches, next_cursor = search_rows(
            session,
            conditions=_filter_conditions(
                tenant_id, replace(filter, full_text=None), ItemRow.sort_key
            ),
            text=filter.full_text,
            limit=limit,
            cursor=cursor,
        )
        return ItemPage(
            items=[row_to_item(row) for row, _score in matches],
            next_cursor=next_cursor,
        )

    sort_column = ItemRow.sort_key

    query = select(ItemRow).where(*_filter_conditions(tenant_id, filter, sort_column))

    if cursor is not None:
        value, last_id = cursor
        # (sort, id) strictly past the cursor in the requested direction, with
        # id as the tie-break so rows sharing a timestamp still page cleanly.
        if descending:
            query = query.where(
                or_(
                    sort_column < value,
                    and_(sort_column == value, ItemRow.id < last_id),
                )
            )
        else:
            query = query.where(
                or_(
                    sort_column > value,
                    and_(sort_column == value, ItemRow.id > last_id),
                )
            )

    order = (
        (sort_column.desc(), ItemRow.id.desc())
        if descending
        else (sort_column.asc(), ItemRow.id.asc())
    )
    # One beyond the page: the extra row says whether a cursor is warranted,
    # without a second round-trip to count.
    rows = list(session.execute(query.order_by(*order).limit(limit + 1)).scalars())

    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = None
    if has_more and page:
        last = page[-1]
        next_cursor = f"{_sort_value(last)}|{last.id}"

    return ItemPage(items=[row_to_item(row) for row in page], next_cursor=next_cursor)


def _fused_page(
    session: Session,
    *,
    tenant_id: str,
    filter: ItemFilter,
    text: str,
    query_vector: Sequence[float],
    limit: int,
    cursor: tuple[str, str] | None,
) -> ItemPage:
    """One page of the fused order, plus the cursor for the next.

    The cursor is `(score, id)` printed, the same shape the relevance branch uses
    and for the same reason: an offset would walk a shifting window. Scores come
    from the fusion of two deterministic paths, so they round-trip exactly.

    `total` counts what the caller can enumerate -- the fused list, which is
    bounded by the depth each path was asked for, not the number of items in the
    corpus. A count that promised more than the list can produce is the kind of
    number callers page to the end trying to reach.
    """
    fused = fused_item_ids(
        session,
        text=text,
        query=query_vector,
        tenant_id=tenant_id,
    )
    conditions = _filter_conditions(
        tenant_id, replace(filter, full_text=None), ItemRow.sort_key
    )
    rows = {
        row.id: row
        for row in session.execute(select(ItemRow).where(*conditions)).scalars()
    }
    ordered = [(rows[item_id], score) for item_id, score in fused if item_id in rows]
    if cursor is not None:
        after_score, after_id = float(cursor[0]), cursor[1]
        ordered = [
            (row, score)
            for row, score in ordered
            if score < after_score or (score == after_score and row.id > after_id)
        ]
    page = ordered[:limit]
    next_cursor = (
        f"{page[-1][1]!r}|{page[-1][0].id}" if len(ordered) > len(page) else None
    )
    return ItemPage(
        items=[row_to_item(row) for row, _score in page], next_cursor=next_cursor
    )


def _filter_conditions(
    tenant_id: str, filter: ItemFilter, sort_column: Any
) -> list[Any]:
    """The WHERE clauses a filter selects by, shared by the page and the count.

    Factored out for the reason the gate is enforced in one place: a count that
    drifts from its page is worse than no count, and the two would drift the
    first time a filter was added to only one of them.
    """
    conditions: list[Any] = [
        # The gate. Pending rows are invisible to every output, and a count is
        # an output -- including it here would report a total the caller can
        # never enumerate.
        visible_item_rows(),
        ItemRow.tenant_id == tenant_id,
    ]
    if filter.origin_id:
        conditions.append(ItemRow.origin_id == filter.origin_id)
    if filter.publish_status:
        conditions.append(ItemRow.publish_status == filter.publish_status)
    if filter.query:
        conditions.append(
            ItemRow.title.like(f"%{escape_like(filter.query)}%", escape="\\")
        )
    if filter.url:
        conditions.append(ItemRow.url == filter.url)
    if filter.since:
        conditions.append(sort_column >= filter.since)
    if filter.until:
        conditions.append(sort_column <= filter.until)
    return conditions


def count_matching(session: Session, *, tenant_id: str, filter: ItemFilter) -> int:
    """How many items the filter selects in total, ignoring any page window.

    This is what tells a caller whether "clear this source" means three items
    or three thousand; paging to the end to find out costs a request per page.
    """
    if filter.full_text:
        return count_matches(
            session,
            conditions=_filter_conditions(
                tenant_id, replace(filter, full_text=None), ItemRow.sort_key
            ),
            text=filter.full_text,
        )

    sort_column = ItemRow.sort_key
    return int(
        session.execute(
            select(func.count())
            .select_from(ItemRow)
            .where(*_filter_conditions(tenant_id, filter, sort_column))
        ).scalar_one()
    )


def _sort_value(row: ItemRow) -> str:
    """The row's keyset position, matching the COALESCE used in the query."""
    return row.published_at or EPOCH_FLOOR


def get_item(session: Session, item_id: str) -> Item | None:
    """One item by id, or None.

    The gate is not applied here: callers need to tell "absent" from "present
    but pending" so they can answer 404 without leaking which is which.
    """
    row = session.get(ItemRow, item_id)
    return row_to_item(row) if row is not None else None


def update_item_content_ref(
    session: Session, *, item_id: str, content_ref: str
) -> None:
    """Point an item at its stored body."""
    row = session.get(ItemRow, item_id)
    if row is None:
        raise ValueError(f"no item with id {item_id!r}")
    row.content_ref = content_ref
    row.updated_at = timestamp(datetime.now(UTC))
