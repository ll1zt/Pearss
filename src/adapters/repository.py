"""Item and RawFetch persistence.

The repository applies the dedupe rule that core decides: core is pure and
holds no database, so it returns a decision and this module executes it. That
split is what lets the dedupe rule be tested without a database while keeping
the write path in one place.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, cast

from sqlalchemy import CursorResult, Select, and_, func, or_, select, update
from sqlalchemy.orm import Session

from adapters.correlation import current
from adapters.db_models import IdempotencyRecord as IdempotencyRow
from adapters.db_models import Item as ItemRow
from adapters.db_models import Job as JobRow
from adapters.db_models import JobRun as JobRunRow
from adapters.db_models import RawFetch as RawFetchRow
from adapters.db_models import ReaderCredential
from adapters.db_models import ReadState as ReadStateRow
from adapters.db_models import Source as SourceRow
from adapters.raw_payload import ConditionalState
from core.enums import (
    ErrorClass,
    ItemEvent,
    JobRunStatus,
    PublishStatus,
    SourceStatus,
)
from core.item_state import TransitionContext
from core.item_state import transition as item_transition
from core.job_state import JobRunContext, JobRunEvent, transition
from core.models import Item
from core.normalize import DedupeAction, decide_dedupe, source_display_label
from core.paging import MAX_RESUME_ROUNDS
from core.reader_state import ReadState as ReadStateDomain
from core.scheduling import observed_gaps

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class StoreOutcome:
    """What a store attempt did."""

    action: DedupeAction
    item: Item
    # Set when the decision was to skip or suppress, naming the existing row.
    matched_id: str | None = None
    reason: str = ""


# Sentinel for an absent publication time. Chosen so that COALESCE against it
# puts undated items last under a descending sort.
EPOCH_FLOOR = "0000-01-01T00:00:00.000000Z"


def _timestamp(moment: datetime) -> str:
    """Store timestamps as fixed-width RFC 3339 UTC text.

    Fixed width is the point, not cosmetics. These values are compared as text
    by the database — every ORDER BY and every keyset cursor relies on it — and
    `isoformat()` omits the fractional part entirely when microseconds happen to
    be zero. A column mixing `...T00:00:00Z` with `...T00:00:00.500000Z` sorts
    `00.5` *before* `00`, because '.' (0x2E) is less than 'Z' (0x5A), so half a
    second would order before the top of the second it falls inside.

    Always emitting six fractional digits keeps lexicographic order equal to
    chronological order, which is what the query API's pagination assumes.
    """
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _optional_timestamp(moment: datetime | None) -> str | None:
    return None if moment is None else _timestamp(moment)


def record_raw_fetch(
    session: Session,
    *,
    fetch_id: str,
    source_id: str,
    status: str,
    correlation_id: str,
    payload_ref: str | None = None,
    content_hash: str | None = None,
    http_status: int | None = None,
    etag: str | None = None,
    last_modified: str | None = None,
    error_class: ErrorClass | None = None,
    fetched_at: datetime | None = None,
) -> None:
    """Persist one fetch attempt.

    Always inserts: a retry creates a new RawFetch rather than overwriting the
    previous one, so the first failure stays available for diagnosis.
    """
    session.add(
        RawFetchRow(
            id=fetch_id,
            source_id=source_id,
            payload_ref=payload_ref,
            content_hash=content_hash,
            http_status=http_status,
            etag=etag,
            last_modified=last_modified,
            status=status,
            error_class=error_class.value if error_class else None,
            fetched_at=_timestamp(fetched_at or datetime.now(UTC)),
            correlation_id=correlation_id,
        )
    )


def existing_items_for_source(session: Session, source_id: str) -> list[Item]:
    """Load the items a dedupe decision needs for a source.

    Scoped to the source to keep the comparison set small; cross-source
    duplicates are found by content_hash lookup instead, which the database
    can serve without loading every row.
    """
    rows = session.execute(
        select(ItemRow).where(ItemRow.source_id == source_id)
    ).scalars()
    return [row_to_item(row) for row in rows]


def find_by_content_hash(session: Session, content_hash: str) -> list[Item]:
    """Every item with this content hash, across all sources."""
    rows = session.execute(
        select(ItemRow).where(ItemRow.content_hash == content_hash)
    ).scalars()
    return [row_to_item(row) for row in rows]


def find_by_identity(session: Session, *, item_id: str, guid: str) -> list[Item]:
    """Items that are this same entry, by id or guid.

    Both are checked because the dedupe rule compares either, and a row written
    by an earlier version of the derivation could match one and not the other.
    """
    rows = session.execute(
        select(ItemRow).where(or_(ItemRow.id == item_id, ItemRow.guid == guid))
    ).scalars()
    return [row_to_item(row) for row in rows]


def store_item(session: Session, candidate: Item) -> StoreOutcome:
    """Persist `candidate`, applying the dedupe rule.

    The comparison set is the union of "same entry" and "same content": the
    first catches an article whose body changed between fetches, the second
    catches the same article arriving from another source. Neither alone is
    enough, and content-only matching is what previously let a reworded article
    fall through to an INSERT that collided on the frozen guid.
    """
    existing = find_by_identity(session, item_id=candidate.id, guid=candidate.guid)
    for other in find_by_content_hash(session, candidate.content_hash):
        if all(seen.id != other.id for seen in existing):
            existing.append(other)
    decision = decide_dedupe(candidate, existing)

    if decision.action in (DedupeAction.INSERT, DedupeAction.INSERT_RELATED):
        _insert(session, candidate)
        if decision.action is DedupeAction.INSERT_RELATED:
            # Stored, but the relationship to its twin is recorded rather than
            # left implicit; a future migration can promote this to a relation
            # table without changing the decision logic.
            LOGGER.info(
                "related item stored: %s duplicates %s",
                candidate.id,
                decision.matched_id,
            )
    elif decision.action is DedupeAction.UPDATE_PENDING:
        _update_pending(session, candidate, matched_id=decision.matched_id or "")
    else:
        if decision.action is DedupeAction.SKIP_PUBLISHED_CONTENT_CHANGED:
            # Worth a warning rather than a debug line: the article a reader
            # sees will differ from the source's current text, and nothing
            # will fix that by itself.
            LOGGER.warning(
                "content changed after publication, keeping the published version: %s",
                candidate.id,
            )
        else:
            LOGGER.info(
                "not stored (%s): %s -> %s",
                decision.action.value,
                candidate.id,
                decision.matched_id,
            )

    return StoreOutcome(
        action=decision.action,
        item=candidate,
        matched_id=decision.matched_id,
        reason=decision.reason,
    )


def _update_pending(session: Session, candidate: Item, *, matched_id: str) -> None:
    """Replace a pending row's content with the freshly fetched version.

    Safe only because nothing has seen it yet: the gate is what makes an
    in-place content change permissible at all.
    """
    row = session.get(ItemRow, matched_id)
    if row is None:  # pragma: no cover - the lookup above found it
        return
    row.title = candidate.title
    row.url = candidate.url
    row.content_hash = candidate.content_hash
    row.content_ref = candidate.content_ref
    row.published_at = _optional_timestamp(candidate.published_at)
    row.updated_at = _timestamp(datetime.now(UTC))


def _insert(session: Session, item: Item) -> None:
    now = _timestamp(datetime.now(UTC))
    session.add(
        ItemRow(
            id=item.id,
            source_id=item.source_id,
            guid=item.guid,
            content_hash=item.content_hash,
            title=item.title,
            url=item.url,
            published_at=_optional_timestamp(item.published_at),
            content_ref=item.content_ref,
            publish_status=item.publish_status.value,
            published_to_feed_at=_optional_timestamp(item.published_to_feed_at),
            tenant_id=item.tenant_id,
            created_at=_timestamp(item.created_at) if item.created_at else now,
            updated_at=_timestamp(item.updated_at) if item.updated_at else now,
        )
    )


# How many items one RSS render carries. A feed is a window onto recent
# content, not an archive: an unbounded render reached 5.7 MB and 4,677 items,
# each of which also pulled its body out of the file store, and a client
# polling on a timer paid that per poll. Items outside the window stay
# reachable through the query API and the Reader API.
FEED_WINDOW: int = 200


def items_ready_for_feed(session: Session, *, limit: int = FEED_WINDOW) -> list[Item]:
    """Items the gate permits, newest first, within the feed window.

    Pending is excluded in SQL rather than in Python: the gate is a hard
    invariant, and letting a pending row reach memory at all means a future
    change could accidentally render it.
    """
    rows = session.execute(
        select(ItemRow)
        .where(ItemRow.publish_status != PublishStatus.PENDING.value)
        .order_by(ItemRow.published_at.desc().nullslast(), ItemRow.guid)
        .limit(max(1, limit))
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
    ids = [item.id for item in items]
    stamp = _timestamp(at)
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


def visible_ids_for_stream(
    session: Session,
    *,
    stream_id: str,
    tenant_id: str,
    limit: int = 10000,
    offset: int = 0,
) -> list[str]:
    """Item ids for a Reader API stream, newest first.

    Pending items are excluded here, in SQL, for the same reason the RSS path
    excludes them: a client that has seen an item cannot be shown a different
    version of it, so the gate has to apply before anything is emitted.

    A state stream filters on that state. An unrecognised one yields **nothing**:
    falling through to "everything" is what made `.../starred` return the whole
    library, which a client then renders as every item being starred. Serving an
    empty list for a filter we do not implement is an honest gap; serving the
    unfiltered list is a wrong answer that looks like data.
    """
    from adapters import reader_api

    query = select(ItemRow.id).where(
        ItemRow.publish_status != PublishStatus.PENDING.value
    )

    source_id = reader_api.parse_feed_stream_id(stream_id)
    if source_id is not None:
        query = query.where(ItemRow.source_id == source_id)
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
        elif reader_api.label_stream_name(stream_id) == "All":
            # The one folder we advertise. Every source lives in it, so it is
            # the reading list; any other label is a folder that does not exist
            # yet and must not silently read as "everything".
            pass
        elif state in {"broadcast", "verified"}:
            # Shared/verified streams: we record neither, so there is nothing
            # to list. Distinct from the unknown-state branch only in intent.
            return []
        else:
            LOGGER.info("unhandled Reader stream %r; returning no items", stream_id)
            return []

    return list(
        session.execute(
            query.order_by(ItemRow.published_at.desc().nullslast(), ItemRow.id)
            .limit(limit)
            .offset(offset)
        ).scalars()
    )


def upsert_reader_credential(
    session: Session, *, username: str, password_hash: str, at: datetime
) -> None:
    """Store or refresh the Reader API login hash.

    The plaintext password is never passed here; it lives in the environment
    only, and what is stored is a scrypt hash (ADR 0005).
    """
    stamp = _timestamp(at)
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


def upsert_read_state(
    session: Session, state: ReadStateDomain, *, tenant_id: str
) -> None:
    """Write consumption state, last write wins.

    The timestamp comes from the state object, which core set from the server
    clock — never from the client.
    """
    stamp = _timestamp(state.updated_at or datetime.now(UTC))
    # Read-modify-write rather than an upsert: SQLAlchemy's typed Insert does
    # not expose on_conflict_do_update, and the dialect-specific form would
    # not carry over to Postgres in P3. The composite primary key prevents a
    # concurrent duplicate row from being created either way.
    row = session.get(ReadStateRow, (state.item_id, tenant_id))
    if row is None:
        session.add(
            ReadStateRow(
                item_id=state.item_id,
                tenant_id=tenant_id,
                is_read=state.is_read,
                is_starred=state.is_starred,
                updated_at=stamp,
            )
        )
    else:
        row.is_read = state.is_read
        row.is_starred = state.is_starred
        row.updated_at = stamp


def mark_all_read(
    session: Session,
    *,
    tenant_id: str,
    before: datetime,
    source_id: str | None = None,
) -> int:
    """Mark items published before `before` as read.

    Only touches items that are unread, so a repeated call is a no-op rather
    than repeatedly rewriting rows and bumping their timestamps.
    """
    cutoff = _timestamp(before)
    existing = select(ReadStateRow.item_id).where(
        ReadStateRow.tenant_id == tenant_id, ReadStateRow.is_read.is_(True)
    )

    candidates = select(ItemRow.id).where(
        ItemRow.publish_status != PublishStatus.PENDING.value,
        # An item with no publication date is still in the stream and must
        # still be marked; excluding it would leave undated items permanently
        # unread no matter how often a client asks.
        or_(ItemRow.published_at.is_(None), ItemRow.published_at <= cutoff),
        ItemRow.id.not_in(existing),
    )
    if source_id:
        candidates = candidates.where(ItemRow.source_id == source_id)

    ids = list(session.execute(candidates).scalars())
    if not ids:
        return 0

    now = _timestamp(datetime.now(UTC))
    for item_id in ids:
        row = session.get(ReadStateRow, (item_id, tenant_id))
        if row is None:
            session.add(
                ReadStateRow(
                    item_id=item_id,
                    tenant_id=tenant_id,
                    is_read=True,
                    is_starred=False,
                    updated_at=now,
                )
            )
        else:
            # Preserve an existing star: marking read is not a request to
            # unstar. Only is_read and the timestamp move.
            row.is_read = True
            row.updated_at = now
    return len(ids)


def _read_state_to_domain(row: ReadStateRow) -> ReadStateDomain:
    return ReadStateDomain(
        item_id=row.item_id,
        is_read=bool(row.is_read),
        is_starred=bool(row.is_starred),
        updated_at=_parse(row.updated_at),
    )


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------


def derive_source_id(*, tenant_id: str, source_type: str, canonical_url: str) -> str:
    """Deterministic source id from the identity the uniqueness constraint uses.

    Deriving rather than generating randomly means "add this feed" is idempotent
    on its own terms: the same URL produces the same id, so a retried request
    cannot create a second source even before the Idempotency-Key is consulted.
    """
    payload = "\x1f".join(("src1", tenant_id, source_type, canonical_url))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def create_source(
    session: Session,
    *,
    source_type: str,
    url: str,
    canonical_url: str,
    schedule_policy: str,
    tenant_id: str,
    trust_level: str = "standard",
    parser_plugin_id: str | None = None,
    at: str,
) -> SourceRow:
    """Insert a source, or return the existing one for this identity.

    Returning the existing row rather than raising is what makes a retried
    create answer with the same body instead of a 409: the caller's intent was
    "this source should exist", and after the first request it does.

    A soft-deleted row is reactivated rather than returned as-is. Answering
    201 with a `deleted` source would claim success for a subscription that
    will never be collected again. Reactivation applies the request's url and
    policy, because the operator is subscribing anew; an existing live source
    keeps its stored configuration, which is what makes a retried create a
    no-op rather than a policy change.
    """
    source_id = derive_source_id(
        tenant_id=tenant_id, source_type=source_type, canonical_url=canonical_url
    )
    existing = session.get(SourceRow, source_id)
    if existing is not None:
        if existing.status == SourceStatus.DELETED.value:
            existing.status = SourceStatus.ACTIVE.value
            existing.url = url
            existing.schedule_policy = schedule_policy
            existing.updated_at = at
            session.flush()
        return existing

    row = SourceRow(
        id=source_id,
        type=source_type,
        url=url,
        canonical_url=canonical_url,
        parser_plugin_id=parser_plugin_id,
        schedule_policy=schedule_policy,
        status=SourceStatus.ACTIVE.value,
        trust_level=trust_level,
        tenant_id=tenant_id,
        created_at=at,
        updated_at=at,
    )
    session.add(row)
    session.flush()
    return row


def get_source(session: Session, source_id: str) -> SourceRow | None:
    return session.get(SourceRow, source_id)


def list_sources(
    session: Session, *, tenant_id: str, limit: int, offset: int = 0
) -> list[SourceRow]:
    """One page of sources, oldest first so a cursor stays stable under inserts."""
    return list(
        session.execute(
            select(SourceRow)
            .where(SourceRow.tenant_id == tenant_id)
            .order_by(SourceRow.created_at, SourceRow.id)
            .limit(limit + 1)
            .offset(offset)
        ).scalars()
    )


# --------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------


def find_idempotent(
    session: Session, *, actor: str, endpoint: str, key: str, now: str
) -> IdempotencyRow | None:
    """A stored result for this write, if present and unexpired.

    Expired rows read as absent rather than being swept here: clearing them on
    the read path would make every request pay for maintenance, and a stale row
    is harmless because the key is unique per actor and endpoint.
    """
    row = session.get(IdempotencyRow, (actor, endpoint, key))
    if row is None:
        return None
    if row.expires_at < now:
        return None
    return row


def store_idempotent(
    session: Session,
    *,
    actor: str,
    endpoint: str,
    key: str,
    status_code: int,
    response_body: str,
    created_at: str,
    expires_at: str,
) -> None:
    row = session.get(IdempotencyRow, (actor, endpoint, key))
    if row is None:
        session.add(
            IdempotencyRow(
                actor=actor,
                endpoint=endpoint,
                key=key,
                status_code=status_code,
                response_body=response_body,
                created_at=created_at,
                expires_at=expires_at,
            )
        )
    else:
        # Overwrite rather than raise: the first response was already delivered,
        # and a 500 here would surface a normal retry race as a server fault.
        row.status_code = status_code
        row.response_body = response_body
        row.created_at = created_at
        row.expires_at = expires_at


def last_conditional_state(
    session: Session, *, source_id: str
) -> ConditionalState | None:
    """Conditional-request state from the most recent fetch of a source.

    ETag and Last-Modified live on RawFetch, not Source, per
    docs/domain/data-model.md: they describe what one response said, not what
    the source is, and a source can in principle be fetched by more than one
    mechanism. Returns None when nothing has been recorded, which is the same
    as "make an unconditional request".
    """

    row = (
        session.execute(
            select(RawFetchRow)
            .where(RawFetchRow.source_id == source_id)
            .order_by(RawFetchRow.fetched_at.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )
    if row is None or not (row.etag or row.last_modified):
        return None
    return ConditionalState(etag=row.etag, last_modified=row.last_modified)


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

    query = select(ItemRow).where(
        ItemRow.publish_status != PublishStatus.PENDING.value,
        ItemRow.tenant_id == tenant_id,
    )

    if filter.source_id:
        query = query.where(ItemRow.source_id == filter.source_id)
    if filter.publish_status:
        query = query.where(ItemRow.publish_status == filter.publish_status)
    if filter.query:
        query = query.where(
            ItemRow.title.like(f"%{escape_like(filter.query)}%", escape="\\")
        )
    if filter.since:
        query = query.where(sort_column >= filter.since)
    if filter.until:
        query = query.where(sort_column <= filter.until)

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
    row.updated_at = _timestamp(datetime.now(UTC))


# --------------------------------------------------------------------------
# Jobs and job runs
# --------------------------------------------------------------------------

COLLECT_JOB_TYPE = "collect"


def ensure_collect_job(
    session: Session, *, source_id: str, tenant_id: str, at: str
) -> str:
    """Create the collection job for a source, or return the existing id.

    One job per source: the job is the durable declaration that this source is
    collected, while a job_run is one execution of that declaration. Keeping
    them separate is what lets a future job type (feed regeneration, cleanup)
    reuse the run machinery.

    Idempotent because it is called from source creation, which is itself
    idempotent.
    """
    existing = (
        session.execute(
            select(JobRow.id).where(
                JobRow.config == source_id, JobRow.type == COLLECT_JOB_TYPE
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        return existing

    job_id = f"job-{source_id}"
    session.add(
        JobRow(
            id=job_id,
            type=COLLECT_JOB_TYPE,
            # The schedule is not stored here on purpose: when the next run is
            # due is derived from the source's policy and history, so this row
            # declares *what* is collected, not *when*.
            schedule=None,
            priority=0,
            status="enabled",
            config=source_id,
            created_at=at,
            updated_at=at,
        )
    )
    session.flush()
    return job_id


def job_for_source(session: Session, source_id: str) -> JobRow | None:
    return (
        session.execute(
            select(JobRow).where(
                JobRow.type == COLLECT_JOB_TYPE, JobRow.config == source_id
            )
        )
        .scalars()
        .first()
    )


def create_job_run(
    session: Session,
    *,
    run_id: str,
    job_id: str,
    correlation_id: str,
    retry_of: str | None = None,
    at: str,
) -> str:
    """Open a queued run. A retry creates a new row rather than reopening one.

    Raises IntegrityError when the job already has a queued or running run:
    the unique partial index over active runs is what enforces one collection
    per job, since every caller creates its own run id and cannot be told
    apart by a conditional update on its own row.
    """
    session.add(
        JobRunRow(
            id=run_id,
            job_id=job_id,
            status=JobRunStatus.QUEUED.value,
            metrics="{}",
            correlation_id=correlation_id,
            retry_of=retry_of,
            started_at=None,
            finished_at=None,
        )
    )
    session.flush()
    return run_id


def claim_job_run(session: Session, *, run_id: str, at: str) -> bool:
    """Move queued -> running with a conditional update.

    Cross-callers mutual exclusion lives in the schema (the unique partial
    index over active runs per job), not here: each caller creates its own
    run id, so this update's condition guards the row's own lifecycle rather
    than arbitrating between callers.
    """
    result = cast(
        "CursorResult[Any]",
        session.execute(
            update(JobRunRow)
            .where(
                JobRunRow.id == run_id,
                JobRunRow.status == JobRunStatus.QUEUED.value,
            )
            .values(status=JobRunStatus.RUNNING.value, started_at=at)
        ),
    )
    return (result.rowcount or 0) == 1


def finish_job_run(
    session: Session,
    *,
    run_id: str,
    event: JobRunEvent,
    correlation_id: str,
    error_class: ErrorClass | None = None,
    metrics: dict[str, object] | None = None,
    at: str,
) -> JobRunStatus:
    """Close a run, routing the transition through the core state machine.

    The status is never assigned directly: the machine is what guarantees a
    failed run carries an error_class and a succeeded one does not, which is
    what keeps the taxonomy queryable and the backoff computable.

    The conditional update also makes the terminal write single-shot — a run
    already finished by another worker cannot be overwritten.
    """
    row = session.get(JobRunRow, run_id)
    if row is None:
        raise ValueError(f"no job run with id {run_id!r}")

    current = JobRunStatus(row.status)
    result = transition(current, event, JobRunContext(error_class=error_class))

    written = cast(
        "CursorResult[Any]",
        session.execute(
            update(JobRunRow)
            .where(
                JobRunRow.id == run_id,
                JobRunRow.status == current.value,
            )
            .values(
                status=result.status.value,
                error_class=result.error_class.value if result.error_class else None,
                metrics=json.dumps(metrics or {}, sort_keys=True),
                finished_at=at,
            )
        ),
    )
    if (written.rowcount or 0) == 0:
        LOGGER.warning("job run %s was already terminalised by another worker", run_id)
    del correlation_id  # already fixed at creation; kept for call-site clarity
    return result.status


# --------------------------------------------------------------------------
# Inputs for the derived schedule
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class PagingState:
    """Where the last walk of this source's origin left off."""

    seen_frontier: int | None = None
    resume_cursor: str | None = None
    resume_rounds: int = 0


