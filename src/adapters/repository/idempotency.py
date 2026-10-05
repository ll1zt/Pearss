"""Idempotency records and conditional-request state."""

from __future__ import annotations

import logging

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from adapters.db_models import IdempotencyRecord as IdempotencyRow
from adapters.db_models import RawFetch as RawFetchRow
from adapters.raw_payload import ConditionalState

LOGGER = logging.getLogger(__name__)


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
    if row.status_code != 0 and row.expires_at < now:
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
    # Compatibility writer for legacy responses: immutable once stored.
    session.execute(
        insert(IdempotencyRow)
        .values(
            actor=actor,
            endpoint=endpoint,
            key=key,
            status_code=status_code,
            response_body=response_body,
            created_at=created_at,
            expires_at=expires_at,
        )
        .on_conflict_do_nothing()
    )


def reserve_idempotent(
    session: Session,
    *,
    actor: str,
    endpoint: str,
    key: str,
    fingerprint: str,
    owner_token: str,
    now: str,
    expires_at: str,
) -> IdempotencyRow:
    """Atomically open the mutation window, or return its existing owner/result.

    Only completed expired responses may be replaced. Unknown outcomes never
    expire into permission to execute again: an external effect may have landed.
    """
    statement = insert(IdempotencyRow).values(
        actor=actor,
        endpoint=endpoint,
        key=key,
        fingerprint=fingerprint,
        owner_token=owner_token,
        status_code=0,
        response_body="{}",
        created_at=now,
        expires_at=expires_at,
    )
    session.execute(
        statement.on_conflict_do_update(
            index_elements=["actor", "endpoint", "key"],
            set_={
                name: getattr(statement.excluded, name)
                for name in (
                    "fingerprint",
                    "owner_token",
                    "status_code",
                    "response_body",
                    "created_at",
                    "expires_at",
                )
            },
            where=(IdempotencyRow.status_code != 0) & (IdempotencyRow.expires_at < now),
        )
    )
    row = session.execute(
        select(IdempotencyRow).where(
            IdempotencyRow.actor == actor,
            IdempotencyRow.endpoint == endpoint,
            IdempotencyRow.key == key,
        )
    ).scalar_one()
    if row.owner_token != owner_token:
        # The key already had an owner: this is the conflict path that answers
        # the caller with the stored response instead of running the write
        # again. DEBUG because a retrying client hits it by design (issue
        # #296).
        LOGGER.debug(
            "idempotency key %s reused on %s; returning the stored response",
            key,
            endpoint,
        )
    return row


def complete_idempotent(
    session: Session,
    *,
    actor: str,
    endpoint: str,
    key: str,
    owner_token: str,
    status_code: int,
    response_body: str,
    expires_at: str,
) -> None:
    """Fence completion; a stale executor cannot overwrite a newer response."""
    completed = session.execute(
        update(IdempotencyRow)
        .where(
            IdempotencyRow.actor == actor,
            IdempotencyRow.endpoint == endpoint,
            IdempotencyRow.key == key,
            IdempotencyRow.owner_token == owner_token,
            IdempotencyRow.status_code == 0,
        )
        .values(
            status_code=status_code, response_body=response_body, expires_at=expires_at
        )
        .returning(IdempotencyRow.key)
    ).scalar_one_or_none()
    if completed is None:
        raise RuntimeError("idempotency reservation no longer owned")


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
