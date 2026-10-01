"""ItemEnrichment persistence: the processor chain's fact source.

Creation happens when an item is stored (pipeline), execution by the
enrichment worker, and the gate consults the statuses here. The row identity
is (item, plugin, version, kind, lang, input_hash): re-processing the same
content version upserts, a content change is a new row.
"""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, Final, cast

from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from adapters.db_models import Item as ItemRow
from adapters.db_models import ItemEnrichment
from adapters.embedding_model import load_manifest, model_directory
from adapters.timestamps import fixed_width_utc as timestamp
from core.enrichment import (
    EnrichmentKind,
    EnrichmentSpec,
    EnrichmentStatus,
    chain_complete,
    input_identity,
    upstream_for,
)
from core.tenancy import SINGLE_TENANT


def parse_chain(
    raw: str | None,
    *,
    target_lang: str | None,
    known_plugins: frozenset[str] | None = None,
) -> tuple[EnrichmentSpec, ...]:
    """The source's chain, or an empty tuple when none is configured.

    Each entry: {plugin_id, kind, lang?, required?}. `lang` falls back to the
    source's `target_lang` for language products — the per-source default the
    operator set once, instead of repeating it per spec.

    `known_plugins` is the set of registered processor ids; when given, a
    spec naming an unregistered plugin raises here — at configuration time —
    instead of deadlocking the gate silently at runtime (a row the worker
    can never claim, skipped without a trace every sweep).
    """
    if not raw:
        return ()
    entries = json.loads(raw)
    if not isinstance(entries, list):
        raise ValueError(f"pipeline_chain must be a JSON list, got: {raw[:80]}")
    specs: list[EnrichmentSpec] = []
    for entry in entries:
        plugin_id = str(entry["plugin_id"])
        if known_plugins is not None and plugin_id not in known_plugins:
            raise ValueError(
                f"pipeline_chain names unregistered processor {plugin_id!r}; "
                f"registered: {sorted(known_plugins)}"
            )
        kind = EnrichmentKind(str(entry["kind"]))
        specs.append(
            EnrichmentSpec(
                plugin_id=plugin_id,
                kind=kind,
                lang=entry.get("lang") or target_lang,
                required=bool(entry.get("required", True)),
                # An embedding's identity is part of what the artifact *is*, so
                # it is fixed here -- when the row is enqueued -- rather than
                # discovered by the processor that later runs it. A row keyed
                # without it is reusable by a different model, which is the
                # silent reuse ADR 0035 decision 6 exists to prevent.
                model=(
                    load_manifest(model_directory()).identity
                    if kind is EnrichmentKind.EMBEDDING
                    else None
                ),
            )
        )
    return tuple(specs)


def chain_for_source(session: Session, *, source_id: str) -> tuple[EnrichmentSpec, ...]:
    """The configured chain for a source; empty when the source has none."""
    from adapters.db_models import Source as SourceRow

    source = session.get(SourceRow, source_id)
    if source is None:
        return ()
    return parse_chain(source.pipeline_chain, target_lang=source.target_lang)


def enqueue_chain(
    session: Session,
    *,
    item_id: str,
    content_hash: str,
    chain: tuple[EnrichmentSpec, ...],
    plugin_version: str = "0.1.0",
    tenant_id: str = SINGLE_TENANT,
    now: datetime | None = None,
) -> int:
    """Queue one row per spec for a freshly stored item.

    Called from the pipeline when the item is stored; the enrichment worker
    picks the rows up. Idempotent by the unique constraint — storing the same
    item twice queues nothing new.
    """
    if not chain:
        return 0
    stamp = timestamp(now or datetime.now(UTC))
    queued = 0
    for spec in chain:
        # Per spec, not one value: a spec whose model identity is part of what
        # its artifact *is* is stored under a different input_hash than one whose
        # output is a function of the content alone (ADR 0035 decision 6).
        identity = input_identity(content_hash, spec)
        existing = session.execute(
            select(ItemEnrichment).where(
                ItemEnrichment.item_id == item_id,
                ItemEnrichment.plugin_id == spec.plugin_id,
                ItemEnrichment.kind == spec.kind.value,
                ItemEnrichment.lang == spec.lang,
                ItemEnrichment.input_hash == identity,
            )
        ).scalar_one_or_none()
        if existing is not None:
            continue
        session.add(
            ItemEnrichment(
                # Random id, like raw_fetch: the row's identity is the unique
                # constraint (item, plugin, version, kind, lang, input_hash),
                # so a content change re-queues a NEW row. An id derived from
                # item+spec collided exactly there (proven in review).
                id=f"enr-{secrets.token_hex(8)}",
                item_id=item_id,
                plugin_id=spec.plugin_id,
                plugin_version=plugin_version,
                kind=spec.kind.value,
                lang=spec.lang,
                input_hash=identity,
                # The model that will produce it, so the row says which one and
                # a later reader does not have to infer it from the digest.
                model=str(spec.model) if spec.model is not None else None,
                status=EnrichmentStatus.QUEUED.value,
                tenant_id=tenant_id,
                created_at=stamp,
                updated_at=stamp,
            )
        )
        queued += 1
    return queued


