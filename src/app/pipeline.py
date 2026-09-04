"""Collection pipeline wiring.

Composition only: it joins the pieces that already exist — normalise, store the
body, attach the reference, persist the item, open the gate. No domain rules
live here; those are in core.

It exists so the ingest path is one thing rather than three. The API endpoint,
any future scheduler, and the tests all need the same sequence, and a copy in
each place is how they drift.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from adapters import filestore
from adapters.repository import StoreOutcome, promote_to_ready, store_item
from adapters.sanitize import sanitize_html
from core.models import Item, ItemDraft
from core.normalize import (
    DedupeAction,
    NormalizationResult,
    normalize,
    with_content_ref,
)

LOGGER = logging.getLogger(__name__)

TENANT = "default"


@dataclass(frozen=True, slots=True, kw_only=True)
class Ingested:
    """One draft, and what storing it decided."""

    result: NormalizationResult
    outcome: StoreOutcome


def normalise_and_store(
    session: Session,
    drafts: list[ItemDraft],
    *,
    tenant_id: str = TENANT,
    now: datetime | None = None,
) -> list[Ingested]:
    """Persist each draft: body to the file store, item to the database.

    Order matters. The body is written first so the row is never persisted with
    a reference to a file that does not exist yet; a crash between the two
    leaves an item with no body, which degrades to a title-only entry, rather
    than a reference that resolves to nothing.

    Returns one entry per draft, including those deduped away, so a caller can
    report what happened to each.
    """
    results: list[Ingested] = []

    for draft in drafts:
        result = normalize(draft, tenant_id=tenant_id, now=now)
        item = result.item

        # Sanitised here, at the boundary where untrusted markup becomes
        # stored content. Every reader of the store — RSS, the Reader API, any
        # future output — therefore serves safe HTML without having to remember
        # to sanitise, which is the failure mode a per-render sanitiser invites.
        if result.body:
            item = with_content_ref(
                item, filestore.store_body(sanitize_html(result.body))
            )

        outcome = store_item(session, item)
        if outcome.action in (DedupeAction.INSERT, DedupeAction.INSERT_RELATED):
            # P1 has no processor chain, so the gate has nothing to wait for
            # and items are promoted immediately. When P3 adds a chain, this
            # call moves to the end of that chain; the gate itself is unchanged.
            promote_to_ready(session, [item])
        results.append(Ingested(result=result, outcome=outcome))

    session.commit()
    LOGGER.info("stored %d draft(s)", len(results))
    return results


def bodies_for(session: Session, items: list[Item]) -> dict[str, str]:
    """Resolve stored bodies for `items`, keyed by item id.

    Items without a reference are omitted: a caller renders them without a
    description rather than with an empty one, which is the honest difference
    between "this item has no body" and "the body is blank".
    """
    return filestore.bodies_for({item.id: item.content_ref for item in items})