def paging_state(session: Session, *, source_id: str) -> PagingState:
    row = session.get(SourceRow, source_id)
    if row is None:
        return PagingState()
    return PagingState(
        seen_frontier=row.seen_frontier,
        resume_cursor=row.resume_cursor,
        resume_rounds=row.resume_rounds,
    )


def save_paging_state(
    session: Session,
    *,
    source_id: str,
    newest_id: int | None,
    resume_cursor: str | None,
) -> None:
    """Persist where this walk ended.

    The frontier only moves forward. A re-walk of old history reports ids
    below it, and letting those win would drag the anchor backwards so every
    later cycle re-reads content it already has.

    `resume_rounds` counts consecutive cycles that ended still holding a
    resume. It resets when the resume clears, which is what bounds how far a
    backlog can slide into the past.
    """
    row = session.get(SourceRow, source_id)
    if row is None:
        return
    if newest_id is not None and (
        row.seen_frontier is None or newest_id > row.seen_frontier
    ):
        row.seen_frontier = newest_id

    # The count tracks "this cycle still ended with unfinished history", not
    # "the same cursor as last time". A productive backfill produces a *new,
    # deeper* cursor every cycle, so keying the count on cursor equality would
    # pin it at 1 forever and the bound would never engage in exactly the case
    # it exists for: a Following timeline with more history than the per-cycle
    # budget can ever reach.
    if resume_cursor:
        row.resume_cursor = resume_cursor
        row.resume_rounds = (
            1 if row.resume_rounds >= MAX_RESUME_ROUNDS else row.resume_rounds + 1
        )
    else:
        row.resume_cursor = None
        row.resume_rounds = 0
    row.updated_at = _timestamp(datetime.now(UTC))


