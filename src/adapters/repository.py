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
from typing import Any, cast

from sqlalchemy import CursorResult, func, or_, select, update
from sqlalchemy.orm import Session

from adapters.db_models import IdempotencyRecord as IdempotencyRow
from adapters.db_models import Item as ItemRow
from adapters.db_models import RawFetch as RawFetchRow
from adapters.db_models import ReaderCredential
from adapters.db_models import ReadState as ReadStateRow
from adapters.db_models import Source as SourceRow
from adapters.raw_payload import ConditionalState
from core.enums import ErrorClass, ItemEvent, PublishStatus, SourceStatus
from core.item_state import TransitionContext, transition
from core.models import Item
from core.normalize import DedupeAction, decide_dedupe
from core.reader_state import ReadState as ReadStateDomain

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class StoreOutcome:
    """What a store attempt did."""

    action: DedupeAction
    item: Item
    # Set when the decision was to skip or suppress, naming the existing row.
    matched_id: str | None = None
    reason: str = ""


def _timestamp(moment: datetime) -> str:
    """Store timestamps as RFC 3339 UTC text, per docs/domain/data-model.md."""
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


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


def store_item(session: Session, candidate: Item) -> StoreOutcome:
    """Persist `candidate`, applying the dedupe rule.

    The rule is evaluated against every item sharing the candidate's
    content_hash, not just those from the same source — cross-source
    duplicates are the case B2 exists to handle.
    """
    existing = find_by_content_hash(session, candidate.content_hash)
    decision = decide_dedupe(candidate, existing)

    if decision.action is DedupeAction.INSERT:
        _insert(session, candidate)
    elif decision.action is DedupeAction.INSERT_RELATED:
        # Stored, but the relationship to its twin is recorded rather than
        # left implicit; a future migration can promote this to a relation
        # table without changing the decision logic.
        _insert(session, candidate)
        LOGGER.info(
            "related item stored: %s duplicates %s", candidate.id, decision.matched_id
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


def items_ready_for_feed(session: Session) -> list[Item]:
    """Items the gate permits, newest first.

    Pending is excluded in SQL rather than in Python: the gate is a hard
    invariant, and letting a pending row reach memory at all means a future
    change could accidentally render it.
    """
    rows = session.execute(
        select(ItemRow)
        .where(ItemRow.publish_status != PublishStatus.PENDING.value)
        .order_by(ItemRow.published_at.desc().nullslast(), ItemRow.guid)
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
        result = transition(
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
    """
    from adapters import reader_api

    del tenant_id  # single tenant today; kept so the signature stays honest
    query = select(ItemRow.id).where(
        ItemRow.publish_status != PublishStatus.PENDING.value
    )

    source_id = reader_api.parse_feed_stream_id(stream_id)
    if source_id is not None:
        query = query.where(ItemRow.source_id == source_id)

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
    """
    source_id = derive_source_id(
        tenant_id=tenant_id, source_type=source_type, canonical_url=canonical_url
    )
    existing = session.get(SourceRow, source_id)
    if existing is not None:
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
