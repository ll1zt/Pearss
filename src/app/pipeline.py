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

import httpx
from sqlalchemy.orm import Session

from adapters import filestore
from adapters.db_models import Source as SourceRow
from adapters.http import FetchError, fetch
from adapters.repository import (
    StoreOutcome,
    last_conditional_state,
    promote_to_ready,
    set_source_title_if_absent,
    store_item,
)
from adapters.sanitize import sanitize_html
from app import collector_registry
from core.enums import ErrorClass
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
        if outcome.action in (
            DedupeAction.INSERT,
            DedupeAction.INSERT_RELATED,
            DedupeAction.UPDATE_PENDING,
        ):
            # P1 has no processor chain, so the gate has nothing to wait for
            # and items are promoted immediately. When P3 adds a chain, this
            # call moves to the end of that chain; the gate itself is unchanged.
            # UPDATE_PENDING belongs here too: the row is still unseen, so it
            # advances on the new content rather than waiting for nothing.
            promote_to_ready(session, [item])
        results.append(Ingested(result=result, outcome=outcome))

    session.commit()
    LOGGER.info("stored %d draft(s)", len(results))
    return results


@dataclass(frozen=True, slots=True, kw_only=True)
class CollectionResult:
    """Outcome of one collection run."""

    succeeded: bool
    error_class: str | None = None
    detail: str | None = None
    drafts: int = 0
    inserted: int = 0
    skipped: int = 0
    not_modified: bool = False
    # Where to continue a backfill from, when the collector exposes a cursor.
    # Carried on the result rather than returned separately so a caller that
    # ignores backfill does not have to handle a second value.
    last_cursor: str | None = None


def collect_source(
    session: Session,
    *,
    source: SourceRow,
    client: httpx.Client,
    tenant_id: str = TENANT,
    max_pages: int = 1,
) -> CollectionResult:
    """Fetch, parse, normalise and store one source.

    Failures are returned rather than raised: a failed collection is a normal
    outcome that has to be recorded on the JobRun with its ErrorClass, and an
    exception would force every caller to reconstruct that classification.

    `max_pages` above 1 walks backwards through an origin's history when the
    collector supports it, for a first-time backfill or to check whether an
    earlier run missed anything. It defaults to 1 because every extra page is a
    request against someone else's server, and routine collection only needs
    what the origin shows as current.
    """
    url = source.url or source.canonical_url
    if not url:
        return CollectionResult(
            succeeded=False,
            error_class=ErrorClass.PARSE.value,
            detail="source has no url",
        )

    result = _collect_one_page(
        session, url=url, source=source, client=client, tenant_id=tenant_id
    )
    if not result.succeeded or max_pages <= 1:
        return result

    registration = collector_registry.select(url)
    if registration is None:
        return result
    backfiller = collector_registry.backfiller(registration)
    if backfiller is None:
        return result

    cursor = result.last_cursor
    pages = 1
    while cursor and pages < max_pages:
        page_url = backfiller.before_url(url, cursor)
        if not page_url or page_url == url:
            # A collector that cannot advance would otherwise spin here. All
            # pages already fetched are kept; only the backfill stops.
            break
        page = _collect_one_page(
            session, url=page_url, source=source, client=client, tenant_id=tenant_id
        )
        pages += 1
        result = _merge(result, page)
        if not page.succeeded:
            break
        next_cursor = page.last_cursor
        # Stop when the origin stops moving backwards: an empty page or a
        # repeated cursor both mean the beginning has been reached. Comparing
        # against the previous cursor also catches an origin that ignores
        # `before`, which would otherwise return the same page forever.
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor

    return result


def _collect_one_page(
    session: Session,
    *,
    url: str,
    source: SourceRow,
    client: httpx.Client,
    tenant_id: str,
) -> CollectionResult:
    """One fetch-parse-store pass over `url`.

    Split out of `collect_source` so a backfill can call it per page without
    duplicating the sequence, and so the single-page case has no loop in it.
    """
    try:
        payload = fetch(
            url,
            client=client,
            source_id=source.id,
            conditional=last_conditional_state(session, source_id=source.id),
        )
    except FetchError as exc:
        return CollectionResult(
            succeeded=False, error_class=exc.error_class.value, detail=exc.detail
        )

    if payload.not_modified:
        return CollectionResult(succeeded=True, not_modified=True)

    registration = collector_registry.select(url)
    if registration is None:
        # No collector claims this URL. Recording it as structure_changed
        # rather than parse: the fetch worked, but nothing here knows how to
        # read what came back — which is what the classification means, and
        # unlike a generic parse failure it will not be retried as if a
        # transient decode problem were to blame.
        return CollectionResult(
            succeeded=False,
            error_class=ErrorClass.STRUCTURE_CHANGED.value,
            detail=f"no collector registered for {url}",
        )

    try:
        drafts = collector_registry.parse_with(registration, payload)
    except collector_registry.CollectorError as exc:
        return CollectionResult(
            succeeded=False, error_class=exc.error_class.value, detail=exc.detail
        )

    # The collectors report the name the origin gives itself through draft
    # metadata, which normalize() drops: a channel title belongs to the source,
    # not to each of its items. Captured here, at the one moment both the
    # drafts and the source row are in scope.
    if drafts:
        set_source_title_if_absent(
            session,
            source_id=source.id,
            title=drafts[0].metadata.get("feed_title"),
        )

    ingested = normalise_and_store(session, drafts, tenant_id=tenant_id)
    inserted = sum(
        1
        for entry in ingested
        if entry.outcome.action in (DedupeAction.INSERT, DedupeAction.INSERT_RELATED)
    )
    backfiller = collector_registry.backfiller(registration)
    cursor = backfiller.oldest_cursor(drafts) if backfiller else None
    return CollectionResult(
        succeeded=True,
        drafts=len(drafts),
        inserted=inserted,
        skipped=len(drafts) - inserted,
        last_cursor=cursor,
    )


def _merge(first: CollectionResult, page: CollectionResult) -> CollectionResult:
    """Accumulate one backfill page into a running result.

    The cursor comes from the newest page because that is where the walk
    continues; everything else sums. A failed page keeps its error: reporting
    a partial backfill as clean would hide that some history was not fetched.
    """
    return CollectionResult(
        succeeded=first.succeeded and page.succeeded,
        error_class=page.error_class or first.error_class,
        detail=page.detail or first.detail,
        drafts=first.drafts + page.drafts,
        inserted=first.inserted + page.inserted,
        skipped=first.skipped + page.skipped,
        last_cursor=page.last_cursor or first.last_cursor,
    )


def bodies_for(session: Session, items: list[Item]) -> dict[str, str]:
    """Resolve stored bodies for `items`, keyed by item id.

    Items without a reference are omitted: a caller renders them without a
    description rather than with an empty one, which is the honest difference
    between "this item has no body" and "the body is blank".
    """
    return filestore.bodies_for({item.id: item.content_ref for item in items})
