"""ItemEnrichment persistence: the processor chain's fact source.

Creation happens when an item is stored (pipeline), execution by the
enrichment worker, and the gate consults the statuses here. The row identity
is (item, plugin, version, kind, lang, input_hash): re-processing the same
content version upserts, a content change is a new row.
"""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from adapters.db_models import ItemEnrichment
from adapters.timestamps import fixed_width_utc as timestamp
from core.enrichment import (
    EnrichmentKind,
    EnrichmentSpec,
    EnrichmentStatus,
    chain_complete,
    upstream_for,
)

TENANT = "default"


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
        specs.append(
            EnrichmentSpec(
                plugin_id=plugin_id,
                kind=EnrichmentKind(str(entry["kind"])),
                lang=entry.get("lang") or target_lang,
                required=bool(entry.get("required", True)),
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
    tenant_id: str = TENANT,
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
        existing = session.execute(
            select(ItemEnrichment).where(
                ItemEnrichment.item_id == item_id,
                ItemEnrichment.plugin_id == spec.plugin_id,
                ItemEnrichment.kind == spec.kind.value,
                ItemEnrichment.lang == spec.lang,
                ItemEnrichment.input_hash == content_hash,
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
                input_hash=content_hash,
                status=EnrichmentStatus.QUEUED.value,
                tenant_id=tenant_id,
                created_at=stamp,
                updated_at=stamp,
            )
        )
        queued += 1
    return queued


def statuses_for_item(
    session: Session, *, item_id: str, content_hash: str
) -> dict[tuple[str, str, str | None], EnrichmentStatus]:
    """Product statuses for one item at one content version."""
    rows = session.execute(
        select(ItemEnrichment).where(
            ItemEnrichment.item_id == item_id,
            ItemEnrichment.input_hash == content_hash,
        )
    ).scalars()
    return {
        (row.plugin_id, row.kind, row.lang): EnrichmentStatus(row.status)
        for row in rows
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
            ItemEnrichment.input_hash == content_hash,
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
        chain, statuses_for_item(session, item_id=item_id, content_hash=content_hash)
    )


def backfill_chain(
    session: Session,
    *,
    source_id: str,
    tenant_id: str = TENANT,
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
            exists = session.execute(
                select(ItemEnrichment).where(
                    ItemEnrichment.item_id == item.id,
                    ItemEnrichment.plugin_id == spec.plugin_id,
                    ItemEnrichment.kind == spec.kind.value,
                    ItemEnrichment.lang == spec.lang,
                    ItemEnrichment.input_hash == item.content_hash,
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
                    input_hash=item.content_hash,
                    status=EnrichmentStatus.QUEUED.value,
                    tenant_id=tenant_id,
                    created_at=stamp,
                    updated_at=stamp,
                )
            )
            queued += 1
    return queued
