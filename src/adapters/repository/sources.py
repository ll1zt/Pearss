"""Source persistence."""

from __future__ import annotations

import hashlib
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from adapters.db_models import Source as SourceRow
from adapters.repository.origins import create_origin
from adapters.repository.subscriptions import create_subscription
from core.enums import (
    SourceStatus,
)
from core.folders import DEFAULT_FOLDER

LOGGER = logging.getLogger(__name__)


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

    The write is a façade over three tables while ADR 0046's split is staged.
    `source` keeps answering every existing read, and `origin`/`subscription`
    are written alongside it so the rows the later stages read already exist
    for every source created from now on. `origin.id` and `subscription.id`
    are this `source.id`, which is what keeps the three addressable by one key
    -- and every item id, guid and `/v1/sources/{id}` URL unchanged.

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
        # DEBUG: "add this feed" is idempotent, and a retrying client asks
        # again by design (issue #296).
        LOGGER.debug("source %s already exists; returning it", source_id)
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

    # The same write, expressed against the tables the split is moving to. The
    # origin is created first because the subscription points at it; both are
    # keyed on this row's id so the three stay one aggregate.
    origin = create_origin(
        session,
        origin_id=source_id,
        source_type=source_type,
        url=url,
        canonical_url=canonical_url,
        tenant_id=tenant_id,
        at=at,
        parser_plugin_id=parser_plugin_id,
    )
    create_subscription(
        session,
        subscription_id=source_id,
        origin_id=origin.id,
        schedule_policy=schedule_policy,
        tenant_id=tenant_id,
        at=at,
        pipeline_chain=pipeline_chain,
        target_lang=target_lang,
        folder=folder,
        trust_level=trust_level,
    )
    LOGGER.debug(
        "created source %s (%s) with its origin and subscription",
        source_id,
        source_type,
    )
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
