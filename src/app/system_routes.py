"""Core API: /v1/jobs and /v1/system.

Read-only inspection of the scheduling layer, which the MCP tools surface as
`list_jobs`, `list_runs` and `get_system_snapshot`.

Nothing here writes. Collection is triggered through
`/v1/sources/{id}:trigger`, so there is exactly one path that starts work and
one path that records it — an inspection surface that could also kick off runs
would give the same action two entry points with different audit trails.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from adapters.correlation import current as current_correlation_id
from adapters.repository import list_job_runs, list_jobs, system_snapshot
from app.api_conventions import (
    api_error,
    decode_cursor,
    decode_keyset_cursor,
    encode_cursor,
    encode_keyset_cursor,
)
from app.auth import Principal, read_required

TENANT = "default"

router = APIRouter(prefix="/v1", tags=["core-api"])


@router.get("/jobs", operation_id="listJobs")
def read_jobs(
    request: Request,
    _: Principal = Depends(read_required),
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(
        default=None,
        max_length=64,
        description="Opaque cursor from a previous response.",
    ),
) -> dict[str, Any]:
    """Declared jobs, each with its source and last run summary.

    Jobs are the durable "collect this source" declarations. Paging is by
    offset rather than keyset because the set changes only when a source is
    added or deleted, and the order is by id, so a page cannot shift under a
    reader the way a newest-first item list can.
    """
    del _
    offset = decode_cursor(cursor)
    with request.app.state.session_factory() as session:
        rows = list_jobs(session)
    page = rows[offset : offset + limit]
    has_more = offset + limit < len(rows)
    return {
        "data": page,
        "total": len(rows),
        "next_cursor": encode_cursor(offset + limit) if has_more else None,
        "correlation_id": current_correlation_id(),
    }


@router.get("/jobs/{job_id}/runs", operation_id="listJobRuns")
def read_job_runs(
    request: Request,
    job_id: str,
    _: Principal = Depends(read_required),
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(
        default=None,
        max_length=256,
        description="Opaque cursor from a previous response.",
    ),
) -> dict[str, Any]:
    """Recent runs for one job, newest first.

    `error_class` is present only on failures, per the JobRun state machine,
    which is what makes a cluster of `structure_changed` visible as the signal
    that a source's page shape moved.

    A run history is append-at-the-front, so this is keyset-paginated: page two
    must not skip a run that started while page one was being read.
    """
    del _
    parsed = decode_keyset_cursor(cursor)
    with request.app.state.session_factory() as session:
        # One beyond the page, so "is there a next page" is answered by the
        # query rather than by a second count.
        rows = list_job_runs(session, job_id=job_id, limit=limit + 1, cursor=parsed)
    if not rows:
        # An unknown job and a job that has never run are indistinguishable
        # here, so say which rather than returning an empty list and leaving the
        # caller to guess whether the id was wrong.
        with request.app.state.session_factory() as session:
            known = any(job["id"] == job_id for job in list_jobs(session))
        if not known:
            raise api_error(
                status_code=404,
                code="job_not_found",
                message=f"No job with id {job_id!r}.",
                suggestion="List jobs to find the intended id.",
            )
    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = None
    if has_more and page:
        last = page[-1]
        next_cursor = encode_keyset_cursor(str(last["started_at"]), str(last["id"]))
    return {
        "data": page,
        "job_id": job_id,
        "next_cursor": next_cursor,
        "correlation_id": current_correlation_id(),
    }


@router.get("/system/snapshot", operation_id="getSystemSnapshot")
def read_snapshot(
    request: Request,
    _: Principal = Depends(read_required),
) -> dict[str, Any]:
    """Compact system state, sized to be pasted into a conversation.

    Reports the decay signals architecture section 7.7 names — error
    distribution, the pending backlog, recent success rate — rather than a
    wall of counters, because the point is noticing that something is going
    wrong, not counting rows.
    """
    del _
    with request.app.state.session_factory() as session:
        return system_snapshot(session, tenant_id=TENANT)
