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

import base64
import binascii
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.orm import Session, sessionmaker

from adapters.correlation import current as current_correlation_id
from adapters.db_models import Source as SourceRow
from adapters.repository import chain_for_source as _chain_for_source
from adapters.repository import (
    create_source,
    find_idempotent,
    get_source,
    list_sources,
    parse_chain,
    store_idempotent,
    timestamp,
)
from adapters.repository import (
    delete_source as repository_delete_source,
)
from adapters.repository import (
    delete_source_items as repository_delete_source_items,
)
from app.auth import Principal, ingest_required, read_required, write_required
from app.pipeline import normalise_and_store
from app.scheduler import collect_now
from core.enums import SourceStatus, SourceType, TrustLevel
from core.folders import DEFAULT_FOLDER, normalize_folder
from core.models import ItemDraft, schedule_policy_from_json, schedule_policy_to_json
from core.normalize import DedupeAction, canonicalize_url, source_canonical_key

LOGGER = logging.getLogger(__name__)

TENANT = "default"

# How long a stored response is replayed. Long enough to cover a client that
# retries after a timeout and then again a few seconds later; short enough that
# the table does not grow without bound.
IDEMPOTENCY_WINDOW = timedelta(days=7)

# What a 409 already_running tells the client to wait. A collection takes
# seconds to a couple of minutes; 30 is a hint that is neither so short that a
# retry immediately hits the same 409 nor so long that a freed slot sits idle.
_BUSY_RETRY_AFTER_SECONDS: Final = 30

# Items one ingest request may carry. See IngestRequest for why this exists
# even though ADR 0022 declines per-source limits.
MAX_INGEST_ITEMS: Final = 100

router = APIRouter(prefix="/v1", tags=["core-api"])


# --------------------------------------------------------------------------
# Error envelope
# --------------------------------------------------------------------------


def api_error(
    *,
    status_code: int,
    code: str,
    message: str,
    suggestion: str,
    error_class: str | None = None,
    details: list[Any] | None = None,
    headers: dict[str, str] | None = None,
) -> HTTPException:
    """Build an HTTPException carrying the documented error envelope.

    The correlation id is embedded so a client can quote one value when
    reporting a problem, and `code` is stable while `message` is allowed to
    change — clients branch on the former.

    `headers` exists for the answers that carry a retry interval: HTTP has a
    header for that, and a response whose body says "try later" while its
    headers say nothing makes every caller invent its own backoff.
    """
    return HTTPException(
        status_code=status_code,
        headers=headers,
        detail={
            "error": {
                "code": code,
                "message": message,
                "error_class": error_class,
                "suggestion": suggestion,
                "details": details or [],
                "correlation_id": current_correlation_id(),
            }
        },
    )


# --------------------------------------------------------------------------
# Pagination
# --------------------------------------------------------------------------


def encode_cursor(offset: int) -> str:
    """An opaque cursor. Clients must not parse it, so its layout is free to
    change; only the value it decodes to is meaningful."""
    return base64.urlsafe_b64encode(f"v1:{offset}".encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode()).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise api_error(
            status_code=400,
            code="invalid_cursor",
            message="The cursor could not be decoded.",
            suggestion="Omit the cursor to start from the beginning.",
        ) from None
    prefix, _, digits = decoded.partition(":")
    if prefix != "v1" or not digits.isdigit():
        raise api_error(
            status_code=400,
            code="invalid_cursor",
            message="The cursor is not valid for this API version.",
            suggestion="Omit the cursor to start from the beginning.",
        )
    offset = int(digits)
    if offset > 10_000_000:
        raise api_error(
            status_code=400,
            code="invalid_cursor",
            message="The cursor is not valid for this API version.",
            suggestion="Page through the list instead of seeking.",
        )
    return offset


# --------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------