def source_display_labels(session: Session, source_ids: list[str]) -> dict[str, str]:
    """Display labels for the given sources, keyed by source id.

    Batched because a contents request can name a hundred items across several
    sources, and resolving a name per item would turn one page into one hundred
    queries. Falls back through core.source_display_label so a source never
    collected still gets something better than its hex id.
    """
    wanted = [source_id for source_id in set(source_ids) if source_id]
    if not wanted:
        return {}
    rows = session.execute(
        select(
            SourceRow.id, SourceRow.title, SourceRow.url, SourceRow.canonical_url
        ).where(SourceRow.id.in_(wanted))
    ).all()
    return {
        row.id: source_display_label(
            title=row.title,
            url=row.url,
            canonical_url=row.canonical_url,
            source_id=row.id,
        )
        for row in rows
    }


def set_source_title_if_absent(
    session: Session, *, source_id: str, title: str | None
) -> bool:
    """Record the name the origin gives itself, when we have none.

    Conditional on purpose. The title arrives from the feed or page, so an
    operator who renamed a source through the API would otherwise have that
    choice overwritten by the next collection; and a source whose origin
    changes its name keeps the one clients already recognise.

    Returns whether the row changed, so the caller can decide whether a commit
    is owed.
    """
    cleaned = (title or "").strip()
    if not cleaned:
        return False
    result = cast(
        "CursorResult[Any]",
        session.execute(
            update(SourceRow)
            .where(
                SourceRow.id == source_id,
                or_(SourceRow.title.is_(None), SourceRow.title == ""),
            )
            .values(title=cleaned, updated_at=_timestamp(datetime.now(UTC)))
        ),
    )
    return (result.rowcount or 0) == 1


