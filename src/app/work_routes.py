"""Core API: /v1/works -- a command in, a handle out (ADR 0046).

The shape this endpoint exists to enforce: **the response is O(1)**. A caller
submits a batch (targets + the kind's params) and receives a handle, the work's
status, and the folded counts. The per-target outcomes are a *separate* read
(`GET /v1/works/{id}/units`, issue #189), which is what makes a client timeout
harmless: the work keeps going and the ledger keeps the answers.

The endpoint it replaces answered 568 addresses with 568 per-address results in
one HTTP response. When the client gave up, the server finished the work and the
entire report was lost -- measured on 2026-09-30 (the journal showed the stores
continuing after the caller had timed out).

Submitting the same command while the first is still queued or running returns
*that* work rather than a second batch: a caller re-issuing a request after a
timeout must not double the work. The arbitration is the database's (a partial
unique index over the live statuses), not a check-then-insert here, because the
race this prevents is exactly the one a check-then-insert loses.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from adapters.correlation import current as current_correlation_id
from adapters.repository import (
    counts_for_work,
    create_work,
    live_work_by_fingerprint,
    timestamp,
    units_for_work,
    work_by_id,
)
from app.api_conventions import (
    api_error,
    decode_target_cursor,
    encode_target_cursor,
    idempotency_key,
    remember,
    replay_or_none,
)
from app.auth import Principal, read_required, write_required
from core.enums import ErrorClass, UnitOutcome
from core.tenancy import SINGLE_TENANT
from core.work import UnitRecord, command_fingerprint

router = APIRouter(prefix="/v1", tags=["core-api"])

# One request's worth of work. The ceiling exists so a single submission cannot
# ask for unbounded work; a caller with more targets sends several commands, and
# each one is paced on its own.
MAX_TARGETS = 1000

# The kinds the host can execute. An unknown kind is refused at submit time
# rather than left queued forever: a typo must not create a work nothing will
# ever run.
WORK_KINDS = frozenset({"fetch", "ingest", "enrich", "retry", "delete", "subscribe"})


class WorkSubmit(BaseModel):
    """Body for submitting a work."""

    kind: str = Field(min_length=1, max_length=32)
    targets: list[str] = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)


@router.post("/works", operation_id="submitWork", status_code=202)
def submit_work(
    request: Request,
    body: WorkSubmit,
    principal: Principal = Depends(write_required),
    key: str = Depends(idempotency_key),
) -> JSONResponse:
    """Accept a batch of work; answer with a handle, not with a report.

    Answers 202 rather than 200 because the work has not happened yet: it is
    queued, and `Location` names where its status lives.

    Every kind this API accepts has an executor, so a submitted work is claimed
    and run. A kind a *future* deployment does not implement is still recorded
    and left `queued` rather than refused -- the command is what the caller
    asked for, and `GET /v1/works/{id}` is where that state is visible.
    """
    endpoint = "POST /v1/works"
    if len(body.targets) > MAX_TARGETS:
        raise api_error(
            status_code=422,
            code="work_too_large",
            message=(
                f"A work may carry at most {MAX_TARGETS} targets; this one has "
                f"{len(body.targets)}."
            ),
            suggestion=(
                f"Split the list into batches of {MAX_TARGETS} targets and "
                "submit one command per batch."
            ),
        )

    kind = body.kind.strip().lower()
    if kind not in WORK_KINDS:
        raise api_error(
            status_code=422,
            code="unknown_work_kind",
            message=f"Unknown work kind {body.kind!r}.",
            suggestion=f"Use one of: {', '.join(sorted(WORK_KINDS))}.",
        )

    factory = request.app.state.session_factory
    replayed = replay_or_none(factory, actor=principal.name, endpoint=endpoint, key=key)
    if replayed is not None:
        return replayed

    fingerprint = command_fingerprint(
        kind=kind, targets=body.targets, params=body.params
    )
    with factory() as session:
        work = live_work_by_fingerprint(session, fingerprint)
        if work is None:
            try:
                work = create_work(
                    session,
                    kind=kind,
                    targets=body.targets,
                    params=body.params,
                    fingerprint=fingerprint,
                    tenant_id=SINGLE_TENANT,
                    correlation_id=current_correlation_id(),
                )
                session.commit()
            except IntegrityError:
                # Two submissions of the same command raced and the index picked
                # one. Answering with the winner is the whole point of the
                # command-value fingerprint: the caller asked for one batch.
                session.rollback()
                work = live_work_by_fingerprint(session, fingerprint)
                if work is None:
                    # The winning work finished between the collision and this
                    # read, so the command is new again. Submitting it once more
                    # is correct; a second collision here would mean a third
                    # submission in the same instant, which is a genuine error.
                    work = create_work(
                        session,
                        kind=kind,
                        targets=body.targets,
                        params=body.params,
                        fingerprint=fingerprint,
                        tenant_id=SINGLE_TENANT,
                        correlation_id=current_correlation_id(),
                    )
                    session.commit()
        counts = counts_for_work(session, work.id)
        work_id, status = work.id, work.status.value

    payload: dict[str, Any] = {
        "work_id": work_id,
        "kind": kind,
        "status": status,
        "counts": counts,
        "correlation_id": current_correlation_id(),
    }
    remember(
        factory,
        actor=principal.name,
        endpoint=endpoint,
        key=key,
        status_code=202,
        body=payload,
    )
    return JSONResponse(
        status_code=202,
        content=payload,
        headers={"Location": f"/v1/works/{work_id}"},
    )


@router.get("/works/{work_id}", operation_id="getWork")
def read_work(
    request: Request,
    work_id: str,
    _: Principal = Depends(read_required),
) -> dict[str, Any]:
    """One work: its status, its command, and the folded counts.

    Deliberately **not** the target list: a 1000-target work must not put 1000
    strings into a response that is meant to be O(1), and the caller already
    has what it submitted. The enumeration is `/units`, which pages.
    """
    del _
    with request.app.state.session_factory() as session:
        work = work_by_id(session, work_id)
        if work is None:
            raise _work_not_found(work_id)
        counts = counts_for_work(session, work_id)
    return {
        "work_id": work.id,
        "kind": work.kind,
        "status": work.status.value,
        "params": work.params,
        "retry_of": work.retry_of,
        "created_at": timestamp(work.created_at),
        "finished_at": (
            None if work.finished_at is None else timestamp(work.finished_at)
        ),
        "counts": counts,
        "correlation_id": current_correlation_id(),
    }


@router.get("/works/{work_id}/units", operation_id="listWorkUnits")
def read_work_units(
    request: Request,
    work_id: str,
    _: Principal = Depends(read_required),
    outcome: str | None = Query(
        default=None,
        description="Filter to one outcome (ok, skipped, rejected, failed).",
    ),
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(
        default=None,
        max_length=512,
        description="Opaque cursor from a previous response.",
    ),
) -> dict[str, Any]:
    """The per-target ledger: what happened to each target, paged.

    This is the read that replaces the report the old endpoint returned inline.
    Because it is a query rather than a response, it survives the client timing
    out, it can be re-read later, and `outcome=failed` answers "what do I need
    to re-send?" in one call -- a question that had no answer once the response
    carrying it was lost.
    """
    del _
    parsed_outcome: UnitOutcome | None = None
    if outcome is not None:
        try:
            parsed_outcome = UnitOutcome(outcome)
        except ValueError:
            raise api_error(
                status_code=422,
                code="unknown_outcome",
                message=f"Unknown outcome {outcome!r}.",
                suggestion=(
                    "Use one of: "
                    + ", ".join(member.value for member in UnitOutcome)
                    + "."
                ),
            ) from None
    after_target = decode_target_cursor(cursor)
    with request.app.state.session_factory() as session:
        if work_by_id(session, work_id) is None:
            raise _work_not_found(work_id)
        # One beyond the page, so "is there a next page" is answered by the
        # query rather than by a second count.
        rows = units_for_work(
            session,
            work_id,
            outcome=parsed_outcome,
            after_target=after_target,
            limit=limit + 1,
        )
    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = encode_target_cursor(page[-1].target) if has_more and page else None
    return {
        "data": [_unit_payload(row) for row in page],
        "work_id": work_id,
        "next_cursor": next_cursor,
        "correlation_id": current_correlation_id(),
    }


def _unit_payload(row: UnitRecord) -> dict[str, Any]:
    """One ledger row as the surface form.

    `target` is always present and `error_class` uses the one taxonomy: a row
    that says nothing about the target would be a row nobody can act on.
    """
    error_class: ErrorClass | None = row.error_class
    return {
        "target": row.target,
        "outcome": row.outcome.value,
        "error_class": None if error_class is None else error_class.value,
        "detail": row.detail,
        "ref": row.ref,
        "at": timestamp(row.at),
    }


def _work_not_found(work_id: str) -> Exception:
    return api_error(
        status_code=404,
        code="work_not_found",
        message=f"No work with id {work_id!r}.",
        suggestion="The id comes from the submission's Location header.",
    )
