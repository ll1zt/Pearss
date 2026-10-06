"""Core API: /v1/subscriptions.

ADR 0046 D-C: a channel is a subscription. These endpoints serve the two halves
apart -- the origin's facts and the subscription's policy -- where
`/v1/sources` (deprecated, issue #366) served them as one row.

Read-only for now, and deliberately: creating and changing a subscription is
still `POST`/`PATCH /v1/sources`, which write the same two tables through the
same façade. The write surface moves here with the unsubscribe/purge split
(issue #373), because "what does DELETE mean" is the decision that split makes;
adding the verb first would mean documenting the old answer under a new name.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from adapters.correlation import current as current_correlation_id
from adapters.repository import SourceView, get_source, list_sources
from app.api_conventions import api_error, decode_cursor, encode_cursor
from app.auth import Principal, read_required
from core.tenancy import SINGLE_TENANT

router = APIRouter(prefix="/v1", tags=["core-api"])


def _subscription_payload(row: SourceView) -> dict[str, Any]:
    """One channel, split into the two halves it is made of.

    `id` is the subscription and `origin.id` the facts. They are equal while the
    1:1 rule holds, and the shape does not have to change when it stops -- which
    is why the halves are nested rather than flattened back together.
    """
    return {
        "id": row.id,
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
