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
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, cast

import httpx
from sqlalchemy.orm import Session

from adapters import filestore
from adapters.correlation import current as current_correlation_id
from adapters.db_models import Source as SourceRow
from adapters.http import FetchError
from adapters.raw_payload import RawPayload
from adapters.repository import (
    StoreOutcome,
    chain_for_source,
    enqueue_chain,
    last_conditional_state,
    paging_state,
    promote_to_ready,
    save_paging_state,
    set_source_title_if_absent,
    store_item,
)
from adapters.sanitize import sanitize_html
from app import collector_registry
from app.collector_registry import Registration as CollectorRegistration
from core.enrichment import EnrichmentKind, EnrichmentSpec
from core.enums import ErrorClass
from core.language import is_already_in
from core.models import Item, ItemDraft
from core.normalize import (
    DedupeAction,
    NormalizationResult,
    normalize,
    plain_text,
    with_content_ref,
)

LOGGER = logging.getLogger(__name__)

TENANT = "default"


@dataclass(frozen=True, slots=True, kw_only=True)
class Ingested:
    """One draft, and what storing it decided."""

    result: NormalizationResult
    outcome: StoreOutcome


def _specs_needing_work(
    chain: tuple[EnrichmentSpec, ...], *, body: str, title: str
) -> tuple[EnrichmentSpec, ...]:
    """The chain's specs that still have work to do for this item.

    A translation spec is dropped when the text is already in its target
    language. Dropping it here rather than at the processor is what keeps such
    an item out of the queue entirely: it publishes in its own cycle instead of
    waiting for a worker to discover there is nothing to do.

    Only translations are judged. A future non-language product (summary, TTS)
    has no "already in the target language" case and must still run, so the
    filter is per-spec and the remaining chain is enqueued unchanged.

    The text is read the same way the processor reads it -- body first, title
    as the fallback for title-only items -- so an item the processor would skip
    is exactly an item dropped here.
    """
    text = plain_text(body) or plain_text(title)
    remaining: list[EnrichmentSpec] = []
    for spec in chain:
        if (
            spec.kind is EnrichmentKind.TRANSLATION
            and spec.lang is not None
            and is_already_in(text, spec.lang)
        ):
            LOGGER.info(
                "item text is already in %s; not queueing its translation",
                spec.lang,
            )
            continue
        remaining.append(spec)
    return tuple(remaining)


