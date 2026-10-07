"""Core API: /v1/subscriptions.

ADR 0046 D-C: a channel is a subscription. These endpoints serve the two halves
apart -- the origin's facts and the subscription's policy -- where
`/v1/sources` (deprecated, issue #366) served them as one row.

Creating and changing a subscription is still `POST`/`PATCH /v1/sources`, which
write the same two tables through the same façade. The *destructive* verb is the
one that moved here first, because "what does DELETE mean" was the decision that
needed making (issue #373, ADR 0063): `DELETE /v1/subscriptions/{id}:unsubscribe`
stops collecting and keeps the content, and `DELETE /v1/origins/{id}:purge`
(see `origins_routes`) removes content for a stated reason. The old
`DELETE /v1/sources/{id}` keeps its URL and changes its meaning to unsubscribe.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from adapters.correlation import current as current_correlation_id
from adapters.repository import (
    SourceView,
    create_subscription_for_origin,
    get_origin,
    get_source,
    list_sources,
    parse_chain,
    timestamp,
)
from adapters.repository import (
    unsubscribe as repository_unsubscribe,
)
from app.api_conventions import (
    _now,
    api_error,
    decode_cursor,
    encode_cursor,
    idempotency_key,
    remember,
    replay_or_none,
)
from app.auth import Principal, read_required, write_required
from core.enums import TrustLevel
from core.folders import DEFAULT_FOLDER, normalize_folder
from core.models import schedule_policy_from_json, schedule_policy_to_json
from core.tenancy import SINGLE_TENANT

router = APIRouter(prefix="/v1", tags=["core-api"])


def _subscription_payload(row: SourceView) -> dict[str, Any]:
    """One channel, split into the two halves it is made of.

    `id` is the subscription and `origin.id` the facts. They are equal while the
    1:1 rule holds, and the shape does not have to change when it stops -- which
    is why the halves are nested rather than flattened back together.
    """
    return {
        "id": row.subscription_id,
        "origin": {
            "id": row.id,
            "type": row.type,
            "url": row.url,
            "canonical_url": row.canonical_url,
            "title": row.title,
            "parser_plugin_id": row.parser_plugin_id,
            "seen_frontier": row.seen_frontier,
            "floor_cursor": row.floor_cursor,
        },
        "policy": {
            "schedule_policy": json.loads(row.schedule_policy),
            "pipeline_chain": (
                json.loads(row.pipeline_chain) if row.pipeline_chain else None
            ),
            "target_lang": row.target_lang,
            "folder": row.folder,
            "status": row.status,
            "trust_level": row.trust_level,
        },
        "tenant_id": row.tenant_id,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


@router.get("/subscriptions", operation_id="listSubscriptions")
def read_subscriptions(
    request: Request,
    _: Principal = Depends(read_required),
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None),
) -> dict[str, Any]:
    """List subscriptions, oldest page first by creation order.

    The same offset paging as the deprecated `/v1/sources`, and for the same
    reason: channels are bounded (an operator's subscriptions, tens not
    millions), and new ones append at the end of this ordering, so a row
    arriving mid-walk shifts nothing already read. One page plus an opaque
    cursor means the response does not grow with the deployment.
    """
    del _
    offset = decode_cursor(cursor)
    with request.app.state.session_factory() as session:
        rows = list_sources(
            session, tenant_id=SINGLE_TENANT, limit=limit, offset=offset
        )

    has_more = len(rows) > limit
    page = rows[:limit]
    return {
        "data": [_subscription_payload(row) for row in page],
        "next_cursor": encode_cursor(offset + limit) if has_more else None,
        "correlation_id": current_correlation_id(),
    }


@router.get("/subscriptions/{subscription_id}", operation_id="getSubscription")
def read_subscription(
    request: Request,
    subscription_id: str,
    _: Principal = Depends(read_required),
) -> dict[str, Any]:
    """One subscription, with its origin's facts beside its policy."""
    del _
    with request.app.state.session_factory() as session:
        row = get_source(session, subscription_id)
    if row is None:
        raise api_error(
            status_code=404,
            code="subscription_not_found",
            message=f"No subscription with id {subscription_id!r}.",
            suggestion="List subscriptions to find the intended id.",
        )
    return {**_subscription_payload(row), "correlation_id": current_correlation_id()}


