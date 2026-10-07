"""Core API: /v1/origins.

The origin is the *fact* half of a channel (ADR 0046 D-C, ADR 0061): what was
collected, as opposed to the policy that decides how it is collected. So this is
where the one irreversible verb lives -- a purge removes the facts themselves --
while `DELETE /v1/subscriptions/{id}:unsubscribe` (`subscriptions_routes`) is
the reversible answer for the same intent.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from adapters.correlation import current as current_correlation_id
from adapters.repository import purge_origin as repository_purge_origin
from app.api_conventions import api_error, idempotency_key, remember, replay_or_none
from app.auth import Principal, write_required

router = APIRouter(prefix="/v1", tags=["core-api"])


@router.delete(
    "/origins/{origin_id}:purge", operation_id="purgeOrigin", status_code=200
)
def purge_origin(
    request: Request,
    origin_id: str,
    reason: str | None = Query(default=None),
    principal: Principal = Depends(write_required),
    key: str = Depends(idempotency_key),
) -> JSONResponse:
    """Delete an origin and everything it collected, for a stated reason.

    Irreversible, and the only endpoint that removes content: the items, their
    bodies, their read state and processor products, the fetch records, the
    collect job and the origin row all go. Unsubscribing is the other answer,
    and the one most callers mean -- it keeps all of this and stops collecting.

    `reason` is required. An irreversible act whose justification is unknown
    afterwards cannot be told apart from a mistake, so the request is refused
    (422 `reason_required`) before anything is touched, and the reason is
    recorded in the audit row of the request that carried it.

    An id that is already gone answers 200 with `sources: 0` rather than 404,
    for the same reason `DELETE /v1/sources/{id}` always did: a client
    retrying a request whose response was lost cannot tell "it was here and I
    removed it" from "it was never here", and after the first call those are
    the same fact.
    """
    if reason is None or not reason.strip():
        raise api_error(
            status_code=422,
            code="reason_required",
            message="A purge must state why it is destroying content.",
            suggestion=(
                "Pass ?reason=<why>. To stop collecting without deleting "
                "anything, use DELETE /v1/subscriptions/{id}:unsubscribe."
            ),
        )

    endpoint = f"DELETE /v1/origins/{origin_id}:purge"
    factory = request.app.state.session_factory
    replayed = replay_or_none(factory, actor=principal.name, endpoint=endpoint, key=key)
    if replayed is not None:
        return replayed

    # The reason rides with the request so the audit middleware records it: the
    # row is the only place it outlives the response.
    request.state.audit_args = {"reason": reason}

    with factory() as session:
        result = repository_purge_origin(session, origin_id, reason=reason)
        session.commit()

    # Files after the commit: an unlink cannot be rolled back, and rows can.
    removed = result.reclaim.reclaim()

    payload: dict[str, Any] = {
        "origin_id": origin_id,
        "reason": reason,
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
