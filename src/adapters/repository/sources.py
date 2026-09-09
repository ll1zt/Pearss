"""Source persistence."""

from __future__ import annotations

import hashlib

from sqlalchemy import select
from sqlalchemy.orm import Session

from adapters.db_models import Source as SourceRow
from core.enums import (
    SourceStatus,
)


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
            existing.pipeline_chain = pipeline_chain
            existing.target_lang = target_lang
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
        pipeline_chain=pipeline_chain,
        target_lang=target_lang,
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