def backlog_for_source(session: Session, source_id: str) -> dict[str, Any]:
    """The chain's state for one source, as counts rather than rows.

    "Why is this source not updating" usually wants "eleven items are stuck
    behind a failed translation", not the eleven rows -- the rows are what
    list_enrichments is for. `oldest_queued_at` is the part counts cannot say:
    a queue of one that has been waiting an hour is a different problem from a
    queue of one that just arrived.
    """
    item_ids = select(ItemRow.id).where(ItemRow.source_id == source_id)
    counts: dict[str, int] = {
        status: int(total)
        for status, total in session.execute(
            select(ItemEnrichment.status, func.count())
            .where(ItemEnrichment.item_id.in_(item_ids))
            .group_by(ItemEnrichment.status)
        ).all()
    }
    oldest = session.execute(
        select(func.min(ItemEnrichment.created_at)).where(
            ItemEnrichment.item_id.in_(item_ids),
            ItemEnrichment.status == EnrichmentStatus.QUEUED.value,
        )
    ).scalar_one_or_none()
    return {
        "queued": counts.get(EnrichmentStatus.QUEUED.value, 0),
        "running": counts.get(EnrichmentStatus.RUNNING.value, 0),
        "succeeded": counts.get(EnrichmentStatus.SUCCEEDED.value, 0),
        "failed": counts.get(EnrichmentStatus.FAILED.value, 0),
        "oldest_queued_at": oldest,
    }


# How far back a completion counts towards the queue's rate. Long enough that
# a quiet few minutes does not read as "stalled", short enough that the number
# still describes *now*: a rate over the whole history would answer "how fast
# has this ever been", which is not what a caller watching a queue is asking.
RATE_WINDOW_MINUTES: Final = 15.0


def completion_rate_per_min(
    session: Session, *, source_id: str, window_minutes: float = RATE_WINDOW_MINUTES
) -> float:
    """Completions per minute over the recent window, derived on every read.

    Derived rather than stored, for the reason `next_run_time` is not stored
    (ADR 0009): a stored rate is a second source of truth, and it disagrees
    with the rows the first time a writer forgets to update it.

    `updated_at` is the completion stamp: the row is written when the product
    lands, and a succeeded row is not touched again.
    """
    since = timestamp(datetime.now(UTC) - timedelta(minutes=window_minutes))
    item_ids = select(ItemRow.id).where(ItemRow.source_id == source_id)
    finished = int(
        session.execute(
            select(func.count())
            .select_from(ItemEnrichment)
            .where(
                ItemEnrichment.item_id.in_(item_ids),
                ItemEnrichment.status == EnrichmentStatus.SUCCEEDED.value,
                ItemEnrichment.updated_at >= since,
            )
        ).scalar_one()
    )
    return round(finished / window_minutes, 3)


def enrichment_status(session: Session, enrichment_id: str) -> str | None:
    """One row's status, or None when no row has that id."""
    return session.execute(
        select(ItemEnrichment.status).where(ItemEnrichment.id == enrichment_id)
    ).scalar_one_or_none()


