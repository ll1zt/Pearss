"""ItemEnrichment persistence: the processor chain's fact source.

Creation happens when an item is stored (pipeline), execution by the
enrichment worker, and the gate consults the statuses here. The row identity
is (item, plugin, version, kind, lang, input_hash): re-processing the same
content version upserts, a content change is a new row.
"""

from __future__ import annotations

import json
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
)

TENANT = "default"


def parse_chain(
    raw: str | None, *, target_lang: str | None
) -> tuple[EnrichmentSpec, ...]:
    """The source's chain, or an empty tuple when none is configured.

    Each entry: {plugin_id, kind, lang?, required?}. `lang` falls back to the
    source's `target_lang` for language products — the per-source default the
    operator set once, instead of repeating it per spec.
    """
    if not raw:
        return ()
    entries = json.loads(raw)
    if not isinstance(entries, list):
        raise ValueError(f"pipeline_chain must be a JSON list, got: {raw[:80]}")
    specs: list[EnrichmentSpec] = []
    for entry in entries:
        kind = entry["kind"]
        specs.append(
            EnrichmentSpec(
                plugin_id=str(entry["plugin_id"]),
                kind=EnrichmentKind(kind),
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
                id=f"enr-{item_id[:8]}-{spec.plugin_id[:8]}-{spec.kind.value[:8]}-{queued}",
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
