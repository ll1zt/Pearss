"""Idempotency records and conditional-request state."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from adapters.db_models import IdempotencyRecord as IdempotencyRow
from adapters.db_models import RawFetch as RawFetchRow
from adapters.raw_payload import ConditionalState


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