def idempotency_key(
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> str:
    """Require and validate the Idempotency-Key header on a write."""
    if not idempotency_key or not idempotency_key.strip():
        raise api_error(
            status_code=400,
            code="idempotency_key_required",
            message="This write requires an Idempotency-Key header.",
            suggestion=(
                "Generate a UUID per logical operation and reuse it when "
                "retrying the same operation."
            ),
        )
    key = idempotency_key.strip()
    if len(key) > 128:
        raise api_error(
            status_code=400,
            code="idempotency_key_too_long",
            message="Idempotency-Key must be at most 128 characters.",
            suggestion="Use a UUID.",
        )
    return key


def _now() -> datetime:
    return datetime.now(UTC)


def _redact_queries(value: Any) -> Any:
    """Strip query strings from URL-shaped strings in a replayed payload.

    The idempotency store keeps whole response bodies for a week, and source
    payloads carry `url`/`canonical_url` verbatim - feed URLs routinely
    embed access tokens in their query (AGENTS.md privacy rule, same reason
    the audit trail redacts headers and adapters.http redacts logged URLs).
    A replay is still an exposure surface: it goes to whoever holds the key,
    which after a leak is exactly the wrong party. Structured recursion over
    dicts and lists; strings that parse as URLs with a query get the query
    dropped, everything else passes through untouched.
    """
    if isinstance(value, dict):
        return {key_: _redact_queries(item) for key_, item in value.items()}
    if isinstance(value, list):
        return [_redact_queries(item) for item in value]
    if isinstance(value, str) and value.count("?") == 1:
        base, _, query = value.partition("?")
        # Only strip when the left side plausibly is a URL (scheme://...):
        # a schedule policy string or a title containing a lone "?" is data,
        # not an address.
        if "://" in base and query:
            return base
    return value


def replay_or_none(
    session_factory: sessionmaker[Any],
    *,
    actor: str,
    endpoint: str,
    key: str,
) -> JSONResponse | None:
    """Return a stored response for this write, if one exists.

    The replayed body is query-redacted: see `_redact_queries`. The original
    execution's response carried the full URLs; the replay deliberately does
    not, because a client that wants the URLs again has the resource itself
    (GET /v1/sources/{id}) under its own authenticated request.
    """
    now = timestamp(_now())
    with session_factory() as session:
        stored = find_idempotent(
            session, actor=actor, endpoint=endpoint, key=key, now=now
        )
        if stored is None:
            return None
        payload: dict[str, Any] = _redact_queries(json.loads(stored.response_body))
        response = JSONResponse(status_code=stored.status_code, content=payload)
    # Replaying must be visible to the caller: without this a retry looks
    # identical to a fresh execution, which is correct but unhelpful when
    # debugging why nothing happened.
    response.headers["Idempotent-Replay"] = "true"
    return response


def remember(
    session_factory: sessionmaker[Any],
    *,
    actor: str,
    endpoint: str,
    key: str,
    status_code: int,
    body: dict[str, Any],
) -> None:
    created = _now()
    with session_factory() as session:
        store_idempotent(
            session,
            actor=actor,
            endpoint=endpoint,
            key=key,
            status_code=status_code,
            response_body=json.dumps(body, sort_keys=True),
            created_at=timestamp(created),
            expires_at=timestamp(created + IDEMPOTENCY_WINDOW),
        )
        session.commit()


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


class IngestItem(BaseModel):
    """One item an agent is pushing into an agent source (ADR 0022).

    Deliberately *not* ItemDraft, which carries `source_id`. Binding that
    directly is how the endpoint this replaces let a write-scoped caller
    inject items into any source, including ones it does not own. The field
    is absent here so the privilege line holds at the type level: there is no
    key to send, so no runtime check can be forgotten.
    """

    external_id: str = Field(min_length=1, max_length=512)
    title: str = Field(default="", max_length=1024)
    canonical_url: str = Field(default="", max_length=4096)
    published_at: datetime | None = None
    content: str = ""
    metadata: dict[str, str] = Field(default_factory=dict)
    derivation_note: str | None = Field(default=None, max_length=1024)


class IngestRequest(BaseModel):
    """Body for pushing items into an agent source.

    The cap is body-parsing hygiene rather than per-source rate limiting
    (which ADR 0022 explicitly declines to add): at 1 MiB per request an
    uncapped list admits thousands of title-only items in one call, and each
    one costs a dedupe lookup and possibly an enrichment row.
    """

    items: list[IngestItem] = Field(min_length=1, max_length=MAX_INGEST_ITEMS)


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
        rows = list_sources(session, tenant_id=TENANT, limit=limit, offset=offset)

    has_more = len(rows) > limit
    page = rows[:limit]
    return {
        "data": [_source_payload(row) for row in page],
        "next_cursor": encode_cursor(offset + limit) if has_more else None,
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
    return _source_payload(row)


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
                    "tts_audio."
                ),
            ) from exc

    with factory() as session:
        row = create_source(
            session,
            source_type=body.type.value,
            url=body.url,
            canonical_url=canonical,
            schedule_policy=policy_json,
            tenant_id=TENANT,
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

    remember(
        factory,
        actor=principal.name,
        endpoint=endpoint,
        key=key,
        status_code=201,
        body=payload,
    )
    return JSONResponse(status_code=201, content=payload)


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
    if body.sent("pipeline_chain") or body.sent("target_lang"):
        # Validate the pair against the enrichment domain before writing:
        # a chain that parse_chain rejects would gate every future item on
        # nothing and surface as a surprise at collection time.
        from app.enrichment_worker import registered_processor_ids

        try:
            parse_chain(
                json.dumps(body.pipeline_chain)
                if body.pipeline_chain is not None
                else None,
                target_lang=body.target_lang,
                known_plugins=registered_processor_ids(),
            )
        except (ValueError, KeyError) as exc:
            raise api_error(
                status_code=422,
                code="invalid_pipeline_chain",
                message=str(exc),
                suggestion=(
                    "Use a list of {plugin_id, kind, lang?, required?}; kinds: "
                    "translation, summary, tts_audio."
                ),
            ) from exc
        # json.dumps of None clears the chain; a chain forces the lang to
        # something explicit so the worker never has to guess.
        changes["pipeline_chain"] = (
            json.dumps(body.pipeline_chain) if body.pipeline_chain is not None else None
        )
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
        for column, value in changes.items():
            setattr(row, column, value)
        row.updated_at = timestamp(_now())
        session.commit()
        payload = _source_payload(row)

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
) -> JSONResponse:
    """Queue processor products for items stored before the chain was enabled.

    A write (it mutates item_enrichment and spends provider budget), so it is
    authenticated, idempotent and audited like every other write.
    """
    endpoint = f"POST /v1/sources/{source_id}:backfill-chain"
    factory = request.app.state.session_factory
    replayed = replay_or_none(factory, actor=principal.name, endpoint=endpoint, key=key)
    if replayed is not None:
        return replayed

    from adapters.repository import backfill_chain

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
            queued = backfill_chain(session, source_id=source_id)
        except ValueError as exc:
            raise api_error(
                status_code=422,
                code="invalid_chain",
                message=str(exc),
                suggestion=(
                    "Use a list of {plugin_id, kind, lang?, required?}; kinds: "
                    "translation, summary, tts_audio."
                ),
            ) from exc
        session.commit()

    payload: dict[str, Any] = {
        "source_id": source_id,
        "queued": queued,
        "audit": "recorded by the Core API",
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


@router.get("/sources/{source_id}/enrichments", operation_id="listEnrichments")
def list_source_enrichments(
    request: Request,
    source_id: str,
    status: str | None = Query(default=None),
    _: Principal = Depends(read_required),
) -> dict[str, Any]:
    """Processor-chain rows for one source: the agent's visibility into
    what the chain is doing and why an item is being held."""
    from sqlalchemy import select as _select

    from adapters.db_models import Item as _ItemRow
    from adapters.db_models import ItemEnrichment

    del _
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
        return {
            "data": [
                {
                    "id": row.id,
                    "item_id": row.item_id,
                    "plugin_id": row.plugin_id,
                    "kind": row.kind,
                    "lang": row.lang,
                    "status": row.status,
                    "error_class": row.error_class,
                    "detail": row.detail,
                    "attempt_count": row.attempt_count,
                }
                for row in rows
            ],
            "total": len(rows),
            "correlation_id": current_correlation_id(),
        }


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


# --------------------------------------------------------------------------
# A3: agent ingest (ADR 0022)
# --------------------------------------------------------------------------


def _ingest_url(item: IngestItem, *, source_url: str) -> str:
    """The canonical_url to normalise for one submitted item.

    `normalize()` calls `canonicalize_url` unconditionally and that raises on
    anything but http(s), because the value is part of item identity and the
    rest of the system only treats it as a fetchable article address. Agent
    submissions may legitimately carry no URL at all, or a non-http scheme
    (a note, a summary), and the user decided such a submission is allowed
    (ADR 0022).

    The fallback is synthesised under the item's own external_id rather than
    reusing the source's `agent://<name>` key, for two reasons: that scheme is
    itself refused by `canonicalize_url`, so it cannot be used; and a single
    shared fallback URL would make every URL-less item in a source carry the
    same link. Identity does not depend on `url` (it is
    `sha256(tenant, source_id, external_id)`), so a per-item fallback changes
    no dedupe outcome -- it only keeps the served link distinct and honest
    about where it came from.

    Handled here rather than by loosening `canonicalize_url`: that function
    serves rss and web too, and relaxing it for this one caller would change
    identity semantics for every source type to spare an agent a fallback.
    """
    supplied = item.canonical_url.strip()
    if supplied:
        try:
            return canonicalize_url(supplied)
        except ValueError:
            pass
    pod = source_url.removeprefix("agent://") or "agent"
    return f"https://agent.invalid/{pod}/{quote(item.external_id, safe='')}"


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
        drafts: list[ItemDraft] = []
        for item in body.items:
            metadata = dict(item.metadata)
            supplied = item.canonical_url.strip()
            resolved = _ingest_url(item, source_url=source_url)
            if supplied and supplied != resolved:
                # Preserved so a URL we could not use as the item's link is
                # still readable rather than silently swallowed.
                metadata.setdefault("submitted_url", supplied)
            drafts.append(
                ItemDraft(
                    source_id=source_id,
                    external_id=item.external_id,
                    title=item.title,
                    canonical_url=resolved,
                    published_at=item.published_at,
                    content=item.content,
                    metadata=metadata,
                    derivation_note=item.derivation_note,
                )
            )

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
