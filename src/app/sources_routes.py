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
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session, sessionmaker

from adapters.correlation import current as current_correlation_id
from adapters.db_models import Source as SourceRow
from adapters.repository import (
    create_source,
    find_idempotent,
    get_source,
    list_sources,
    store_idempotent,
)
from app.auth import Principal, read_required, write_required
from app.scheduler import collect_now
from core.enums import SourceStatus, SourceType, TrustLevel
from core.models import schedule_policy_from_json, schedule_policy_to_json
from core.normalize import source_canonical_key

LOGGER = logging.getLogger(__name__)

TENANT = "default"

# How long a stored response is replayed. Long enough to cover a client that
# retries after a timeout and then again a few seconds later; short enough that
# the table does not grow without bound.
IDEMPOTENCY_WINDOW = timedelta(days=7)

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
) -> HTTPException:
    """Build an HTTPException carrying the documented error envelope.

    The correlation id is embedded so a client can quote one value when
    reporting a problem, and `code` is stable while `message` is allowed to
    change — clients branch on the former.
    """
    return HTTPException(
        status_code=status_code,
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
    return int(digits)


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


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def replay_or_none(
    session_factory: sessionmaker[Any],
    *,
    actor: str,
    endpoint: str,
    key: str,
) -> JSONResponse | None:
    """Return a stored response for this write, if one exists."""
    now = _iso(_now())
    with session_factory() as session:
        stored = find_idempotent(
            session, actor=actor, endpoint=endpoint, key=key, now=now
        )
        if stored is None:
            return None
        payload: dict[str, Any] = json.loads(stored.response_body)
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
            created_at=_iso(created),
            expires_at=_iso(created + IDEMPOTENCY_WINDOW),
        )
        session.commit()


# --------------------------------------------------------------------------
# Request models
# --------------------------------------------------------------------------


class SourceCreate(BaseModel):
    """Body for creating a source."""

    url: str = Field(min_length=1, max_length=4096)
    type: SourceType = SourceType.RSS
    schedule_policy: dict[str, Any]
    trust_level: TrustLevel = TrustLevel.STANDARD
    parser_plugin_id: str | None = Field(default=None, max_length=128)

    @field_validator("url")
    @classmethod
    def _valid_url(cls, value: str, info: Any) -> str:
        # Validating against the dedupe key rather than a generic URL parser:
        # rss and web sources must carry a canonicalisable URL, while agent
        # sources legitimately carry a synthetic `agent://<name>` instead.
        source_type = info.data.get("type", SourceType.RSS)
        kind = getattr(source_type, "value", source_type)
        try:
            source_canonical_key(source_type=str(kind), url=value)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        return value


class SourcePatch(BaseModel):
    """Body for updating a source. Absent fields are left alone."""

    schedule_policy: dict[str, Any] | None = None
    status: SourceStatus | None = None
    trust_level: TrustLevel | None = None


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
        "parser_plugin_id": row.parser_plugin_id,
        "schedule_policy": json.loads(row.schedule_policy),
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
    """List sources, newest page first by creation order.

    Cursor rather than offset: an offset shifts when rows are inserted, so a
    client paging through a changing collection silently skips or repeats.
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
            at=_iso(_now()),
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

    changes: dict[str, str] = {}
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

    if not changes:
        raise api_error(
            status_code=400,
            code="empty_patch",
            message="The request body changed nothing.",
            suggestion="Supply at least one of schedule_policy, status, trust_level.",
        )

    with factory() as session:
        row = _require_source(session, source_id)
        for column, value in changes.items():
            setattr(row, column, value)
        row.updated_at = _iso(_now())
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


@router.delete("/sources/{source_id}", operation_id="deleteSource")
def delete_source(
    request: Request,
    source_id: str,
    principal: Principal = Depends(write_required),
    key: str = Depends(idempotency_key),
) -> JSONResponse:
    """Soft-delete a source.

    Soft, because docs/domain/data-model.md forbids deleting a source's items:
    they are published, and their GUIDs are frozen. Removing the source would
    orphan content a client still has cached.
    """
    endpoint = f"DELETE /v1/sources/{source_id}"
    factory = request.app.state.session_factory
    replayed = replay_or_none(factory, actor=principal.name, endpoint=endpoint, key=key)
    if replayed is not None:
        return replayed

    with factory() as session:
        row = _require_source(session, source_id)
        row.status = SourceStatus.DELETED.value
        row.updated_at = _iso(_now())
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
            suggestion="Check list_runs for the outcome of the in-flight run.",
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
