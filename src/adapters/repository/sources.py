"""Source persistence."""

from __future__ import annotations

import hashlib

from sqlalchemy import select
from sqlalchemy.orm import Session

from adapters.db_models import Source as SourceRow
from core.enums import (
    SourceStatus,
)
from core.folders import DEFAULT_FOLDER


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
    pipeline_chain: str | None = None,
    target_lang: str | None = None,
    folder: str = DEFAULT_FOLDER,
    at: str,
) -> SourceRow:
    """Insert a source, or return the existing one for this identity.

    Returning the existing row rather than raising is what makes a retried
    create answer with the same body instead of a 409: the caller's intent was
    "this source should exist", and after the first request it does.

    There is no revival branch any more. It existed for soft-deleted rows,
    which answered 201 while never being collected again; deletion now removes
    the row (ADR 0021), so a re-subscribe finds nothing and inserts fresh. The
    new source starts with an empty cursor and re-samples history -- which is
    what deletion means, and the reason `seen_frontier` is worth keeping when
    the intent was only to clear a bad crawl.
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
        pipeline_chain=pipeline_chain,
        target_lang=target_lang,
        folder=folder,
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
