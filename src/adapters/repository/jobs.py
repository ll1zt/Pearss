"""Collect jobs and job runs: creation, claim, finish."""

from __future__ import annotations

import json
import logging
from typing import Any, cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.orm import Session

from adapters.db_models import Job as JobRow
from adapters.db_models import JobRun as JobRunRow
from core.enums import (
    ErrorClass,
    JobRunStatus,
)
from core.job_state import JobRunContext, JobRunEvent, transition

LOGGER = logging.getLogger(__name__)

COLLECT_JOB_TYPE = "collect"


def ensure_collect_job(
    session: Session, *, source_id: str, tenant_id: str, at: str
) -> str:
    """Create the collection job for a source, or return the existing id.

    One job per source: the job is the durable declaration that this source is
    collected, while a job_run is one execution of that declaration. Keeping
    them separate is what lets a future job type (feed regeneration, cleanup)
    reuse the run machinery.

    Idempotent because it is called from source creation, which is itself
    idempotent.
    """
    existing = (
        session.execute(
            select(JobRow.id).where(
                JobRow.config == source_id, JobRow.type == COLLECT_JOB_TYPE
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        return existing

    job_id = f"job-{source_id}"
    session.add(
        JobRow(
            id=job_id,
            type=COLLECT_JOB_TYPE,
            # The schedule is not stored here on purpose: when the next run is
            # due is derived from the source's policy and history, so this row
            # declares *what* is collected, not *when*.
            schedule=None,
            priority=0,
            status="enabled",
            config=source_id,
            created_at=at,
            updated_at=at,
        )
    )
    session.flush()
    return job_id


def job_for_source(session: Session, source_id: str) -> JobRow | None:
    return (
        session.execute(
            select(JobRow).where(
                JobRow.type == COLLECT_JOB_TYPE, JobRow.config == source_id
            )
        )
        .scalars()
        .first()
    )


def create_job_run(
    session: Session,
    *,
    run_id: str,
    job_id: str,
    correlation_id: str,
    retry_of: str | None = None,
    at: str,
) -> str:
    """Open a queued run. A retry creates a new row rather than reopening one.

    Raises IntegrityError when the job already has a queued or running run:
    the unique partial index over active runs is what enforces one collection
    per job, since every caller creates its own run id and cannot be told
    apart by a conditional update on its own row.
    """
    session.add(
        JobRunRow(
            id=run_id,
            job_id=job_id,
            status=JobRunStatus.QUEUED.value,
            metrics="{}",
            correlation_id=correlation_id,
            retry_of=retry_of,
            started_at=None,
            finished_at=None,
        )
    )
    session.flush()
    return run_id


def claim_job_run(session: Session, *, run_id: str, at: str) -> bool:
    """Move queued -> running with a conditional update.

    Cross-callers mutual exclusion lives in the schema (the unique partial
    index over active runs per job), not here: each caller creates its own
    run id, so this update's condition guards the row's own lifecycle rather
    than arbitrating between callers.
    """
    result = cast(
        "CursorResult[Any]",
        session.execute(
            update(JobRunRow)
            .where(
                JobRunRow.id == run_id,
                JobRunRow.status == JobRunStatus.QUEUED.value,
            )
            .values(status=JobRunStatus.RUNNING.value, started_at=at)
        ),
    )
    return (result.rowcount or 0) == 1


def finish_job_run(
    session: Session,
    *,
    run_id: str,
    event: JobRunEvent,
    correlation_id: str,
    error_class: ErrorClass | None = None,
    metrics: dict[str, object] | None = None,
    at: str,
) -> JobRunStatus:
    """Close a run, routing the transition through the core state machine.

    The status is never assigned directly: the machine is what guarantees a
    failed run carries an error_class and a succeeded one does not, which is
    what keeps the taxonomy queryable and the backoff computable.

    The conditional update also makes the terminal write single-shot — a run
    already finished by another worker cannot be overwritten.
    """
    row = session.get(JobRunRow, run_id)
    if row is None:
        raise ValueError(f"no job run with id {run_id!r}")

    current = JobRunStatus(row.status)
    result = transition(current, event, JobRunContext(error_class=error_class))

    written = cast(
        "CursorResult[Any]",
        session.execute(
            update(JobRunRow)
            .where(
                JobRunRow.id == run_id,
                JobRunRow.status == current.value,
            )
            .values(
                status=result.status.value,
                error_class=result.error_class.value if result.error_class else None,
                metrics=json.dumps(metrics or {}, sort_keys=True),
                finished_at=at,
            )
        ),
    )
    if (written.rowcount or 0) == 0:
        LOGGER.warning("job run %s was already terminalised by another worker", run_id)
    del correlation_id  # already fixed at creation; kept for call-site clarity
    return result.status
