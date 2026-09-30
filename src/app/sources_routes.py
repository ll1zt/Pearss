"""Core API: /v1/sources.

Resource endpoints for managing what we collect. Per docs/api/style-guide.md
this is the surface everything else projects from — Swagger UI, scripts, and
eventually the MCP tools — so the shapes here are a published contract.

Writes require an Idempotency-Key, as AGENTS.md and the style guide both
mandate. A retry after a dropped connection must not create a second source or
trigger a second fetch, and the only way to guarantee that across processes is
to store the first response and replay it.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any, Final

import httpx
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.orm import Session

from adapters.correlation import current as current_correlation_id
from adapters.db_models import Source as SourceRow
from adapters.repository import (
    backlog_for_source,
    create_source,
    derive_source_id,
    failed_enrichments_for_source,
    get_source,
    job_for_source,
    list_job_runs,
    list_sources,
    parse_chain,
    timestamp,
)
from adapters.repository import (
    delete_source as repository_delete_source,
)
from adapters.repository import (
    delete_source_items as repository_delete_source_items,
)

# The API-wide conventions live in app.api_conventions; they are re-exported
# so the sibling route modules keep one import shape during the move.
from app.api_conventions import (
    _now,
    api_error,
    decode_cursor,
    encode_cursor,
    enrichment_page,
    idempotency_key,
    remember,
    replay_or_none,
)
from app.auth import Principal, read_required, write_required
from app.collector_registry import infer_source_type
from app.scheduler import collect_now
from core.enums import ErrorClass, SourceStatus, SourceType, TrustLevel
from core.folders import DEFAULT_FOLDER, normalize_folder
from core.models import schedule_policy_from_json, schedule_policy_to_json
from core.normalize import source_canonical_key
from core.tenancy import SINGLE_TENANT

LOGGER = logging.getLogger(__name__)

# How many recent runs the diagnostic view carries. Enough to see a failure
# streak, few enough that the answer stays one screen: the runs themselves have
# their own paged endpoint.
DIAGNOSE_RUNS = 5

# How many failed chain rows the diagnostic view carries (issue #152). The
# counts say how much is stuck; these rows say what is wrong, and five is
# enough to see a class and its provider message without becoming a list.
DIAGNOSE_FAILED_ENRICHMENTS = 5

# How many URLs one batch request may carry. A bound, not a capability: the
# endpoint exists for "subscribe this list", and a list longer than this is
# better split so one request stays reviewable and bounded (ADR 0044).
SOURCE_BATCH_LIMIT: Final = 50


# How long a stored response is replayed. Long enough to cover a client that
# retries after a timeout and then again a few seconds later; short enough that
# the table does not grow without bound.
IDEMPOTENCY_WINDOW = timedelta(days=7)

# What a 409 already_running tells the client to wait. A collection takes
# seconds to a couple of minutes; 30 is a hint that is neither so short that a
# retry immediately hits the same 409 nor so long that a freed slot sits idle.
_BUSY_RETRY_AFTER_SECONDS: Final = 30

router = APIRouter(prefix="/v1", tags=["core-api"])


# --------------------------------------------------------------------------
# Request models
# --------------------------------------------------------------------------


class SourceCreate(BaseModel):
    """Body for creating a source.

    `pipeline_chain` + `target_lang` enable the processing chain at creation
    time — the correct ordering: first source with chain, then first fetch,
    so no item is ever cached unprocessed.
    """

    url: str = Field(min_length=1, max_length=4096)
    type: SourceType = SourceType.RSS
    schedule_policy: dict[str, Any]
    trust_level: TrustLevel = TrustLevel.STANDARD
    parser_plugin_id: str | None = Field(default=None, max_length=128)
    pipeline_chain: list[dict[str, Any]] | None = None
    target_lang: str | None = None
    # The sidebar folder. Absent means the default folder, not "unset": every
    # subscription sits somewhere a client can show.
    #
    # The bound here is a coarse anti-abuse guard, not the real limit: it
    # measures before the validator trims, so a name of exactly the maximum
    # length with a stray space would be refused by a tighter value. The
    # authoritative limit lives in core.folders.
    folder: str | None = Field(default=None, max_length=256)

    @field_validator("folder")
    @classmethod
    def _valid_folder(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return normalize_folder(value)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("url")
    @classmethod
    def _url_not_empty(cls, value: str) -> str:
        # Shape only. Whether the URL is valid *for its type* is decided in
        # `_url_matches_type` below, which can see the type; a field validator
        # cannot, because pydantic runs them in declaration order and `url`
        # is declared before `type`.
        if not value.strip():
            raise ValueError("url must not be blank")
        return value

    @model_validator(mode="after")
    def _url_matches_type(self) -> SourceCreate:
        """Check the URL against the dedupe key for this source's type.

        Regression: this used to be a `field_validator` on `url`, which reads
        `info.data["type"]` -- but `url` is declared first, so `type` was not
        there yet and the validator fell back to its `RSS` default. Every
        `agent://` source was therefore refused as "unsupported URL scheme:
        'agent'", and A3 could not be configured through the API at all. An
        after-validator sees every field, so the check no longer depends on
        declaration order.

        The key is the thing to validate against rather than a generic URL
        parser: rss and web sources must carry a canonicalisable URL, while
        agent sources legitimately carry a synthetic `agent://<name>`.
        """
        kind = getattr(self.type, "value", self.type)
        try:
            source_canonical_key(source_type=str(kind), url=self.url)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        return self


class SourceBatchCreate(BaseModel):
    """Body for creating several sources in one request.

    Shared options apply to every URL. `type` may be `'auto'`, resolved per URL
    against the collectors exactly as the single endpoint's callers resolve it,
    so there is no per-item type field to keep in step.

    The list is validated for shape only. Whether a URL is usable *for its
    type* is decided per item by the handler, because one bad entry must fail
    alone rather than refuse the batch (ADR 0044).
    """

    urls: list[str] = Field(min_length=1)
    schedule_policy: dict[str, Any]
    type: str = "auto"
    trust_level: TrustLevel = TrustLevel.STANDARD
    parser_plugin_id: str | None = Field(default=None, max_length=128)
    pipeline_chain: list[dict[str, Any]] | None = None
    target_lang: str | None = None
    folder: str | None = Field(default=None, max_length=256)

    @field_validator("folder")
    @classmethod
    def _valid_folder(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return normalize_folder(value)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc


class SourcePatch(BaseModel):
    """Body for updating a source. Absent fields are left alone.

    `pipeline_chain` is a JSON list of {plugin_id, kind, lang?, required?}
    specs the source's items must complete before the gate opens; an explicit
    `null` clears the chain (items go ready straight after normalisation).
    Absent vs explicit-null is decided by `model_fields_set`, the pydantic v2
    way to give PATCH bodies three-state fields.
    """

    schedule_policy: dict[str, Any] | None = None
    status: SourceStatus | None = None
    trust_level: TrustLevel | None = None
    pipeline_chain: list[dict[str, Any]] | None = None
    target_lang: str | None = None
    # Explicit null moves the source back to the default folder -- the same
    # three-state convention pipeline_chain uses for clearing. Same coarse
    # bound as SourceCreate; core.folders owns the real limit.
    folder: str | None = Field(default=None, max_length=256)

    @field_validator("folder")
    @classmethod
    def _valid_folder(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return normalize_folder(value)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

    def sent(self, field: str) -> bool:
        """Whether the caller sent this field at all (even as null)."""
        return field in self.model_fields_set


class DeleteSourceItemsRequest(BaseModel):
    """Narrowing for a per-source item deletion.

    Every field is optional: an empty body means "everything this source
    collected", which is the common case after a bad crawl.
    """

    # Fixed-width UTC bounds compared against published_at. An item with no
    # published date falls outside any window, so a bounded delete leaves it
    # alone -- there is no timestamp to place it in one.
    since: str | None = None
    until: str | None = None
    # See the endpoint: defaulting true is the honest reading of "delete this
    # source's items". Passing false is the narrower request.
    include_published: bool = True


class BackfillChainRequest(BaseModel):
    """Narrowing for a chain backfill.

    `limit` caps how many items join the queue in one call. It exists because
    this verb is the one that queues fetches of third-party pages (issue
    #35): the default is a conservative batch, and an operator who wants a
    longer run asks for it explicitly rather than discovering it as a burst
    at someone's blog. Repeat the call to continue — queuing is idempotent.
    """

    limit: int | None = Field(default=None, ge=1)


def _shared_client(request: Request) -> httpx.Client:
    """The process-wide HTTP client, or a clear failure if startup did not run.

    The client is created by the application lifespan. Mounting the ASGI app
    with a bare transport — as tests and embedded uses do — skips that, and a
    direct `app.state.http_client` would then raise AttributeError and surface
    as an unexplained 500 on an endpoint whose contract says it triggers a
    fetch.
    """
    client: httpx.Client | None = getattr(request.app.state, "http_client", None)
    if client is None:
        raise api_error(
            status_code=503,
            code="http_client_unavailable",
            message=(
                "The shared HTTP client is not initialised; the application "
                "lifespan has not run."
            ),
            suggestion=(
                "Serve the app with a lifespan-aware runner (uvicorn) rather "
                "than a bare ASGI transport."
            ),
        )
    return client


def _source_payload(row: SourceRow) -> dict[str, Any]:
    """Render a source row.

    `url` is echoed as given and `canonical_url` as stored, so an operator can
    see what the dedupe key actually became rather than guessing.

    The paging state is included because it is the answer to "why are items
    missing": `seen_frontier`/`floor_cursor` bound the interval a paging
    collector has walked, and that interval is not recoverable from the stored
    items (a gap and the end of the walk look the same from `item` alone). It
    is null for sources that do not page, which is most of them.
    """
    return {
        "id": row.id,
        "type": row.type,
        "url": row.url,
        "canonical_url": row.canonical_url,
        # The name the origin reports, discovered at collection time. Null
        # until a source has been fetched once.
        "title": row.title,
        "parser_plugin_id": row.parser_plugin_id,
        "schedule_policy": json.loads(row.schedule_policy),
        "pipeline_chain": (
            json.loads(row.pipeline_chain) if row.pipeline_chain else None
        ),
        "target_lang": row.target_lang,
        "folder": row.folder,
        "status": row.status,
        "trust_level": row.trust_level,
        "tenant_id": row.tenant_id,
        # What the walk has covered. Both null for non-paging sources.
        "seen_frontier": row.seen_frontier,
        "floor_cursor": row.floor_cursor,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


# --------------------------------------------------------------------------
# Read endpoints
# --------------------------------------------------------------------------


@router.get("/sources", operation_id="listSources")
def read_sources(
    request: Request,
    _: Principal = Depends(read_required),
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None),
) -> dict[str, Any]:
    """List sources, oldest page first by creation order.

    Offset paging, unlike the items endpoint's keyset cursor. Sources are
    bounded (an operator's subscriptions, tens not millions) and new ones are
    appended at the end of this ordering, so a row arriving mid-walk shifts
    nothing already read -- the failure offset paging is famous for needs
    insertion *ahead* of the cursor, which is what items do and sources do not.
    The cursor is opaque to clients either way, so switching to keyset later
    would not be a breaking change.
    """
    del _
    offset = decode_cursor(cursor)
    with request.app.state.session_factory() as session:
        # One beyond the page: the extra row tells us whether to offer a cursor
        # without a second count query.
        rows = list_sources(
            session, tenant_id=SINGLE_TENANT, limit=limit, offset=offset
        )

    has_more = len(rows) > limit
    page = rows[:limit]
    return {
        "data": [_source_payload(row) for row in page],
        "next_cursor": encode_cursor(offset + limit) if has_more else None,
        "correlation_id": current_correlation_id(),
    }


@router.get("/sources/{source_id}:diagnose", operation_id="diagnoseSource")
def diagnose_source(
    request: Request,
    source_id: str,
    _: Principal = Depends(read_required),
) -> dict[str, Any]:
    """Everything one question needs: why is this source not updating?

    Declared *before* `GET /sources/{source_id}` on purpose: that route's path
    parameter matches `:diagnose` too, and Starlette takes the first route that
    matches, so the other order answers 404 with `source_id` set to
    "abc:diagnose". The POST verbs elsewhere in this module do not have the
    problem because no GET route can shadow them.

    An aggregate endpoint rather than six calls, which is ADR 0036's rule for a
    question that is always asked the same way: the source's own state, its job
    and recent runs with their error classes, how much is stuck behind its
    processing chain, and whether the collection loop is alive at all. The order
    those calls go in is knowledge the caller should not have to have, and
    stopping after two of them produces a confident answer built from half the
    evidence.

    `get_source` remains the full single row; this is the diagnostic view.
    """
    del _
    with request.app.state.session_factory() as session:
        row = _require_source(session, source_id)
        job = job_for_source(session, source_id)
        runs = (
            list_job_runs(session, job_id=job.id, limit=DIAGNOSE_RUNS)
            if job is not None
            else []
        )
        chain = backlog_for_source(session, source_id)
        failed_enrichments = failed_enrichments_for_source(
            session, source_id=source_id, limit=DIAGNOSE_FAILED_ENRICHMENTS
        )
        source = {
            "id": row.id,
            "type": row.type,
            "status": row.status,
            "url": row.url,
            "title": row.title,
            "schedule_policy": (
                json.loads(row.schedule_policy) if row.schedule_policy else None
            ),
            "pipeline_chain": (
                json.loads(row.pipeline_chain) if row.pipeline_chain else None
            ),
            "target_lang": row.target_lang,
            "folder": row.folder,
            # A paging collector's interval: how far back it has reached. Null
            # for feeds, which do not page.
            "seen_frontier": row.seen_frontier,
            "floor_cursor": row.floor_cursor,
        }
        job_summary = (
            None
            if job is None
            else {
                "id": job.id,
                "status": job.status,
                "schedule": job.schedule,
                "priority": job.priority,
            }
        )

    # Read from the running object rather than from the database: the loop's
    # liveness is a property of this process, and a row would be a second
    # opinion that can disagree with it.
    scheduler = getattr(request.app.state, "scheduler", None)
    loop_alive, loop_owner = (
        scheduler.liveness() if scheduler is not None else (True, "")
    )

    return {
        "source": source,
        "job": job_summary,
        "runs": runs,
        # The newest run's class, lifted to the top: "what did the last attempt
        # fail with" is the first question, and `runs[0]` buries it (issue #152).
        "last_run": (
            None
            if not runs
            else {
                "status": runs[0]["status"],
                "error_class": runs[0]["error_class"],
                "finished_at": runs[0]["finished_at"],
            }
        ),
        "chain": chain,
        # Why the chain is stuck: the failure class and the provider's own
        # message, newest first. Metadata only -- never the item body (issue #152).
        "failed_enrichments": failed_enrichments,
        "collection": {"loop_alive": loop_alive, "loop_owner": loop_owner},
        "correlation_id": current_correlation_id(),
    }


@router.get("/sources/{source_id}", operation_id="getSource")
def read_source(
    request: Request,
    source_id: str,
    _: Principal = Depends(read_required),
) -> dict[str, Any]:
    del _
    with request.app.state.session_factory() as session:
        row = get_source(session, source_id)
    if row is None:
        raise api_error(
            status_code=404,
            code="source_not_found",
            message=f"No source with id {source_id!r}.",
            suggestion="List sources to find the intended id.",
        )
    return {**_source_payload(row), "correlation_id": current_correlation_id()}


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------


@router.post("/sources", operation_id="addSource", status_code=201)
def add_source(
    request: Request,
    body: SourceCreate,
    principal: Principal = Depends(write_required),
    key: str = Depends(idempotency_key),
) -> JSONResponse:
    """Add a source to collect.

    Returns the existing source when this URL is already known, rather than an
    error: the operation is "make sure this is subscribed", and a retry after a
    dropped response must not fail.
    """
    endpoint = "POST /v1/sources"
    factory = request.app.state.session_factory
    replayed = replay_or_none(factory, actor=principal.name, endpoint=endpoint, key=key)
    if replayed is not None:
        return replayed

    try:
        policy_json = schedule_policy_to_json(
            schedule_policy_from_json(body.schedule_policy)
        )
        canonical = source_canonical_key(source_type=body.type.value, url=body.url)
    except ValueError as exc:
        raise api_error(
            status_code=422,
            code="invalid_source",
            message=str(exc),
            suggestion=(
                "schedule_policy is {type: fixed, interval: '30m'} or "
                "{type: adaptive, min: '15m', max: '24h'}."
            ),
        ) from exc
    if body.pipeline_chain:
        # Same gate-time deadlock guard as PATCH: a chain naming a processor
        # this deployment cannot run must be refused at configuration time.
        from adapters.repository import parse_chain as _parse_chain
        from app.enrichment_worker import registered_processor_ids

        try:
            _parse_chain(
                json.dumps(body.pipeline_chain),
                target_lang=body.target_lang,
                known_plugins=registered_processor_ids(),
            )
        except (ValueError, KeyError) as exc:
            raise api_error(
                status_code=422,
                code="invalid_pipeline_chain",
                message=str(exc),
                suggestion=(
                    "Use a list of {plugin_id, kind, lang?, required?} naming "
                    "registered processors; kinds: translation, summary, "
                    "fulltext, tts_audio."
                ),
            ) from exc

    with factory() as session:
        row = create_source(
            session,
            source_type=body.type.value,
            url=body.url,
            canonical_url=canonical,
            schedule_policy=policy_json,
            tenant_id=SINGLE_TENANT,
            trust_level=body.trust_level.value,
            parser_plugin_id=body.parser_plugin_id,
            pipeline_chain=(
                json.dumps(body.pipeline_chain) if body.pipeline_chain else None
            ),
            target_lang=body.target_lang,
            folder=body.folder or DEFAULT_FOLDER,
            at=timestamp(_now()),
        )
        session.commit()
        payload = _source_payload(row)

    # docs/api/style-guide.md: a response names the correlation id so a caller
    # can ask about the request it made. Set before `remember` so the stored
    # replay carries it too -- a retry is answered with the same fact.
    payload["correlation_id"] = current_correlation_id()
    remember(
        factory,
        actor=principal.name,
        endpoint=endpoint,
        key=key,
        status_code=201,
        body=payload,
    )
    return JSONResponse(status_code=201, content=payload)


@router.post("/sources:batch", operation_id="addSourcesBatch")
def add_sources_batch(
    request: Request,
    body: SourceBatchCreate,
    principal: Principal = Depends(write_required),
    key: str = Depends(idempotency_key),
) -> JSONResponse:
    """Add several sources in one request, reporting each item's outcome.

    The batch primitive behind MCP's `add_sources` (ADR 0044). Shared options
    apply to every URL, and `type: 'auto'` is resolved per URL. A URL that
    cannot be used fails alone -- the rest are still created -- because
    refusing the whole list over one typo is the loop this endpoint replaces.

    Answers 200 with per-item results rather than 201: the body is a report
    about a list, and some of it may have failed.
    """
    endpoint = "POST /v1/sources:batch"
    if len(body.urls) > SOURCE_BATCH_LIMIT:
        raise api_error(
            status_code=422,
            code="batch_too_large",
            message=(
                f"A batch may carry at most {SOURCE_BATCH_LIMIT} URLs; this one "
                f"has {len(body.urls)}."
            ),
            suggestion=(
                f"Split the list into batches of {SOURCE_BATCH_LIMIT} and retry."
            ),
        )

    factory = request.app.state.session_factory
    replayed = replay_or_none(factory, actor=principal.name, endpoint=endpoint, key=key)
    if replayed is not None:
        return replayed

    try:
        policy_json = schedule_policy_to_json(
            schedule_policy_from_json(body.schedule_policy)
        )
    except ValueError as exc:
        raise api_error(
            status_code=422,
            code="invalid_source",
            message=str(exc),
            suggestion=(
                "schedule_policy is {type: fixed, interval: '30m'} or "
                "{type: adaptive, min: '15m', max: '24h'}."
            ),
        ) from exc

    requested_type = body.type.strip().lower()
    if requested_type not in {"auto", "rss", "web", "agent"}:
        raise api_error(
            status_code=422,
            code="invalid_source",
            message=f"unsupported source type: {body.type!r}",
            suggestion="Use 'auto', 'rss', 'web' or 'agent'.",
        )

    if body.pipeline_chain:
        # Same gate-time deadlock guard as the single endpoint: a chain naming
        # a processor this deployment cannot run is refused at configuration
        # time, before any source exists.
        from app.enrichment_worker import registered_processor_ids

        try:
            parse_chain(
                json.dumps(body.pipeline_chain),
                target_lang=body.target_lang,
                known_plugins=registered_processor_ids(),
            )
        except (ValueError, KeyError) as exc:
            raise api_error(
                status_code=422,
                code="invalid_pipeline_chain",
                message=str(exc),
                suggestion=(
                    "Use a list of {plugin_id, kind, lang?, required?} naming "
                    "registered processors; kinds: translation, summary, "
                    "fulltext, tts_audio."
                ),
            ) from exc

    folder = body.folder or DEFAULT_FOLDER
    chain_json = json.dumps(body.pipeline_chain) if body.pipeline_chain else None
    at = timestamp(_now())

    results: list[dict[str, Any]] = []
    with factory() as session:
        for url in body.urls:
            try:
                source_type = (
                    infer_source_type(url)
                    if requested_type == "auto"
                    else requested_type
                )
                canonical = source_canonical_key(source_type=source_type, url=url)
            except ValueError as exc:
                # This item alone: the rest of the list still gets created.
                results.append(
                    {
                        "url": url,
                        "status": "failed",
                        "error_class": ErrorClass.PARSE.value,
                        "detail": str(exc),
                    }
                )
                continue

            source_id = derive_source_id(
                tenant_id=SINGLE_TENANT,
                source_type=source_type,
                canonical_url=canonical,
            )
            existed = session.get(SourceRow, source_id) is not None
            row = create_source(
                session,
                source_type=source_type,
                url=url,
                canonical_url=canonical,
                schedule_policy=policy_json,
                tenant_id=SINGLE_TENANT,
                trust_level=body.trust_level.value,
                parser_plugin_id=body.parser_plugin_id,
                pipeline_chain=chain_json,
                target_lang=body.target_lang,
                folder=folder,
                at=at,
            )
            results.append(
                {
                    "url": url,
                    "status": "existing" if existed else "created",
                    "source_id": row.id,
                }
            )
        session.commit()

    payload: dict[str, Any] = {
        "created": sum(1 for item in results if item["status"] == "created"),
        "existing": sum(1 for item in results if item["status"] == "existing"),
        "failed": sum(1 for item in results if item["status"] == "failed"),
        "results": results,
    }
    payload["correlation_id"] = current_correlation_id()
    remember(
        factory,
        actor=principal.name,
        endpoint=endpoint,
        key=key,
        status_code=200,
        body=payload,
    )
    return JSONResponse(status_code=200, content=payload)


def _require_source(session: Session, source_id: str) -> SourceRow:
    row = get_source(session, source_id)
    if row is None:
        raise api_error(
            status_code=404,
            code="source_not_found",
            message=f"No source with id {source_id!r}.",
            suggestion="List sources to find the intended id.",
        )
    return row


@router.patch("/sources/{source_id}", operation_id="updateSource")
def update_source(
    request: Request,
    source_id: str,
    body: SourcePatch,
    principal: Principal = Depends(write_required),
    key: str = Depends(idempotency_key),
) -> JSONResponse:
    """Change a source's schedule policy, status or trust level."""
    endpoint = f"PATCH /v1/sources/{source_id}"
    factory = request.app.state.session_factory
    replayed = replay_or_none(factory, actor=principal.name, endpoint=endpoint, key=key)
    if replayed is not None:
        return replayed

    changes: dict[str, str | None] = {}
    if body.schedule_policy is not None:
        try:
            changes["schedule_policy"] = schedule_policy_to_json(
                schedule_policy_from_json(body.schedule_policy)
            )
        except ValueError as exc:
            raise api_error(
                status_code=422,
                code="invalid_schedule_policy",
                message=str(exc),
                suggestion=(
                    "Use {type: fixed, interval: '30m'} or "
                    "{type: adaptive, min: '15m', max: '24h'}."
                ),
            ) from exc
    if body.status is not None:
        changes["status"] = body.status.value
    if body.trust_level is not None:
        changes["trust_level"] = body.trust_level.value
    if body.sent("pipeline_chain"):
        # json.dumps of None clears the chain; an empty list also clears it
        # (parse_chain normalises both to "no chain"). A sent-but-omitted
        # companion field is merged with the stored value below, before the
        # pair is validated -- a language-only PATCH must not wipe the chain.
        changes["pipeline_chain"] = (
            json.dumps(body.pipeline_chain) if body.pipeline_chain is not None else None
        )
    if body.sent("target_lang"):
        changes["target_lang"] = body.target_lang
    if body.sent("folder"):
        # Null resets to the default folder; the validator normalized any
        # sent name already.
        changes["folder"] = body.folder if body.folder is not None else DEFAULT_FOLDER

    if not changes:
        raise api_error(
            status_code=400,
            code="empty_patch",
            message="The request body changed nothing.",
            suggestion=(
                "Supply at least one of schedule_policy, status, trust_level, "
                "pipeline_chain, target_lang, folder."
            ),
        )

    with factory() as session:
        row = _require_source(session, source_id)
        if body.sent("pipeline_chain") or body.sent("target_lang"):
            # Validate the resulting pair before mutating anything. dict.get
            # preserves explicit null while omitted fields use stored values.
            from app.enrichment_worker import registered_processor_ids

            try:
                parse_chain(
                    changes.get("pipeline_chain", row.pipeline_chain),
                    target_lang=changes.get("target_lang", row.target_lang),
                    known_plugins=registered_processor_ids(),
                )
            except (ValueError, KeyError) as exc:
                raise api_error(
                    status_code=422,
                    code="invalid_pipeline_chain",
                    message=str(exc),
                    suggestion=(
                        "Use a list of {plugin_id, kind, lang?, required?}; kinds: "
                        "translation, summary, fulltext, tts_audio."
                    ),
                ) from exc
        for column, value in changes.items():
            setattr(row, column, value)
        row.updated_at = timestamp(_now())
        session.commit()
        payload = _source_payload(row)

    payload["correlation_id"] = current_correlation_id()
    remember(
        factory,
        actor=principal.name,
        endpoint=endpoint,
        key=key,
        status_code=200,
        body=payload,
    )
    return JSONResponse(status_code=200, content=payload)


@router.post("/sources/{source_id}:backfill-chain", operation_id="backfillChain")
def backfill_source_chain(
    request: Request,
    source_id: str,
    principal: Principal = Depends(write_required),
    key: str = Depends(idempotency_key),
    payload: BackfillChainRequest | None = None,
) -> JSONResponse:
    """Queue processor products for items stored before the chain was enabled.

    A write (it mutates item_enrichment and spends provider budget), so it is
    authenticated, idempotent and audited like every other write.

    The batch size defaults to a conservative limit; see
    `BackfillChainRequest`. Unbounded was how one call turned into 122 fetches
    of a single blog, so the default is now something an operator can watch.
    """
    endpoint = f"POST /v1/sources/{source_id}:backfill-chain"
    factory = request.app.state.session_factory
    replayed = replay_or_none(factory, actor=principal.name, endpoint=endpoint, key=key)
    if replayed is not None:
        return replayed

    from adapters.repository import backfill_chain
    from app.fetch_pacing import backfill_default_limit

    body = payload or BackfillChainRequest()
    limit = body.limit if body.limit is not None else backfill_default_limit()

    with factory() as session:
        row = _require_source(session, source_id)
        if not row.pipeline_chain:
            raise api_error(
                status_code=422,
                code="no_pipeline_chain",
                message="This source has no processing chain to backfill.",
                suggestion="Set pipeline_chain via PATCH first.",
            )
        try:
            queued = backfill_chain(session, source_id=source_id, limit=limit)
        except ValueError as exc:
            raise api_error(
                status_code=422,
                code="invalid_chain",
                message=str(exc),
                suggestion=(
                    "Use a list of {plugin_id, kind, lang?, required?}; kinds: "
                    "translation, summary, fulltext, tts_audio."
                ),
            ) from exc
        session.commit()

    result: dict[str, Any] = {
        "source_id": source_id,
        "queued": queued,
        "limit": limit,
        "audit": "recorded by the Core API",
        "correlation_id": current_correlation_id(),
    }
    remember(
        factory,
        actor=principal.name,
        endpoint=endpoint,
        key=key,
        status_code=200,
        body=result,
    )
    return JSONResponse(status_code=200, content=result)


@router.get("/sources/{source_id}/enrichments", operation_id="listEnrichments")
def list_source_enrichments(
    request: Request,
    source_id: str,
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None),
    _: Principal = Depends(read_required),
) -> dict[str, Any]:
    """Processor-chain rows for one source: the agent's visibility into
    what the chain is doing and why an item is being held.

    Bounded like every other list route. This one had no bound at all, which
    was survivable only while the MCP wrapper cut every list to 20; once the
    wrapper stopped overruling the requested page (issue #65), an unbounded
    `data` here would have been the one way a result set could still grow
    without anyone asking. `total` counts every match, so a short page is
    announced rather than implied.
    """
    from sqlalchemy import select as _select

    from adapters.db_models import Item as _ItemRow
    from adapters.db_models import ItemEnrichment

    del _
    offset = decode_cursor(cursor)
    with request.app.state.session_factory() as session:
        _require_source(session, source_id)
        query = _select(ItemEnrichment).where(
            ItemEnrichment.item_id.in_(
                _select(_ItemRow.id).where(_ItemRow.source_id == source_id)
            )
        )
        if status is not None:
            query = query.where(ItemEnrichment.status == status)
        rows = session.execute(query).scalars().all()
        return enrichment_page(
            rows, limit=limit, offset=offset, correlation_id=current_correlation_id()
        )


@router.post("/sources/{source_id}:retry-enrichments", operation_id="retryEnrichments")
def retry_source_enrichments(
    request: Request,
    source_id: str,
    principal: Principal = Depends(write_required),
    key: str = Depends(idempotency_key),
) -> JSONResponse:
    """Requeue failed enrichment rows for one source.

    This is the manual/agent re-trigger the retry policy stops at: rows that
    exhausted their attempts, or failed non-transiently (auth after a key
    rotation, parse on a content shape since fixed), come back as queued and
    the worker charges the provider again. A write for that reason.
    """
    endpoint = f"POST /v1/sources/{source_id}:retry-enrichments"
    factory = request.app.state.session_factory
    replayed = replay_or_none(factory, actor=principal.name, endpoint=endpoint, key=key)
    if replayed is not None:
        return replayed

    from sqlalchemy import select as _select
    from sqlalchemy import update as _update

    from adapters.db_models import Item as _ItemRow
    from adapters.db_models import ItemEnrichment

    with factory() as session:
        _require_source(session, source_id)
        result = session.execute(
            _update(ItemEnrichment)
            .where(
                ItemEnrichment.status == "failed",
                ItemEnrichment.item_id.in_(
                    _select(_ItemRow.id).where(_ItemRow.source_id == source_id)
                ),
            )
            .values(
                status="queued",
                # The content has not changed, so keep input_hash; but the
                # attempt budget restarts on an explicit re-trigger — the
                # operator has decided the failure is worth another pass.
                # The stale failure's error fields are cleared with it.
                attempt_count=0,
                next_attempt_at=None,
                error_class=None,
                detail=None,
                updated_at=timestamp(_now()),
            ),
        )
        requeued = int(result.rowcount or 0)
        session.commit()

    payload: dict[str, Any] = {
        "source_id": source_id,
        "requeued": requeued,
        "correlation_id": current_correlation_id(),
    }
    remember(
        factory,
        actor=principal.name,
        endpoint=endpoint,
        key=key,
        status_code=200,
        body=payload,
    )
    return JSONResponse(status_code=200, content=payload)


@router.delete("/sources/{source_id}", operation_id="deleteSource")
def delete_source(
    request: Request,
    source_id: str,
    principal: Principal = Depends(write_required),
    key: str = Depends(idempotency_key),
) -> JSONResponse:
    """Delete a source and everything it collected (ADR 0021).

    Destructive and not reversible: the source row, its items, their bodies,
    their read state and processor products, and the fetch records all go. The
    endpoint used to be a soft delete that left the source as a tombstone with
    its items published; that is no longer what this means, and any caller
    relying on the old behaviour is relying on something the API does not
    promise.

    Deleting an id that is already gone answers 200 with `sources: 0` rather
    than 404. A client retrying a request whose response was lost cannot tell
    "it was here and I removed it" from "it was never here", and after the
    first call those are the same fact.
    """
    endpoint = f"DELETE /v1/sources/{source_id}"
    factory = request.app.state.session_factory
    replayed = replay_or_none(factory, actor=principal.name, endpoint=endpoint, key=key)
    if replayed is not None:
        return replayed

    with factory() as session:
        result = repository_delete_source(session, source_id)
        session.commit()

    # Files after the commit: an unlink cannot be rolled back, and rows can.
    removed = result.reclaim.reclaim()

    payload: dict[str, Any] = {
        "source_id": source_id,
        **result.counts,
        "files_removed": removed,
        "correlation_id": current_correlation_id(),
    }
    remember(
        factory,
        actor=principal.name,
        endpoint=endpoint,
        key=key,
        status_code=200,
        body=payload,
    )
    return JSONResponse(status_code=200, content=payload)


@router.post("/sources/{source_id}/items:delete", operation_id="deleteSourceItems")
def delete_source_items(
    request: Request,
    source_id: str,
    principal: Principal = Depends(write_required),
    key: str = Depends(idempotency_key),
    payload: DeleteSourceItemsRequest | None = None,
) -> JSONResponse:
    """Delete a source's items, keeping the source itself.

    The "this crawl was wrong, keep collecting" case. The source keeps its
    cursor, so the subscription resumes rather than re-sampling history
    (ADR 0021).

    `include_published` defaults true: the caller has explicitly asked to
    clear this source's contents, and silently keeping the items every client
    has already cached would leave a source that looks emptied while its
    articles are still everywhere.
    """
    endpoint = f"POST /v1/sources/{source_id}/items:delete"
    factory = request.app.state.session_factory
    replayed = replay_or_none(factory, actor=principal.name, endpoint=endpoint, key=key)
    if replayed is not None:
        return replayed

    body = payload or DeleteSourceItemsRequest()
    with factory() as session:
        _require_source(session, source_id)
        outcome = repository_delete_source_items(
            session,
            source_id,
            since=body.since,
            until=body.until,
            include_published=body.include_published,
        )
        session.commit()

    # Files after the commit: an unlink cannot be rolled back, and rows can.
    removed = outcome.reclaim.reclaim()

    result: dict[str, Any] = {
        "source_id": source_id,
        **outcome.counts,
        "files_removed": removed,
        "correlation_id": current_correlation_id(),
    }
    remember(
        factory,
        actor=principal.name,
        endpoint=endpoint,
        key=key,
        status_code=200,
        body=result,
    )
    return JSONResponse(status_code=200, content=result)


@router.post("/sources/{source_id}:trigger", operation_id="triggerSource")
def trigger_source(
    request: Request,
    source_id: str,
    principal: Principal = Depends(write_required),
    key: str = Depends(idempotency_key),
) -> JSONResponse:
    """Collect a source now.

    Runs in the request rather than returning 202 with a job id: P1 has no
    worker, and the handler is `def` so the event loop stays free while it
    runs. The scheduler work replaces this with a durable job; until then a
    promise that could be lost to a restart would be worse than a slower
    answer that is true.
    """
    endpoint = f"POST /v1/sources/{source_id}:trigger"
    factory = request.app.state.session_factory
    replayed = replay_or_none(factory, actor=principal.name, endpoint=endpoint, key=key)
    if replayed is not None:
        return replayed

    with factory() as session:
        row = _require_source(session, source_id)
        if row.status != SourceStatus.ACTIVE.value:
            raise api_error(
                status_code=409,
                code="source_not_active",
                message=f"Source is {row.status}; only active sources are collected.",
                suggestion="Set status to active before triggering.",
            )
        # Resolved after the status check: a paused source is the caller's
        # error to see, and looking up the client first would report a missing
        # lifespan over a perfectly valid 409.
        client = _shared_client(request)
        # collect_now, not collect_source: it is what opens the JobRun, claims
        # it and closes it with the outcome. Calling the pipeline helper
        # directly collected the source but left no record that it happened, so
        # a manual trigger was invisible to list_runs and to the backoff that
        # reads run history — the same action observed through one entry point
        # and not the other.
        status, result = collect_now(
            session,
            source_id=source_id,
            policy_json=row.schedule_policy,
            client=client,
            registry=request.app.state.collector_registry,
        )
        session.commit()

    if result is None:
        raise api_error(
            status_code=409,
            code="already_running",
            message="Another collection of this source is in progress.",
            suggestion=(
                "Check list_runs for the outcome of the in-flight run, then "
                f"retry after about {_BUSY_RETRY_AFTER_SECONDS} seconds."
            ),
            # Wait for the in-flight run, not for a scheduling interval: the
            # slot frees when that collection ends, which is seconds to a
            # couple of minutes. The number is a hint -- HTTP allows any
            # positive delay -- and a client that retries once it has passed
            # either succeeds or gets 409 again with the same hint.
            headers={"Retry-After": str(_BUSY_RETRY_AFTER_SECONDS)},
        )

    payload: dict[str, Any] = {
        "source_id": source_id,
        "run_status": status.value,
        "succeeded": result.succeeded,
        "not_modified": result.not_modified,
        "drafts": result.drafts,
        "inserted": result.inserted,
        "skipped": result.skipped,
        "error_class": result.error_class,
        # The classification alone does not say what went wrong; without this
        # a failed run reports "parse" and the operator has to go digging in
        # the job_run table. It is origin-side text on a fetch failure, so it
        # is data: the docs' untrusted-content rule applies to it.
        "detail": result.detail,
        "correlation_id": current_correlation_id(),
    }
    status_code = 200 if result.succeeded else 502

    remember(
        factory,
        actor=principal.name,
        endpoint=endpoint,
        key=key,
        status_code=status_code,
        body=payload,
    )
    return JSONResponse(status_code=status_code, content=payload)
