"""Core API: agent ingest (A3, ADR 0022).

`POST /v1/sources/{source_id}:ingest` is how an external agent pushes
content it gathered elsewhere. It is split from the sources CRUD module
because it is a separate capability with its own credential (`ingest`
scope), its own schema and its own ADR -- the only thing it shares with
source management is the conventions module and the source row itself.

The draft is built here and handed to `normalise_and_store`, the same call
a collector's parse output takes. Everything downstream -- sanitising at
the store boundary, content-addressed storage, the six-way dedupe, the
publish gate -- is therefore shared with collection rather than
reimplemented, which is the whole point of the design: a second writer
would either bypass sanitisation or force it to be duplicated.
"""

from __future__ import annotations

from typing import Any, Final

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from adapters.correlation import current as current_correlation_id
from adapters.repository import chain_for_source as _chain_for_source
from adapters.repository import get_source
from app.api_conventions import api_error, idempotency_key, remember, replay_or_none
from app.auth import Principal, ingest_required
from app.ingest import IngestItem, ingest_draft
from app.pipeline import normalise_and_store
from core.enums import SourceType
from core.normalize import DedupeAction

router = APIRouter(prefix="/v1", tags=["core-api"])

# Items one ingest request may carry. See IngestRequest for why this exists
# even though ADR 0022 declines per-source limits.
MAX_INGEST_ITEMS: Final = 100


class IngestRequest(BaseModel):
    """Body for pushing items into an agent source.

    The cap is body-parsing hygiene rather than per-source rate limiting
    (which ADR 0022 explicitly declines to add): at 1 MiB per request an
    uncapped list admits thousands of title-only items in one call, and each
    one costs a dedupe lookup and possibly an enrichment row.
    """

    items: list[IngestItem] = Field(min_length=1, max_length=MAX_INGEST_ITEMS)


@router.post("/sources/{source_id}:ingest", operation_id="ingestItems")
def ingest_items(
    request: Request,
    source_id: str,
    body: IngestRequest,
    principal: Principal = Depends(ingest_required),
    key: str = Depends(idempotency_key),
) -> JSONResponse:
    """Push items into an agent source (A3, ADR 0022).

    The draft is built here and handed to `normalise_and_store`, the same call
    a collector's parse output takes. Everything downstream -- sanitising at
    the store boundary, content-addressed storage, the six-way dedupe, the
    publish gate -- is therefore shared with collection rather than
    reimplemented, which is the whole point of the design: a second writer
    would either bypass sanitisation or force it to be duplicated.

    Scope is `ingest`: a credential that can push content is not automatically
    one that can delete sources, rewrite schedules or read anyone's state. The
    full token holds both, so this narrows the *ingest* credential rather than
    the full one (ADR 0022).

    Items are visible on submit when the source has no chain, matching a
    chainless rss source. The user's decision (2026-09-14) was explicit that
    agent content should be visible without a hold-back step.
    """
    endpoint = f"POST /v1/sources/{source_id}:ingest"
    factory = request.app.state.session_factory
    replayed = replay_or_none(factory, actor=principal.name, endpoint=endpoint, key=key)
    if replayed is not None:
        return replayed

    with factory() as session:
        source = get_source(session, source_id)
        if source is None:
            raise api_error(
                status_code=404,
                code="source_not_found",
                message=f"No source with id {source_id!r}.",
                suggestion="Create the agent source first with POST /v1/sources.",
            )
        # Checked rather than assumed: pushing into an rss source would mix
        # agent-supplied content into a source whose items are supposed to
        # come from one origin, and the provenance would be unrecoverable.
        if source.type != SourceType.AGENT.value:
            raise api_error(
                status_code=409,
                code="not_an_agent_source",
                message=(
                    f"Source {source_id!r} has type {source.type!r}; only "
                    "agent sources accept pushed items."
                ),
                suggestion=(
                    "Create a source with type='agent' and a url like "
                    "'agent://<name>', then push to that."
                ),
            )

        source_url = source.canonical_url
        drafts = [
            ingest_draft(item, source_id=source_id, source_url=source_url)
            for item in body.items
        ]

        ingested = normalise_and_store(
            session,
            drafts,
            tenant_id=source.tenant_id,
            chain=_chain_for_source(session, source_id=source_id),
        )

    results = [
        {
            "id": entry.result.item.id,
            "external_id": drafts[index].external_id,
            "action": entry.outcome.action.value,
            "reason": entry.outcome.reason,
        }
        for index, entry in enumerate(ingested)
    ]
    payload: dict[str, Any] = {
        "source_id": source_id,
        "submitted": len(drafts),
        "inserted": sum(
            1
            for entry in ingested
            if entry.outcome.action
            in (
                DedupeAction.INSERT,
                DedupeAction.INSERT_RELATED,
                DedupeAction.UPDATE_PENDING,
            )
        ),
        "results": results,
        "correlation_id": current_correlation_id(),
    }
    remember(
        factory,
        actor=principal.name,
        endpoint=endpoint,
        key=key,
        status_code=201,
        body=payload,
    )
    return JSONResponse(status_code=201, content=payload)