def normalise_and_store(
    session: Session,
    drafts: list[ItemDraft],
    *,
    tenant_id: str = TENANT,
    now: datetime | None = None,
    chain: tuple[EnrichmentSpec, ...] = (),
) -> list[Ingested]:
    """Persist each draft: body to the file store, item to the database.

    Order matters. The body is written first so the row is never persisted with
    a reference to a file that does not exist yet; a crash between the two
    leaves an item with no body, which degrades to a title-only entry, rather
    than a reference that resolves to nothing.

    With a non-empty `chain`, each stored item gets its enrichment rows
    queued and stays pending — the enrichment worker opens the gate when the
    chain completes. Without one (P1 behaviour) items are promoted here.

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
            # UPDATE_PENDING belongs here too: the row is still unseen, so it
            # advances on the new content rather than waiting for nothing.
            #
            # Content already in the target language drops its translation
            # spec *here*, before any row exists. Skipping at the processor
            # instead still occupies a queue slot and still holds the gate
            # until a worker reaches it -- so a Chinese timeline waited behind
            # its own English posts to be told there was nothing to do. The
            # decision is the same one the processor makes, from the same
            # function, so the two can never disagree about an item.
            wanted = _specs_needing_work(chain, body=result.body, title=item.title)
            if wanted:
                enqueue_chain(
                    session,
                    item_id=item.id,
                    content_hash=item.content_hash,
                    chain=wanted,
                    tenant_id=tenant_id,
                    now=now,
                )
            else:
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
    # Set when the source's plugin is disabled: nothing was attempted, and
    # the run is not an incident (ADR 0017 — an operator decision, not a
    # failure). Callers treat it like not_modified.
    skipped_plugin: bool = False
    # Where to continue a backfill from, when the collector exposes a cursor.
    # Carried on the result rather than returned separately so a caller that
    # ignores backfill does not have to handle a second value.
    last_cursor: str | None = None


def _plugin_disabled(session: Session, plugin_id: str, *, tenant_id: str) -> bool:
    """Whether the operator has disabled this collector (ADR 0017)."""
    from adapters.repository import plugin_status

    return plugin_status(session, plugin_id, tenant_id=tenant_id) == "disabled"


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
    # The plugin lifecycle gate (ADR 0017): a disabled collector collects
    # nothing. Skipped rather than failed — the same semantics as a paused
    # source, an operator decision that must not masquerade as an incident
    # (a failed run here would page the operator for a state they chose).
    registration = collector_registry.select_for(
        source.type, source.url or source.canonical_url or ""
    )
    if registration is not None and _plugin_disabled(
        session, str(registration.manifest["id"]), tenant_id=tenant_id
    ):
        LOGGER.info(
            "collection skipped: plugin %s is disabled",
            registration.manifest["id"],
        )
        return CollectionResult(succeeded=True, skipped_plugin=True)

    url = source.url or source.canonical_url
    if not url:
        return CollectionResult(
            succeeded=False,
            error_class=ErrorClass.PARSE.value,
            detail="source has no url",
        )

    result = _collect_one_page(
        session,
        url=url,
        source=source,
        client=client,
        tenant_id=tenant_id,
        max_pages=max_pages,
    )
    if not result.succeeded or max_pages <= 1:
        return result

    registration = collector_registry.select_for(source.type, url)
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
    max_pages: int = 1,
) -> CollectionResult:
    """One fetch-parse-store pass over `url`.

    Split out of `collect_source` so a backfill can call it per page without
    duplicating the sequence, and so the single-page case has no loop in it.

    `max_pages` is forwarded to a paged collector's walk: without this the
    walk received its own default (20) no matter what the caller asked for,
    and every routine collection of a paged source crawled up to twenty pages
    instead of the one the budget promised.
    """
    # Selection happens before any network call: match_url is documented as
    # network-free, and a URL no collector claims should not be fetched at all.
    registration = collector_registry.select_for(source.type, url)
    if registration is None:
        # Classified as structure_changed rather than parse: nothing here knows
        # how to read what the origin would return, which is what the
        # classification means, and unlike a transient decode problem it will
        # not be retried as if waiting helps.
        return CollectionResult(
            succeeded=False,
            error_class=ErrorClass.STRUCTURE_CHANGED.value,
            detail=f"no collector registered for {url}",
        )

    try:
        payload = collector_registry.fetch_page_with(
            registration,
            url,
            client=client,
            source_id=source.id,
            conditional=last_conditional_state(session, source_id=source.id),
        )
    except FetchError as exc:
        # Failed fetches are recorded too: the run's diagnostics point at a
        # RawFetch row, and a 403's absence of ETag is itself the conditional
        # state the next cycle should act on (none — revalidate from scratch).
        _record_raw_fetch_failure(session, source_id=source.id, exc=exc)
        return CollectionResult(
            succeeded=False, error_class=exc.error_class.value, detail=exc.detail
        )

    _record_raw_fetch_payload(session, source_id=source.id, payload=payload)

    if payload.not_modified:
        return CollectionResult(succeeded=True, not_modified=True)

    if registration.paged:
        return collect_paged(
            session,
            source=source,
            registration=registration,
            client=client,
            tenant_id=tenant_id,
            max_pages=max_pages,
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

    ingested = normalise_and_store(
        session,
        drafts,
        tenant_id=tenant_id,
        chain=chain_for_source(session, source_id=source.id),
    )
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


if TYPE_CHECKING:

    class PagedCollector(Protocol):
        """A collector registered with `paged=True` (capability contract)."""

        def collect_pages(
            self,
            *,
            source_id: str,
            source_url: str,
            client: httpx.Client,
            state: Any,
            max_pages: int = 20,
        ) -> Any: ...


def _record_raw_fetch_payload(
    session: Session, *, source_id: str, payload: RawPayload
) -> None:
    """Record the fetch attempt and its conditional state.

    This is the loop-closer for Conditional GET: the response's ETag and
    Last-Modified land on a RawFetch row, and `last_conditional_state` reads
    the newest row for the next cycle's If-None-Match. A 304 is recorded
    like a 200 — its headers re-affirm the current validators, and without
    the row the next fetch would go unconditional. The payload body itself
    stays out of the database (payload_ref/filestore is the raw-content
    path for 200s; a 304 carries none).
    """
    session.add(
        _raw_fetch_row(
            source_id=source_id,
            status="not_modified" if payload.not_modified else "fetched",
            payload=payload,
        )
    )


def _record_raw_fetch_failure(
    session: Session, *, source_id: str, exc: FetchError
) -> None:
    """Record a failed fetch. No validators arrived, so none are stored —
    the next cycle revalidates from scratch, which is the correct response
    to a 403/5xx that may or may not be validator-related."""
    session.add(
        _raw_fetch_row(
            source_id=source_id,
            status="failed",
            payload=None,
            error_class=exc.error_class,
            detail=exc.detail,
        )
    )


def _raw_fetch_row(
    *,
    source_id: str,
    status: str,
    payload: RawPayload | None,
    error_class: ErrorClass | None = None,
    detail: str = "",
) -> Any:
    import secrets

    from adapters.db_models import RawFetch as RawFetchRow
    from adapters.repository import timestamp

    row = RawFetchRow(
        id=f"rawfetch-{secrets.token_hex(8)}",
        source_id=source_id,
        status=status,
        http_status=payload.status_code if payload else None,
        # A 304 re-affirms the validators it was sent with; RawPayload.etag
        # on a 304 carries exactly those (adapters.http echoes them back).
        etag=payload.etag if payload else None,
        last_modified=payload.last_modified if payload else None,
        error_class=error_class.value if error_class else None,
        fetched_at=timestamp(datetime.now(UTC)),
        correlation_id=current_correlation_id(),
    )
    if payload is not None and payload.body:
        # content_hash of the body, for diagnostics; the body itself goes to
        # the file store only through the item path (raw feed bodies are
        # rebuildable from the origin, and storing them twice is waste).
        import hashlib

        row.content_hash = hashlib.sha256(payload.body).hexdigest()
    return row


def collect_paged(
    session: Session,
    *,
    source: SourceRow,
    registration: CollectorRegistration,
    client: httpx.Client,
    tenant_id: str = TENANT,
    max_pages: int = 20,
) -> CollectionResult:
    """Collect a source whose origin is walked a page at a time.

    The walk itself is collector knowledge — credentials, page shape, origin
    recovery quirks — and lives in the collector (`collect_pages`). The host
    owns everything durable: it reads the paging state, hands it in, stores
    the resulting drafts and writes the frontier back. A collector never
    touches SQLAlchemy, and this module never imports a collector module.
    """
    from adapters.repository import PagingState

    state = paging_state(session, source_id=source.id)
    # `paged=True` on the registration is the capability contract; the
    # Protocol above gives that attribute a static home.
    walker = cast("PagedCollector", registration.collector).collect_pages
    walk = walker(
        source_id=source.id,
        source_url=source.canonical_url,
        client=client,
        state=PagingState(
            seen_frontier=state.seen_frontier,
            resume_cursor=state.resume_cursor,
        ),
        max_pages=max_pages,
    )

    set_source_title_if_absent(
        session, source_id=source.id, title=_paged_source_title(registration)
    )
    # Only a walk that ended on the floor may move it. `resume_cursor` also
    # carries gap cursors and budget-stop bottoms -- positions that are real
    # but shallower -- and cursors are opaque strings, so the host cannot tell
    # them apart without the walk saying so.
    save_paging_state(
        session,
        source_id=source.id,
        newest_id=walk.newest_id,
        resume_cursor=walk.resume_cursor if walk.reached_floor else None,
    )

    if walk.error_class is not None:
        # The walk classifies itself; the host just carries it onto the run.
        return CollectionResult(
            succeeded=False,
            error_class=walk.error_class.value,
            detail=walk.detail,
        )

    ingested = normalise_and_store(
        session,
        walk.drafts,
        tenant_id=tenant_id,
        chain=chain_for_source(session, source_id=source.id),
    )
    inserted = sum(
        1
        for entry in ingested
        if entry.outcome.action in (DedupeAction.INSERT, DedupeAction.INSERT_RELATED)
    )
    return CollectionResult(
        succeeded=True,
        drafts=len(walk.drafts),
        inserted=inserted,
        skipped=len(walk.drafts) - inserted,
    )


def _paged_source_title(registration: CollectorRegistration) -> str:
    """The display title a paged origin reports for itself.

    Page-based origins are timelines, not feeds: no payload carries a channel
    title, so the manifest's display_title is the honest label. (The feed
    path learns titles from the payload itself; see _collect_one_page.)
    """
    title = registration.manifest["display_title"] or registration.manifest["id"]
    return str(title)


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