def requeue_enrichment(session: Session, enrichment_id: str) -> bool:
    """Put one failed row back in the queue. True if it was failed.

    The only writer of "this row becomes retryable": the source-scoped route
    calls it once per failed row instead of carrying its own UPDATE, so the two
    granularities cannot drift in what a requeue *means*. It means the attempt
    budget restarts and the stale failure is cleared -- the operator or agent
    has decided the failure is worth another pass -- while `input_hash` stays,
    because the content has not changed.

    A row that is not failed is left alone: requeueing a `running` row would
    hand the same product to two workers, and a `succeeded` row already has one.
    """
    # session.execute is typed as returning Result[Any], but a DML statement
    # yields a CursorResult at runtime, which is where rowcount lives (the same
    # cast work.py and feed.py use for the same reason).
    result = cast(
        "CursorResult[Any]",
        session.execute(
            update(ItemEnrichment)
            .where(
                ItemEnrichment.id == enrichment_id,
                ItemEnrichment.status == EnrichmentStatus.FAILED.value,
            )
            .values(
                status=EnrichmentStatus.QUEUED.value,
                attempt_count=0,
                next_attempt_at=None,
                error_class=None,
                detail=None,
                updated_at=timestamp(datetime.now(UTC)),
            )
        ),
    )
    return int(result.rowcount or 0) > 0


def failed_enrichment_ids_for_source(session: Session, *, source_id: str) -> list[str]:
    """The failed chain rows of one source, oldest first.

    Selected rather than updated in bulk so the source-scoped requeue is a loop
    over `requeue_enrichment` -- one meaning of "requeue", N callers.
    """
    item_ids = select(ItemRow.id).where(ItemRow.source_id == source_id)
    return list(
        session.execute(
            select(ItemEnrichment.id)
            .where(
                ItemEnrichment.item_id.in_(item_ids),
                ItemEnrichment.status == EnrichmentStatus.FAILED.value,
            )
            .order_by(ItemEnrichment.updated_at, ItemEnrichment.id)
        ).scalars()
    )