def update_gaps_for_source(
    session: Session, *, source_id: str, limit: int = 40
) -> list[float]:
    """Seconds between this source's recent publication times.

    Section 4 says to derive the update cadence from RawFetch history, but a
    RawFetch records when *we* fetched, not when the source *published*. Using
    the items' own publication timestamps measures the thing the policy
    actually wants — how often the source produces content — and is not
    distorted by our own fetch cadence, which is the feedback loop a
    fetch-derived interval would create.

    Returns the most recent `limit` gaps, newest first, or an empty list when
    there are too few items to measure anything.
    """
    stamps = list(
        session.execute(
            select(ItemRow.published_at)
            .where(
                ItemRow.source_id == source_id,
                ItemRow.published_at.is_not(None),
            )
            .order_by(ItemRow.published_at.desc())
            .limit(limit)
        ).scalars()
    )
    moments = [parsed for parsed in (_parse(value) for value in stamps) if parsed]
    if len(moments) < 2:
        return []
    ordered = sorted(moments)
    return observed_gaps([moment.timestamp() for moment in ordered])


def reap_orphaned_runs(session: Session, *, older_than: datetime) -> int:
    """Time out runs left `running` by a previous process.

    A run is claimed, then collection happens, then it is closed. If the
    process dies in the middle the row stays `running` forever: nothing will
    ever close it, and the run history silently accumulates zombies that look
    like work still in progress.

    Timed out rather than deleted, because the state machine treats a timeout
    as a terminal outcome and the row is real evidence that an attempt started
    and never came back.
    """
    cutoff = _timestamp(older_than)
    result = cast(
        "CursorResult[Any]",
        session.execute(
            update(JobRunRow)
            .where(
                JobRunRow.status == JobRunStatus.RUNNING.value,
                or_(
                    JobRunRow.started_at.is_(None),
                    JobRunRow.started_at < cutoff,
                ),
            )
            .values(
                status=JobRunStatus.TIMED_OUT.value,
                finished_at=_timestamp(datetime.now(UTC)),
                error_class=ErrorClass.NETWORK.value,
            )
        ),
    )
    return int(result.rowcount or 0)


