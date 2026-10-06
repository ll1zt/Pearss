"""Origin persistence: the fact half of the source/origin split (ADR 0046 D-A).

Stage A: `source` is still what every read path uses. This module is the
write/read pair for the table it will become, and it exists as a module rather
than as three more statements inside `sources.create_source` so that the
dual-write is a call rather than a second copy of the field list -- the copy is
exactly what drifts when one side gains a column.

Nothing here decides policy: scheduling, the pipeline chain and the sidebar
folder belong to `subscriptions`.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from adapters.db_models import Origin as OriginRow

LOGGER = logging.getLogger(__name__)


def create_origin(
    session: Session,
    *,
    origin_id: str,
    source_type: str,
    url: str,
    canonical_url: str,
    tenant_id: str,
    at: str,
    title: str | None = None,
    parser_plugin_id: str | None = None,
    seen_frontier: int | None = None,
    floor_cursor: str | None = None,
) -> OriginRow:
    """Insert an origin, or return the existing row sharing its id.

    Idempotent on the id, not on the URL: the caller derives the id from the
    same identity the uniqueness constraint uses (`derive_source_id`), so "the
    row is already there" and "this is the same origin" are one statement.
    """
    existing = session.get(OriginRow, origin_id)
    if existing is not None:
        LOGGER.debug("origin %s already exists; returning it", origin_id)
        return existing

    row = OriginRow(
        id=origin_id,
        type=source_type,
        url=url,
        canonical_url=canonical_url,
        title=title,
        parser_plugin_id=parser_plugin_id,
        seen_frontier=seen_frontier,
        floor_cursor=floor_cursor,
        tenant_id=tenant_id,
        created_at=at,
        updated_at=at,
    )
    session.add(row)
    session.flush()
    LOGGER.debug("created origin %s (%s)", origin_id, source_type)
    return row


def get_origin(session: Session, origin_id: str) -> OriginRow | None:
    return session.get(OriginRow, origin_id)


def list_origins(
    session: Session, *, tenant_id: str, limit: int, offset: int = 0
) -> list[OriginRow]:
    """One page, oldest first so a cursor stays stable under inserts."""
    return list(
        session.execute(
            select(OriginRow)
            .where(OriginRow.tenant_id == tenant_id)
            .order_by(OriginRow.created_at, OriginRow.id)
            .limit(limit + 1)
            .offset(offset)
        ).scalars()
    )
