"""Reader API persistence: stream visibility, credentials, read state."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from adapters.db_models import Item as ItemRow
from adapters.db_models import Origin as OriginRow
from adapters.db_models import ReaderCredential
from adapters.db_models import ReadState as ReadStateRow
from adapters.db_models import Subscription as SubscriptionRow
from adapters.repository._shared import _parse, visible_item_rows
from adapters.timestamps import fixed_width_utc as timestamp
from core.reader_state import ReadState as ReadStateDomain
from core.reader_state import TagChange, merge_state
from core.tenancy import SINGLE_TENANT

LOGGER = logging.getLogger(__name__)


class AmbiguousChannelError(Exception):
    """A legacy channel id names an origin with more than one subscription.

    Raised rather than resolved arbitrarily: the caller asked for one channel,
    and picking one of several would show it a different channel's items under
    the id it named -- the wrong answer that looks like data.
    """


def origin_of_channel(
    session: Session, *, channel_id: str, tenant_id: str
) -> str | None:
    """The origin a Reader channel id names, resolving the pre-split alias.

    A channel is a **subscription** (ADR 0046 D-C): what a client subscribes to
    is the policy, and the items are its origin's. A client that cached
    `feed/<id>` before the split may still be sending an *origin* id, so an id
    with no subscription behind it is tried as an origin -- but only while that
    origin has exactly one subscription. With none it is not a channel (None);
    with several there is no single answer, and guessing is the failure this
    function exists to prevent (`AmbiguousChannelError`).
    """
    subscription = session.execute(
        select(SubscriptionRow.origin_id).where(
            SubscriptionRow.id == channel_id,
            SubscriptionRow.tenant_id == tenant_id,
        )
    ).scalar_one_or_none()
    if subscription is not None:
        return subscription

    origin = session.execute(
        select(OriginRow.id).where(
            OriginRow.id == channel_id, OriginRow.tenant_id == tenant_id
        )
    ).scalar_one_or_none()
    if origin is None:
        return None
    subscriptions = session.execute(
        select(func.count())
        .select_from(SubscriptionRow)
        .where(SubscriptionRow.origin_id == origin)
    ).scalar_one()
    if subscriptions == 0:
        return None
    if subscriptions > 1:
        raise AmbiguousChannelError(
            f"channel {channel_id!r} names an origin with {subscriptions} "
            "subscriptions; name the subscription instead"
        )
    return origin


def visible_ids_for_stream(
    session: Session,
    *,
    stream_id: str,
    tenant_id: str,
    limit: int = 10000,
    offset: int = 0,
    include_states: tuple[str, ...] = (),
    exclude_states: tuple[str, ...] = (),
    after_published_at: str | None = None,
    after_item_id: str | None = None,
    after_published_null: bool = False,
    published_after: str | None = None,
    with_ordering: bool = False,
) -> list[str] | list[tuple[str, str | None]]:
    """Item ids for a Reader API stream, newest first.

    Pending items are excluded here, in SQL, for the same reason the RSS path
    excludes them: a client that has seen an item cannot be shown a different
    version of it, so the gate has to apply before anything is emitted.

    A state stream filters on that state. An unrecognised one yields **nothing**:
    falling through to "everything" is what made `.../starred` return the whole
    library, which a client then renders as every item being starred. Serving an
    empty list for a filter we do not implement is an honest gap; serving the
    unfiltered list is a wrong answer that looks like data.

    `include_states`/`exclude_states` are the `it=`/`xt=` tag filters Read You
    sends. They are applied **in SQL, before `limit`** -- filtering afterwards
    would let the limit silently drop items that match, so a client asking for
    "the newest 1000 unread" would receive fewer than 1000 and count the
    difference as items that do not exist. That is a wrong answer that looks
    like data, and it drifts as items are read, which is exactly the bug the
    unread badge was showing.

    `after_*` carries the keyset boundary from a continuation cursor: the last
    `(published_at, id)` actually served. Unlike an OFFSET, a stable boundary
    survives membership changes between pages -- an item read elsewhere
    shrinks the set, a deletion removes a served row, a new arrival pushes
    rows deeper, and in all three cases the next page still starts exactly
    after the last served id rather than at an absolute row number.

    `published_after` is the `ot=` window: a fixed-width UTC bound, applied
    before `limit` for the same reason `it`/`xt` are -- a window filtered after
    the limit would return fewer items than asked and look like missing data.
    It bounds *dated* history only: undated rows are kept, because they have no
    position in that ordering and dropping them makes their read state
    unsynchronisable (ADR 0024, 2026-10-05 revision).
    """
    from adapters import reader_api

    query = select(ItemRow.id, ItemRow.published_at).where(visible_item_rows())

    subscription_id = reader_api.parse_feed_stream_id(stream_id)
    if subscription_id is not None:
        origin_id = origin_of_channel(
            session, channel_id=subscription_id, tenant_id=tenant_id
        )
        if origin_id is None:
            # A channel id no subscription carries is an empty channel, not
            # "everything" -- the same rule the label branch applies below.
            return []
        query = query.where(ItemRow.origin_id == origin_id)
    else:
        state = reader_api.state_stream_name(stream_id)
        if state == "reading-list":
            pass  # every visible item; nothing further to filter on
        elif state == "starred":
            query = query.where(
                ItemRow.id.in_(
                    select(ReadStateRow.item_id).where(
                        ReadStateRow.is_starred.is_(True),
                        ReadStateRow.tenant_id == tenant_id,
                    )
                )
            )
        elif state == "read":
            # The read stream itself. A client that asks for it by name --
            # NetNewsWire's Google-Reader mode and Read You's non-FreshRSS
            # branch both do -- got the "unhandled stream" answer, i.e. an
            # empty read list forever, while the same server answered the
            # `it=read` include-tag form correctly. Both spellings must
            # agree; this branch delegates to the same predicate the tag
            # filter applies, so they cannot drift again.
            query = query.where(
                ItemRow.id.in_(
                    select(ReadStateRow.item_id).where(
                        ReadStateRow.is_read.is_(True),
                        ReadStateRow.tenant_id == tenant_id,
                    )
                )
            )
        elif (label := reader_api.label_stream_name(stream_id)) is not None:
            # A folder stream: items of the origins the subscriptions in that
            # folder consume. A name no subscription carries is an empty
            # stream, not "everything" -- serving the unfiltered list for a
            # filter with no matches is a wrong answer that looks like data.
            query = query.where(
                ItemRow.origin_id.in_(
                    select(SubscriptionRow.origin_id).where(
                        SubscriptionRow.folder == label,
                        SubscriptionRow.tenant_id == tenant_id,
                    )
                )
            )
        elif state in {"broadcast", "verified"}:
            # Shared/verified streams: we record neither, so there is nothing
            # to list. Distinct from the unknown-state branch only in intent.
            return []
        else:
            LOGGER.info("unhandled Reader stream %r; returning no items", stream_id)
            return []

    query = _apply_state_filters(
        query,
        tenant_id=tenant_id,
        include_states=include_states,
        exclude_states=exclude_states,
    )

    if published_after is not None:
        # `ot` bounds *dated* history: strictly after the bound, with the
        # undated rows kept. Dropping them reads as tidy and is worse than
        # wrong -- Read You builds its `remoteRead` set from exactly this
        # windowed query and its `remoteUnread` set from an unwindowed one, so
        # an already-read undated item lands in neither and `toBeReadLocal`
        # never carries "the server says read" back to the phone. The
        # feed-level path (`getItemIdsForFeed`) sends no `ot` and does serve
        # it; that disagreement between one client's two paths is how this was
        # found (issue #285, ADR 0024's 2026-10-05 revision).
        query = query.where(
            or_(
                ItemRow.published_at.is_(None),
                ItemRow.published_at > published_after,
            )
        )

    # Keyset continuation, not OFFSET: a membership change between pages
    # (an item read elsewhere, a deletion, a new arrival) shifts an offset
    # past unserved rows, while a stable `(published_at, id)` boundary keeps
    # every page anchored to the last row actually served.
    #
    # The tiebreak is `id DESC`, matching the predicate below (`id <` the last
    # served id) and the ordering convention in `items_query`: an ascending
    # tiebreak under a `<` predicate re-serves the group's smallest id and never
    # reaches the rest of it (issue #274).
    if after_item_id is not None:
        if after_published_null or after_published_at is None:
            # The boundary row had no published_at; with nullslast those rows
            # sort after every dated one, so the page continues inside the
            # undated tail by id.
            query = query.where(
                ItemRow.published_at.is_(None), ItemRow.id < after_item_id
            )
        else:
            # Dated rows strictly after the boundary position, plus the whole
            # undated tail (nullslast puts it after every dated row).
            query = query.where(
                or_(
                    (ItemRow.published_at.is_not(None))
                    & (
                        (ItemRow.published_at < after_published_at)
                        | (
                            (ItemRow.published_at == after_published_at)
                            & (ItemRow.id < after_item_id)
                        )
                    ),
                    ItemRow.published_at.is_(None),
                )
            )
    elif offset:
        query = query.offset(offset)

    rows = session.execute(
        query.order_by(
            ItemRow.published_at.desc().nullslast(), ItemRow.id.desc()
        ).limit(limit)
    ).all()
    if with_ordering:
        return [(row_id, published) for row_id, published in rows]
    return [row_id for row_id, _ in rows]


def _apply_state_filters(
    query: Any,
    *,
    tenant_id: str,
    include_states: tuple[str, ...],
    exclude_states: tuple[str, ...],
) -> Any:
    """Narrow a stream query by `it=`/`xt=` states, in SQL.

    Every modelled state lives in `read_state`: an item with no row there is
    unread and unstarred (see `read_states_for`). So "must be read" is
    `IN (row WHERE is_read)` and "must not be read" is its complement.

    A state that matches nothing yields a query that matches nothing on its
    own -- an empty subquery -- rather than a special case here. That keeps
    the rule "a filter we cannot honour must not widen into the unfiltered
    list" in one place: the caller rejects unrecognised states before this
    runs, so anything reaching here is a state we do model, and an empty
    result is then the honest answer.
    """
    from adapters import reader_api

    for state in include_states:
        column = reader_api.TAG_STATE_COLUMNS[state]
        query = query.where(
            ItemRow.id.in_(
                select(ReadStateRow.item_id).where(
                    getattr(ReadStateRow, column).is_(True),
                    ReadStateRow.tenant_id == tenant_id,
                )
            )
        )
    for state in exclude_states:
        column = reader_api.TAG_STATE_COLUMNS[state]
        query = query.where(
            ItemRow.id.not_in(
                select(ReadStateRow.item_id).where(
                    getattr(ReadStateRow, column).is_(True),
                    ReadStateRow.tenant_id == tenant_id,
                )
            )
        )
    return query


def upsert_reader_credential(
    session: Session, *, username: str, password_hash: str, at: datetime
) -> None:
    """Store or refresh the Reader API login hash.

    The plaintext password is never passed here; it lives in the environment
    only, and what is stored is a scrypt hash (ADR 0005).
    """
    stamp = timestamp(at)
    row = session.get(ReaderCredential, username)
    if row is None:
        session.add(
            ReaderCredential(
                username=username,
                password_hash=password_hash,
                created_at=stamp,
                updated_at=stamp,
            )
        )
    else:
        row.password_hash = password_hash
        row.updated_at = stamp


def reader_credential(session: Session, username: str) -> str | None:
    """The stored password hash for `username`, or None if unset."""
    row = session.get(ReaderCredential, username)
    return row.password_hash if row is not None else None


def resolve_reader_ids(session: Session, external_ids: list[int]) -> dict[int, str]:
    """Map external Reader API ids back to internal item ids.

    The external id is the leading hex digits of the internal id, so the
    reverse lookup is a prefix match. `substr` is used rather than LIKE
    because it is exact and does not depend on the column's collation or on
    SQLite's case_sensitive_like setting.
    """
    from adapters import reader_api

    if not external_ids:
        return {}

    resolved: dict[int, str] = {}
    for external in set(external_ids):
        prefix = reader_api.reader_id_hex(external)
        found = (
            session.execute(
                select(ItemRow.id).where(
                    func.substr(ItemRow.id, 1, len(prefix)) == prefix
                )
            )
            .scalars()
            .first()
        )
        if found is not None:
            resolved[external] = found
    return resolved


def read_states_for(
    session: Session, item_ids: list[str], *, tenant_id: str
) -> dict[str, ReadStateDomain]:
    """Consumption state for the given items, keyed by item id.

    Items with no row are absent from the result, which callers must read as
    "unread and unstarred" rather than as missing data.
    """
    if not item_ids:
        return {}
    rows = session.execute(
        select(ReadStateRow).where(
            ReadStateRow.tenant_id == tenant_id,
            ReadStateRow.item_id.in_(item_ids),
        )
    ).scalars()
    return {row.item_id: _read_state_to_domain(row) for row in rows}


def apply_read_state(
    session: Session,
    *,
    item_id: str,
    change: TagChange,
    at: datetime,
    tenant_id: str = SINGLE_TENANT,
) -> ReadStateDomain:
    """Change one item's consumption state, through the rule that defines it.

    The domain merge decides what the new state *is* (`core.reader_state`:
    last-write-wins per flag, and a field the caller did not mention keeps its
    value), and this is the one place that reads the current row, merges and
    writes it back. The Reader compatibility layer and
    `PATCH /v1/items/{id}/read-state` both come through here, which is what keeps
    "mark this read" meaning one thing in Google Reader's vocabulary and in
    first-class terms (issue #202).

    The bulk path is deliberately not this: `mark_all_read` writes the same rule
    set-based, one statement for a whole scope, because a library-wide
    "mark all as read" is thousands of items and looping them would be a request
    per item.
    """
    current = read_states_for(session, [item_id], tenant_id=tenant_id).get(item_id)
    merged = merge_state(item_id=item_id, current=current, change=change, at=at)
    upsert_read_state(session, merged, tenant_id=tenant_id)
    return merged


def upsert_read_state(
    session: Session, state: ReadStateDomain, *, tenant_id: str
) -> None:
    """Write consumption state, last write wins.

    The timestamp comes from the state object, which core set from the server
    clock — never from the client.

    A single atomic INSERT ... ON CONFLICT DO UPDATE, not a read-modify-write.
    The read-modify-write version lost to its own concurrency: the INSERT is
    pending until commit, and a concurrent request that commits the same
    (item, tenant) in that window turns the first request's next query into an
    autoflush that violates the composite primary key -- the batch fails with
    a 500 even though "both wrote, the later one wins" was the whole contract.
    The atomic form resolves the race inside the statement: whichever row is
    already there gets updated, whichever request commits second wins.
    """
    stamp = timestamp(state.updated_at or datetime.now(UTC))
    session.execute(
        sqlite_insert(ReadStateRow)
        .values(
            item_id=state.item_id,
            tenant_id=tenant_id,
            is_read=state.is_read,
            is_starred=state.is_starred,
            updated_at=stamp,
        )
        .on_conflict_do_update(
            index_elements=[ReadStateRow.item_id, ReadStateRow.tenant_id],
            set_={
                "is_read": state.is_read,
                "is_starred": state.is_starred,
                "updated_at": stamp,
            },
        )
    )


def mark_all_read(
    session: Session,
    *,
    tenant_id: str,
    before: datetime,
    subscription_id: str | None = None,
    folder: str | None = None,
) -> int:
    """Mark items published before `before` as read.

    Only touches items that are unread, so a repeated call is a no-op rather
    than repeatedly rewriting rows and bumping their timestamps.

    `subscription_id` and `folder` narrow the sweep to one channel or one
    folder. At most one may be given: a caller that can name both has misparsed
    the stream, and the safe answer is to refuse rather than intersect.
    """
    if subscription_id is not None and folder is not None:
        raise ValueError("mark_all_read takes a subscription or a folder, not both")
    cutoff = timestamp(before)
    existing = select(ReadStateRow.item_id).where(
        ReadStateRow.tenant_id == tenant_id, ReadStateRow.is_read.is_(True)
    )

    candidates = select(ItemRow.id).where(
        visible_item_rows(),
        # An item with no publication date is still in the stream and must
        # still be marked; excluding it would leave undated items permanently
        # unread no matter how often a client asks.
        or_(ItemRow.published_at.is_(None), ItemRow.published_at <= cutoff),
        ItemRow.id.not_in(existing),
    )
    if subscription_id:
        origin_id = origin_of_channel(
            session, channel_id=subscription_id, tenant_id=tenant_id
        )
        if origin_id is None:
            # An unknown channel marks nothing. Sweeping the library because a
            # client named a channel we do not have is the wrong answer that
            # looks like a lot of work.
            return 0
        candidates = candidates.where(ItemRow.origin_id == origin_id)
    if folder is not None:
        # A folder no subscription carries is an empty scope, not the whole
        # library: the same rule the folder *read* path applies.
        candidates = candidates.where(
            ItemRow.origin_id.in_(
                select(SubscriptionRow.origin_id).where(
                    SubscriptionRow.folder == folder,
                    SubscriptionRow.tenant_id == tenant_id,
                )
            )
        )

    ids = list(session.execute(candidates).scalars())
    if not ids:
        return 0

    now = timestamp(datetime.now(UTC))
    # One bulk upsert rather than a loop of read-modify-writes (see
    # upsert_read_state for why read-modify-write loses to concurrency). The
    # candidates query already excludes read items, so the conflict branch
    # fires only for a row that exists *unread* -- an item starred-but-unread,
    # or one a client marked unread after an earlier mark-all. Those keep
    # their star and gain is_read, exactly as the per-row loop used to.
    session.execute(
        sqlite_insert(ReadStateRow)
        .values(
            [
                {
                    "item_id": item_id,
                    "tenant_id": tenant_id,
                    "is_read": True,
                    "is_starred": False,
                    "updated_at": now,
                }
                for item_id in ids
            ]
        )
        .on_conflict_do_update(
            index_elements=[ReadStateRow.item_id, ReadStateRow.tenant_id],
            # is_starred is deliberately absent: marking read is not a request
            # to unstar, same as the loop it replaces.
            set_={"is_read": True, "updated_at": now},
        )
    )
    return len(ids)


def _read_state_to_domain(row: ReadStateRow) -> ReadStateDomain:
    return ReadStateDomain(
        item_id=row.item_id,
        is_read=bool(row.is_read),
        is_starred=bool(row.is_starred),
        updated_at=_parse(row.updated_at),
    )
