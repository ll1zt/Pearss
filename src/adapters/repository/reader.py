"""Reader API persistence: stream visibility, credentials, read state."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from adapters.db_models import Item as ItemRow
from adapters.db_models import ReaderCredential
from adapters.db_models import ReadState as ReadStateRow
from adapters.db_models import Source as SourceRow
from adapters.repository._shared import _parse
from adapters.timestamps import fixed_width_utc as timestamp
from core.enums import (
    PublishStatus,
)
from core.reader_state import ReadState as ReadStateDomain

LOGGER = logging.getLogger(__name__)


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
        elif (label := reader_api.label_stream_name(stream_id)) is not None:
            # A folder stream: items of the sources in that folder. A name no
            # source carries is an empty stream, not "everything" -- serving
            # the unfiltered list for a filter with no matches is a wrong
            # answer that looks like data.
            query = query.where(
                ItemRow.source_id.in_(
                    select(SourceRow.id).where(
                        SourceRow.folder == label,
                        SourceRow.tenant_id == tenant_id,
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


def upsert_read_state(
    session: Session, state: ReadStateDomain, *, tenant_id: str
) -> None:
    """Write consumption state, last write wins.

    The timestamp comes from the state object, which core set from the server
    clock — never from the client.
    """
    stamp = timestamp(state.updated_at or datetime.now(UTC))
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
    cutoff = timestamp(before)
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

    now = timestamp(datetime.now(UTC))
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