def failed_enrichments_for_source(
    session: Session, *, source_id: str, limit: int = 5
) -> list[dict[str, Any]]:
    """The most recent failed chain rows for one source, newest first.

    The `:diagnose` answer to "why" (issue #152): `backlog_for_source` says how
    much is stuck, not what is wrong, and the failure class and the provider's
    own message live on these rows. Metadata and error text only -- never the
    item body, which is why this returns selected columns rather than the rows.
    """
    item_ids = select(ItemRow.id).where(ItemRow.source_id == source_id)
    rows = (
        session.execute(
            select(ItemEnrichment)
            .where(
                ItemEnrichment.item_id.in_(item_ids),
                ItemEnrichment.status == EnrichmentStatus.FAILED.value,
            )
            .order_by(ItemEnrichment.updated_at.desc(), ItemEnrichment.id.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return [
        {
            "enrichment_id": row.id,
            "item_id": row.item_id,
            "plugin_id": row.plugin_id,
            "kind": row.kind,
            "lang": row.lang,
            "error_class": row.error_class,
            "detail": row.detail,
            "attempt_count": row.attempt_count,
            "updated_at": row.updated_at,
        }
        for row in rows
    ]


def statuses_for_item(
    session: Session,
    *,
    item_id: str,
    content_hash: str,
    chain: tuple[EnrichmentSpec, ...],
) -> dict[tuple[str, str, str | None], EnrichmentStatus]:
    """Product statuses for one item, at the identity each spec asks for.

    `content_hash` is the item's content hash -- the value each spec's identity is
    derived *from* -- and not a row's own `input_hash`. Passing the latter would
    derive a second time and match nothing.

    The identities are per spec, not one value: an embedding is stored under a
    digest that includes its model, while a content-only product is stored under
    the content hash. Filtering on the content hash alone would miss the vector
    row and leave the gate shut on a chain that had actually finished -- a silent
    stall in place of the silent reuse this replaces.
    """
    wanted = {
        (spec.plugin_id, spec.kind.value, spec.lang): input_identity(content_hash, spec)
        for spec in chain
    }
    rows = session.execute(
        select(ItemEnrichment).where(ItemEnrichment.item_id == item_id)
    ).scalars()
    return {
        (row.plugin_id, row.kind, row.lang): EnrichmentStatus(row.status)
        for row in rows
        if row.input_hash == wanted.get((row.plugin_id, row.kind, row.lang))
    }


def upstream_for_spec(
    session: Session,
    *,
    item_id: str,
    content_hash: str,
    spec: EnrichmentSpec,
    chain: tuple[EnrichmentSpec, ...],
) -> tuple[EnrichmentSpec | None, str | None]:
    """The upstream spec for `spec` and its stored product ref (ADR 0030).

    Returns `(None, None)` when the spec reads the Item's own content, and
    `(upstream_spec, None)` when it consumes an upstream that has not produced
    a product for this content version yet -- the caller must tell "no upstream
    to wait for" apart from "waiting", and a single None cannot say both.

    Scoped to `content_hash` on purpose: the product must belong to the Item
    version being processed now. Resolving "the newest succeeded row" instead
    would feed a *different revision* of the upstream product into a spec
    running against this one -- the stale-text failure the dependency exists to
    prevent.
    """
    upstream = upstream_for(chain, spec)
    if upstream is None:
        return None, None
    ref = session.execute(
        select(ItemEnrichment.payload_ref)
        .where(
            ItemEnrichment.item_id == item_id,
            # The upstream spec's identity, not the item's: if the upstream is an
            # embedding, its row lives under a digest that includes its model.
            ItemEnrichment.input_hash == input_identity(content_hash, upstream),
            ItemEnrichment.plugin_id == upstream.plugin_id,
            ItemEnrichment.status == EnrichmentStatus.SUCCEEDED.value,
        )
        .order_by(ItemEnrichment.updated_at.desc())
    ).scalar_one_or_none()
    return upstream, ref


def is_source_chain_complete(
    session: Session,
    *,
    item_id: str,
    content_hash: str,
    chain: tuple[EnrichmentSpec, ...],
) -> bool:
    """Whether the item's gate may open — core.enrichment over stored rows."""
    if not chain:
        return True
    return chain_complete(
        chain,
        statuses_for_item(
            session, item_id=item_id, content_hash=content_hash, chain=chain
        ),
    )


def backfill_chain(
    session: Session,
    *,
    source_id: str,
    tenant_id: str = SINGLE_TENANT,
    limit: int | None = None,
) -> int:
    """Queue translations for a source's already-stored items.

    Enabling a chain gates *new* items at collection time; items stored
    before the chain existed are already through the gate (usually
    published, their content cached by clients). This one-off pass queues
    products for those items so the merged view fills in — idempotent by
    the same (item, plugin, version, kind, lang, input_hash) identity the
    pipeline uses, so re-running costs nothing for items already queued.
    """
    from adapters.db_models import Item as ItemRow
    from adapters.db_models import Source as SourceRow

    source = session.get(SourceRow, source_id)
    if source is None:
        raise ValueError(f"no source {source_id!r}")
    chain = parse_chain(source.pipeline_chain, target_lang=source.target_lang)
    if not chain:
        raise ValueError(
            f"source {source_id!r} has no pipeline_chain; nothing to backfill"
        )

    query = (
        select(ItemRow)
        .where(ItemRow.source_id == source_id)
        .order_by(ItemRow.published_at.desc())
    )
    items = session.execute(query).scalars().all()

    # `limit` caps the rows this call *queues*, not the items it looks at.
    # Capping the item scan instead made a repeated call a no-op: the newest
    # `limit` items were already queued, so the count stayed 0 while the rest
    # of the backlog was never reached. Batch, repeat, finish.
    queued = 0
    now = datetime.now(UTC)
    stamp = timestamp(now)
    for item in items:
        if limit is not None and queued >= limit:
            break
        for spec in chain:
            if limit is not None and queued >= limit:
                break
            identity = input_identity(item.content_hash, spec)
            exists = session.execute(
                select(ItemEnrichment).where(
                    ItemEnrichment.item_id == item.id,
                    ItemEnrichment.plugin_id == spec.plugin_id,
                    ItemEnrichment.kind == spec.kind.value,
                    ItemEnrichment.lang == spec.lang,
                    ItemEnrichment.input_hash == identity,
                )
            ).scalar_one_or_none()
            if exists is not None:
                continue
            session.add(
                ItemEnrichment(
                    id=f"enr-{secrets.token_hex(8)}",
                    item_id=item.id,
                    plugin_id=spec.plugin_id,
                    plugin_version="0.1.0",
                    kind=spec.kind.value,
                    lang=spec.lang,
                    input_hash=identity,
                    model=str(spec.model) if spec.model is not None else None,
                    status=EnrichmentStatus.QUEUED.value,
                    tenant_id=tenant_id,
                    created_at=stamp,
                    updated_at=stamp,
                )
            )
            queued += 1
    return queued
