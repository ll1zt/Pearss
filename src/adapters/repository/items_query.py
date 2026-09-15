"""Item queries: keyset pagination over the gate-visible set."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from adapters.db_models import Item as ItemRow
from adapters.repository._shared import EPOCH_FLOOR, row_to_item, visible_item_rows
from adapters.timestamps import fixed_width_utc as timestamp
from core.models import Item


@dataclass(frozen=True, slots=True, kw_only=True)
class ItemFilter:
    """Constraints on an item query.

    `publish_status` narrows within what the gate already allows; it can never
    widen it, because pending rows are excluded in SQL regardless.
    """

    source_id: str | None = None
    publish_status: str | None = None
    query: str | None = None
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
) -> ItemPage:
    """Keyset-pagated item query.

    Keyset rather than offset because new items arrive at the front of a
    newest-first ordering. An offset cursor walks a shifting window: page two
    silently skips whatever was inserted ahead of it, and can repeat rows too.
    Keyset compares against the last seen (sort value, id), so inserts elsewhere
    cannot move the boundary.

    `cursor` is the (sort_value, id) of the final row of the previous page.
    """
    sort_column = func.coalesce(ItemRow.published_at, EPOCH_FLOOR)

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
    if filter.source_id:
        conditions.append(ItemRow.source_id == filter.source_id)
    if filter.publish_status:
        conditions.append(ItemRow.publish_status == filter.publish_status)
    if filter.query:
        conditions.append(
            ItemRow.title.like(f"%{escape_like(filter.query)}%", escape="\\")
        )
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
    sort_column = func.coalesce(ItemRow.published_at, EPOCH_FLOOR)
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
