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
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final, Protocol, cast
from urllib.parse import urlsplit

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
from adapters.repository.search_index import index_item
from adapters.sanitize import sanitize_html
from app import collector_registry
from app.collector_registry import CollectorRegistry
from app.collector_registry import Registration as CollectorRegistration
from core.enrichment import EnrichmentKind, EnrichmentSpec
from core.enums import ErrorClass, UnitOutcome
from core.language import is_already_in, prose_text
from core.models import Item, ItemDraft
from core.normalize import (
    DedupeAction,
    NormalizationResult,
    normalize,
    with_content_ref,
)
from core.tenancy import SINGLE_TENANT

LOGGER = logging.getLogger(__name__)


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
    text = prose_text(body) or prose_text(title)
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
    tenant_id: str = SINGLE_TENANT,
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
        # Sanitised once, kept in hand: the index below needs the same text the
        # store gets, and reading the file back to get it would be a second
        # source of the same bytes. The condition stays on the *raw* body, so an
        # item whose markup sanitises away is still stored with an empty body
        # rather than becoming title-only -- that is a different state, and this
        # change has no business choosing it.
        body_markup = sanitize_html(result.body)
        if result.body:
            item = with_content_ref(item, filestore.store_body(body_markup))

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
            # The search index is written here, in the same transaction as the
            # row it describes, and only for the outcomes that write or advance
            # the row -- a deduped-away draft changed nothing to index.
            index_item(
                session,
                item_id=item.id,
                title=item.title,
                body=body_markup,
            )
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
    tenant_id: str = SINGLE_TENANT,
    max_pages: int = 1,
    registry: CollectorRegistry | None = None,
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

    `registry` is the application's own collector registry; `None` means the
    process default, which is what direct callers (the CLI, collector suites)
    pass. Application paths always pass theirs explicitly -- the point of the
    parameter is that an application never silently reads another one's
    collectors (issue #84).
    """
    registry = registry or collector_registry.default_registry()
    # The plugin lifecycle gate (ADR 0017): a disabled collector collects
    # nothing. Skipped rather than failed — the same semantics as a paused
    # source, an operator decision that must not masquerade as an incident
    # (a failed run here would page the operator for a state they chose).
    registration = registry.select_for(
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
        registry=registry,
    )
    if not result.succeeded or max_pages <= 1:
        return result

    registration = registry.select_for(source.type, url)
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
            session,
            url=page_url,
            source=source,
            client=client,
            tenant_id=tenant_id,
            registry=registry,
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
    registry: CollectorRegistry | None = None,
) -> CollectionResult:
    """One fetch-parse-store pass over `url`.

    Split out of `collect_source` so a backfill can call it per page without
    duplicating the sequence, and so the single-page case has no loop in it.

    `max_pages` is forwarded to a paged collector's walk: without this the
    walk received its own default (20) no matter what the caller asked for,
    and every routine collection of a paged source crawled up to twenty pages
    instead of the one the budget promised.
    """
    registry = registry or collector_registry.default_registry()
    # Selection happens before any network call: match_url is documented as
    # network-free, and a URL no collector claims should not be fetched at all.
    registration = registry.select_for(source.type, url)
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
    tenant_id: str = SINGLE_TENANT,
    max_pages: int = 20,
) -> CollectionResult:
    """Collect a source whose origin is walked a page at a time.

    The walk itself is collector knowledge — credentials, page shape, origin
    recovery quirks — and lives in the collector (`collect_pages`). The host
    owns everything durable: it reads the paging state, hands it in, stores
    the resulting drafts and writes the frontier back. A mid-walk failure
    keeps the pages that did arrive and leaves the frontier where it was:
    moving it past content that was never fetched would skip that content for
    good (issue #131). A collector never
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
            floor_cursor=state.floor_cursor,
        ),
        max_pages=max_pages,
    )

    set_source_title_if_absent(
        session, source_id=source.id, title=_paged_source_title(registration)
    )

    if walk.error_class is not None:
        # A partial walk: a page failed after earlier ones arrived. The walk
        # kept those pages by contract, so store them -- and deliberately do
        # not move the frontier. Advancing it past content that was never
        # fetched (the page that failed, and everything below it) would make
        # the next round start at the top, meet the moved frontier and stop,
        # skipping that content for good (issue #131).
        if walk.drafts:
            normalise_and_store(
                session,
                walk.drafts,
                tenant_id=tenant_id,
                chain=chain_for_source(session, source_id=source.id),
            )
        # The walk classifies itself; the host just carries it onto the run.
        return CollectionResult(
            succeeded=False,
            error_class=walk.error_class.value,
            detail=walk.detail,
        )

    # Only a budget-limited walk yields a floor candidate, and the walk says
    # so itself -- cursors are opaque, so no comparison here could tell a
    # floor from a gap position or a frontier stop. Written only on a clean
    # end: a frontier moved by a partial walk would strand the unfetched
    # pages below it.
    save_paging_state(
        session,
        source_id=source.id,
        newest_id=walk.newest_id,
        floor_candidate=walk.floor_candidate,
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


# --------------------------------------------------------------------------
# Content-level discovery: a caller's address list, against one source
#
# The pipeline owns the sequence -- accept the address, fetch it through the
# source's collector, store the draft -- so the route, the MCP tool and any
# future caller share one implementation of "what happens to an address". A
# collector only ever sees addresses this module has already accepted.
# --------------------------------------------------------------------------

# The caller's knobs and the server's bounds on them. The defaults are the
# polite shape: a burst of `batch_size` addresses, then a pause.
DEFAULT_FETCH_ITEMS_BATCH: Final = 20
MAX_FETCH_ITEMS_BATCH: Final = 50
DEFAULT_FETCH_ITEMS_INTERVAL: Final = 2.0
MAX_FETCH_ITEMS_INTERVAL: Final = 60.0


class FetchItemAction(StrEnum):
    """What happened to one address, in the caller's vocabulary.

    Four values rather than the store's own action names: the caller branches
    on "did this address become an item", and the dedupe nuance (`insert` vs
    `insert_related` vs `skip_same_source`) is this deployment's business.
    """

    # A new item (or a pending one advanced by fresher content).
    STORED = "stored"
    # The store recognised the item and kept what was there.
    EXISTING = "existing"
    # Refused here, before the collector was called.
    REJECTED = "rejected"
    # The collector was called and the fetch or the parse failed.
    FAILED = "failed"


@dataclass(frozen=True, slots=True, kw_only=True)
class FetchedAddress:
    """One address the caller supplied, and what became of it."""

    url: str
    action: FetchItemAction
    item_id: str | None = None
    error_class: str | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class FetchItemsResult:
    """The whole report: what was asked, what was stored, per address.

    The effective pacing rides along because the server clamps what the caller
    asked for: a caller that cannot see the effective values cannot tell
    "accepted as asked" from "quietly overridden".
    """

    requested: int
    inserted: int
    batch_size: int
    min_interval_seconds: float
    deployment_min_interval_seconds: float
    results: tuple[FetchedAddress, ...]


def fetch_items(
    session: Session,
    *,
    source: SourceRow,
    client: httpx.Client,
    urls: Sequence[str],
    tenant_id: str = SINGLE_TENANT,
    batch_size: int | None = None,
    min_interval_seconds: float | None = None,
    registry: CollectorRegistry | None = None,
) -> FetchItemsResult:
    """Fetch a caller's addresses into one source, reporting each outcome.

    The caller supplies addresses it discovered outside the feed window (an
    agent enumerating a site's history); the source's collector fetches them,
    so the site's knowledge stays in the collector and every item still lands
    through `normalise_and_store` -- one write path, one dedupe rule, one gate.

    Two guards run *before* anything is fetched:

    - **the host**: an address must be on the source's own host (`www.` aside).
      `source_id` is the provenance boundary (ADR 0022), and a list of
      addresses is not a licence to fetch from servers this source never
      agreed to talk to.
    - **the capability**: `collect_item_with` refuses a collector that does not
      declare the item capability, so an unsupported source produces no
      request at all.

    One address's failure does not stop the list -- except a `blocked` answer,
    which is the origin asking this deployment to stop. The remaining
    addresses are then reported as rejected and not fetched, because
    continuing is exactly the burst the pacing exists to prevent.

    `batch_size` and `min_interval_seconds` shape the pacing: that many
    addresses are fetched, then a pause of that many seconds. Both are clamped
    to the server's bounds, and the deployment's own interval is a floor under
    them (an operator's statement about how hard this deployment may hit
    someone else's server, which a request cannot talk down).
    """
    registry = registry or collector_registry.default_registry()
    source_url = source.url or source.canonical_url or ""
    registration = registry.select_for(source.type, source_url)
    effective_batch = _clamped_batch_size(batch_size)
    effective_interval, deployment_floor = _clamped_interval(min_interval_seconds)
    chain = chain_for_source(session, source_id=source.id)

    results: list[FetchedAddress] = []
    inserted = 0

    for index, raw_url in enumerate(urls):
        if index and index % effective_batch == 0:
            # Between bursts, not before the first: the caller asked for this
            # shape and the deployment's floor already governs the spacing.
            time.sleep(effective_interval)

        unit = fetch_one(
            session,
            source=source,
            registration=registration,
            client=client,
            url=raw_url.strip(),
            chain=chain,
            tenant_id=tenant_id,
        )
        if unit.action is FetchItemAction.STORED:
            inserted += 1
        results.append(
            FetchedAddress(
                url=raw_url,
                action=unit.action,
                item_id=unit.item_id,
                error_class=(
                    None if unit.error_class is None else unit.error_class.value
                ),
                detail=unit.detail,
            )
        )
        if unit.error_class is ErrorClass.BLOCKED:
            # The origin asked this deployment to stop. The rest are reported
            # as not attempted, because continuing is the burst the pacing
            # exists to prevent.
            results.extend(
                FetchedAddress(
                    url=rest,
                    action=FetchItemAction.REJECTED,
                    error_class=ErrorClass.BLOCKED.value,
                    detail="not attempted: the origin refused this address",
                )
                for rest in urls[index + 1 :]
            )
            break

    return FetchItemsResult(
        requested=len(urls),
        inserted=inserted,
        batch_size=effective_batch,
        min_interval_seconds=effective_interval,
        deployment_min_interval_seconds=deployment_floor,
        results=tuple(results),
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class FetchUnit:
    """One address's outcome, in both vocabularies the two surfaces need.

    `action` is the report's word (`stored`/`existing`/`rejected`/`failed`) and
    the ledger's outcome is *derived* from it by `outcome_for`: two vocabularies
    that are tracked separately are two vocabularies that can disagree.
    """

    action: FetchItemAction
    error_class: ErrorClass | None = None
    detail: str = ""
    item_id: str | None = None


# The ledger's word for a report's action. `stored` and `existing` are the same
# outcome -- the unit succeeded -- and differ only in what the store did, which
# the detail line carries.
_ACTION_TO_OUTCOME: dict[FetchItemAction, UnitOutcome] = {
    FetchItemAction.STORED: UnitOutcome.OK,
    FetchItemAction.EXISTING: UnitOutcome.OK,
    FetchItemAction.REJECTED: UnitOutcome.REJECTED,
    FetchItemAction.FAILED: UnitOutcome.FAILED,
}


def outcome_for(action: FetchItemAction) -> UnitOutcome:
    """The ledger's outcome for one address's action."""
    return _ACTION_TO_OUTCOME[action]


def fetch_one(
    session: Session,
    *,
    source: SourceRow,
    registration: CollectorRegistration | None,
    client: httpx.Client,
    url: str,
    chain: tuple[EnrichmentSpec, ...] = (),
    tenant_id: str = SINGLE_TENANT,
) -> FetchUnit:
    """One address, end to end: preflight, fetch, store. Never raises for a
    classified failure -- the caller needs an outcome, not an exception.

    This is the atom both granularities share: the batch entry point loops it
    and reports, the work worker loops it and appends a ledger row. Extracting
    it is what keeps "one implementation" true while the *reporting* differs.

    One address is one unit even when a collector returns several drafts: they
    are stored together (one transaction, one dedupe decision each) and reported
    as one outcome, because the ledger's key is `(work, target)`.
    """
    source_url = source.url or source.canonical_url or ""
    if not _on_source_host(source_url, url):
        return FetchUnit(
            action=FetchItemAction.REJECTED,
            error_class=ErrorClass.STRUCTURE_CHANGED,
            detail="address is not on the source's own host",
        )
    if registration is None:
        return FetchUnit(
            action=FetchItemAction.REJECTED,
            error_class=ErrorClass.STRUCTURE_CHANGED,
            detail="no collector serves this source",
        )

    try:
        drafts = collector_registry.collect_item_with(
            registration, source_id=source.id, url=url, client=client
        )
    except Exception as exc:
        # The classified error is duck-typed across every plugin boundary in
        # this repository (in-process FetchError, the spec's CollectorError,
        # the registry's own): one handler covers them all, and anything
        # without an ErrorClass is a bug, not an outcome.
        error_class = getattr(exc, "error_class", None)
        if not isinstance(error_class, ErrorClass):
            raise
        return FetchUnit(
            action=FetchItemAction.FAILED,
            error_class=error_class,
            detail=getattr(exc, "detail", str(exc)),
        )

    if not drafts:
        return FetchUnit(
            action=FetchItemAction.FAILED,
            error_class=ErrorClass.PARSE,
            detail="the address produced no item",
        )

    ingested = normalise_and_store(session, drafts, tenant_id=tenant_id, chain=chain)
    stored = any(
        entry.outcome.action
        in (
            DedupeAction.INSERT,
            DedupeAction.INSERT_RELATED,
            DedupeAction.UPDATE_PENDING,
        )
        for entry in ingested
    )
    return FetchUnit(
        action=FetchItemAction.STORED if stored else FetchItemAction.EXISTING,
        detail="stored" if stored else "already present",
        item_id=ingested[0].outcome.item.id if ingested else None,
    )


def fetch_pacing(params: Mapping[str, Any]) -> tuple[int, float]:
    """(batch size, interval) for a work's pacing knobs, both clamped.

    Shared by the batch entry point and the work worker so a work paced one way
    through `POST /v1/works` and another way through the legacy endpoint is not
    possible: the knobs live in the same place and clamp the same way.
    """
    requested_interval = params.get("min_interval_seconds")
    interval, _floor = _clamped_interval(
        None if requested_interval is None else float(requested_interval)
    )
    requested_batch = params.get("batch_size")
    return (
        _clamped_batch_size(None if requested_batch is None else int(requested_batch)),
        interval,
    )


def _clamped_batch_size(batch_size: int | None) -> int:
    if batch_size is None:
        return DEFAULT_FETCH_ITEMS_BATCH
    return max(1, min(int(batch_size), MAX_FETCH_ITEMS_BATCH))


def _clamped_interval(min_interval_seconds: float | None) -> tuple[float, float]:
    """The effective interval, and the deployment's floor under it.

    The floor comes from the deployment's own pacing settings
    (`app.fetch_pacing`, the same numbers the enrichment worker obeys), so one
    operator setting governs every path that fetches from someone else's
    server.
    """
    from app.fetch_pacing import HostPacer

    floor = HostPacer.from_env().min_interval
    requested = (
        DEFAULT_FETCH_ITEMS_INTERVAL
        if min_interval_seconds is None
        else float(min_interval_seconds)
    )
    return max(0.0, min(requested, MAX_FETCH_ITEMS_INTERVAL), floor), floor


def _on_source_host(source_url: str, address: str) -> bool:
    """Whether `address` is on the source's own host (`www.` aside).

    `www.` is the one difference allowed: sites publish their feed and their
    articles under either name, and they are the same site. Anything that is
    not an http(s) address is refused here too -- the check is on the parsed
    host, and a `file:` or `javascript:` address has none.
    """
    source_host = _url_host(source_url)
    address_host = _url_host(address)
    return bool(source_host) and _without_www(source_host) == _without_www(address_host)


def _url_host(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return ""
    return (parts.hostname or "").lower()


def _without_www(host: str) -> str:
    return host[4:] if host.startswith("www.") else host