def last_collect_at(session: Session, *, job_id: str) -> datetime | None:
    """When this source's collection job last finished, any outcome.

    Failures count: backing off requires knowing the last attempt happened,
    and ignoring failed runs would retry a blocked source on its normal
    cadence.
    """
    finished = (
        session.execute(
            select(JobRunRow.finished_at)
            .where(
                JobRunRow.job_id == job_id,
                JobRunRow.finished_at.is_not(None),
            )
            .order_by(JobRunRow.finished_at.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )
    return _parse(finished) if finished else None


def consecutive_failures(session: Session, *, job_id: str, limit: int = 20) -> int:
    """How many most recent runs failed, stopping at the first success.

    Walks back from the newest run and stops early: a source that recovered
    after three failures should not keep its backoff, so counting all failures
    ever would be wrong.
    """
    statuses = session.execute(
        select(JobRunRow.status)
        .where(JobRunRow.job_id == job_id)
        .order_by(JobRunRow.started_at.desc().nullslast())
        .limit(limit)
    ).scalars()
    count = 0
    for status in statuses:
        if status == JobRunStatus.SUCCEEDED.value:
            break
        if status in (
            JobRunStatus.FAILED.value,
            JobRunStatus.TIMED_OUT.value,
        ):
            count += 1
        # A queued or running run is neither a failure nor a recovery; skip it
        # rather than stopping, since it has not resolved.
    return count


def collectable_sources(session: Session, *, tenant_id: str) -> list[tuple[str, str]]:
    """(source_id, schedule_policy_json) for sources we should consider.

    Only `active`: a paused or disabled source is not collected, and that is
    the operator's explicit decision rather than something the scheduler
    should second-guess.
    """
    rows = session.execute(
        select(SourceRow.id, SourceRow.schedule_policy).where(
            SourceRow.tenant_id == tenant_id,
            SourceRow.status == SourceStatus.ACTIVE.value,
        )
    ).all()
    return [(str(row[0]), str(row[1])) for row in rows]


# --------------------------------------------------------------------------
# Job and run inspection, and the system snapshot
# --------------------------------------------------------------------------


def list_jobs(session: Session) -> list[dict[str, object]]:
    """Every declared job, with its source and last run summary.

    Ordered by id rather than recency: this feeds a listing a client pages
    through, and an unstable order makes clients re-announce entries.
    """
    rows = session.execute(select(JobRow).order_by(JobRow.id)).scalars()
    jobs: list[dict[str, object]] = []
    for row in rows:
        last = (
            session.execute(
                select(JobRunRow)
                .where(JobRunRow.job_id == row.id)
                .order_by(JobRunRow.started_at.desc().nullslast())
                .limit(1)
            )
            .scalars()
            .first()
        )
        jobs.append(
            {
                "id": row.id,
                "type": row.type,
                "source_id": row.config,
                "status": row.status,
                "priority": row.priority,
                "last_run_status": last.status if last is not None else None,
                "last_run_finished_at": last.finished_at if last is not None else None,
            }
        )
    return jobs


def list_job_runs(
    session: Session, *, job_id: str, limit: int = 50
) -> list[dict[str, object]]:
    """Recent runs for one job, newest first."""
    rows = session.execute(
        select(JobRunRow)
        .where(JobRunRow.job_id == job_id)
        .order_by(JobRunRow.started_at.desc().nullslast())
        .limit(limit)
    ).scalars()
    return [
        {
            "id": row.id,
            "job_id": row.job_id,
            "status": row.status,
            "error_class": row.error_class,
            "metrics": json.loads(row.metrics or "{}"),
            "correlation_id": row.correlation_id,
            "retry_of": row.retry_of,
            "started_at": row.started_at,
            "finished_at": row.finished_at,
        }
        for row in rows
    ]


def system_snapshot(session: Session, *, tenant_id: str) -> dict[str, object]:
    """Compact system state, shaped for a conversational dashboard.

    Section 7.7 names the signals that matter: error_class distribution (a
    cluster of structure_changed means the world changed under us), how long
    pending items have been waiting, and fetch success rate. Those are what is
    reported here, rather than a generic row count.
    """

    def counts(
        statement: Select[tuple[Any, int]],
        *,
        every: type[StrEnum] | None = None,
    ) -> dict[str, int]:
        """Tally a grouped `select(value, count())` into a mapping.

        A comprehension rather than `dict(...)`: mypy cannot infer a value type
        from a Row sequence, and the empty-dict annotation it produces poisons
        every downstream use.

        `every` fills in the states that have no rows, so a consumer reads an
        explicit zero instead of a missing key. Omitting them cost us a real
        misreading: `pending` was absent while 4,576 `ready` items were present,
        and a client labelled the whole set "待处理" because the one status that
        would have contradicted that reading was not in the payload.
        """
        tallied = {
            str(key): int(total) for key, total in session.execute(statement).all()
        }
        if every is not None:
            for member in every:
                tallied.setdefault(str(member.value), 0)
        return tallied

    sources = counts(
        select(SourceRow.status, func.count())
        .where(SourceRow.tenant_id == tenant_id)
        .group_by(SourceRow.status),
        every=SourceStatus,
    )
    items = counts(
        select(ItemRow.publish_status, func.count())
        .where(ItemRow.tenant_id == tenant_id)
        .group_by(ItemRow.publish_status),
        every=PublishStatus,
    )
    runs_by_status = counts(
        select(JobRunRow.status, func.count()).group_by(JobRunRow.status),
        every=JobRunStatus,
    )
    # error_class is nullable; the query drops the NULLs, and str() keeps the
    # key type uniform without asserting away the None mypy still sees.
    runs_by_error = counts(
        select(JobRunRow.error_class, func.count())
        .where(JobRunRow.error_class.is_not(None))
        .group_by(JobRunRow.error_class),
        # Every class is listed: "no blocked runs" is a fact worth stating,
        # and a missing key cannot be told apart from a query that forgot.
        every=ErrorClass,
    )
    recent = session.execute(
        select(JobRunRow.status)
        .where(JobRunRow.finished_at.is_not(None))
        .order_by(JobRunRow.finished_at.desc())
        .limit(50)
    ).scalars()
    recent_statuses = list(recent)
    succeeded = sum(1 for status in recent_statuses if status == "succeeded")

    return {
        "sources_by_status": sources,
        "items_by_publish_status": items,
        "runs_by_status": runs_by_status,
        "runs_by_error_class": runs_by_error,
        # A rate over the last 50 finished runs rather than all time: a source
        # that broke an hour ago should move this number immediately.
        "recent_run_success_rate": (
            round(succeeded / len(recent_statuses), 3) if recent_statuses else None
        ),
        "recent_runs_considered": len(recent_statuses),
        "correlation_id": current(),
    }


def count_items(session: Session) -> int:
    return int(session.execute(select(func.count()).select_from(ItemRow)).scalar() or 0)


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