class SubscriptionCreate(BaseModel):
    """Body for adding a subscription to an existing origin (ADR 0061).

    The origin's facts already exist, so only policy is accepted. Two
    subscriptions over one origin differ by exactly these fields -- which is
    what "one origin, two chains" means.
    """

    schedule_policy: dict[str, Any]
    folder: str = Field(default=DEFAULT_FOLDER, max_length=256)
    pipeline_chain: list[dict[str, Any]] | None = None
    target_lang: str | None = None
    trust_level: TrustLevel = TrustLevel.STANDARD

    @field_validator("folder")
    @classmethod
    def _valid_folder(cls, value: str) -> str:
        return normalize_folder(value)


@router.post(
    "/origins/{origin_id}/subscriptions",
    operation_id="addSubscription",
    status_code=201,
)
def add_subscription(
    request: Request,
    origin_id: str,
    body: SubscriptionCreate,
    principal: Principal = Depends(write_required),
    key: str = Depends(idempotency_key),
) -> JSONResponse:
    """Add a subscription to an origin, idempotently.

    The pair (origin, folder) is the identity, so adding the same pair twice
    returns the subscription that already exists rather than a second one --
    the same rule `POST /v1/sources` applies to a URL.
    """
    endpoint = f"POST /v1/origins/{origin_id}/subscriptions"
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
            code="invalid_schedule_policy",
            message=str(exc),
            suggestion=(
                "Use {type: fixed, interval: '30m'} or "
                "{type: adaptive, min: '15m', max: '24h'}."
            ),
        ) from exc

    chain_json = (
        json.dumps(body.pipeline_chain) if body.pipeline_chain is not None else None
    )
    from app.enrichment_worker import registered_processor_ids

    try:
        parse_chain(
            chain_json,
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
                "translation, summary, fulltext, tts_audio."
            ),
        ) from exc

    at = timestamp(_now())
    with factory() as session:
        if get_origin(session, origin_id) is None:
            raise api_error(
                status_code=404,
                code="origin_not_found",
                message=f"No origin with id {origin_id!r}.",
                suggestion="List subscriptions to find the origin you meant.",
            )
        created = create_subscription_for_origin(
            session,
            origin_id=origin_id,
            tenant_id=SINGLE_TENANT,
            schedule_policy=policy_json,
            at=at,
            folder=body.folder,
            pipeline_chain=chain_json,
            target_lang=body.target_lang or None,
            trust_level=body.trust_level.value,
        )
        subscription_id = created.id
        session.commit()
        view = get_source(session, subscription_id)

    if view is None:  # unreachable: the row was just created in this session
        raise api_error(
            status_code=500,
            code="subscription_not_readable",
            message="The subscription was created but could not be read back.",
            suggestion="Retry the request; the write is idempotent.",
        )
    payload = {
        **_subscription_payload(view),
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


@router.delete(
    "/subscriptions/{subscription_id}:unsubscribe",
    operation_id="unsubscribeSubscription",
    status_code=200,
)
def unsubscribe_subscription(
    request: Request,
    subscription_id: str,
    principal: Principal = Depends(write_required),
    key: str = Depends(idempotency_key),
) -> JSONResponse:
    """Stop collecting a subscription, keeping everything it collected.

    The counterpart of `:purge`, and what a client that wants to stop reading a
    feed means: no row is removed, no body file is unlinked, and no reason is
    asked for -- there is nothing to justify. Re-subscribing the same source
    finds this subscription again and re-activates it, so the items it already
    had are visible again under the same ids.

    Idempotent, including by `Idempotency-Key`: a replay returns the first
    answer, and a fresh call on something already stopped answers
    `subscriptions: 0` rather than inventing a second effect.
    """
    endpoint = f"DELETE /v1/subscriptions/{subscription_id}:unsubscribe"
    factory = request.app.state.session_factory
    replayed = replay_or_none(factory, actor=principal.name, endpoint=endpoint, key=key)
    if replayed is not None:
        return replayed

    with factory() as session:
        result = repository_unsubscribe(session, subscription_id)
        session.commit()

    payload: dict[str, Any] = {
        "subscription_id": result.subscription_id,
        "subscriptions": 1 if result.unsubscribed else 0,
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
