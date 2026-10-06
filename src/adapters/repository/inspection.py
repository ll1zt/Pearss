"""Job/run inspection and the system snapshot."""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Select, case, func, select
from sqlalchemy.orm import Session

from adapters.correlation import current
from adapters.db_models import Item as ItemRow
from adapters.db_models import ItemEnrichment
from adapters.db_models import Job as JobRow
from adapters.db_models import JobRun as JobRunRow
from adapters.db_models import Source as SourceRow
from core.alerting import DETAIL_EXCERPT_CHARS, ExhaustedSource
from core.enrichment import EnrichmentStatus
from core.enums import (
    ErrorClass,
    JobRunStatus,
    PublishStatus,
    SourceStatus,
)

# ADR 0034's minimum-sample rule, applied to a chain instead of a rate: one
# failed product is a normal per-item failure the worker retries, and alerting
# on it would make the channel noise.
EXHAUSTED_MIN_ROWS = 3


def exhausted_enrichment_sources(
    session: Session, *, tenant_id: str, min_rows: int = EXHAUSTED_MIN_ROWS
) -> list[ExhaustedSource]:
    """Sources whose pending chain is entirely failed, with their attribution.

    "Entirely" and "at least `min_rows`" are both load-bearing. A source whose
    every queued product has failed will never open its gate again without
    someone looking -- that is a different failure from a slow success rate, and
    it is invisible to one: the rate counts *runs*, and this is rows that no
    future run will fix on its own.

    The title and the representative failure (issue #152) come from rows this
    query already reads: the class and message are on the `item_enrichment` rows
    the `having` clause just counted, and the title is on the source row in the
    same join. The representative is the *newest* failed row -- the failure
    being hit now -- and `failed_count` is every failed row for the source.
    """
    source_ids = list(
        session.execute(
            select(ItemRow.source_id)
            .join(ItemEnrichment, ItemEnrichment.item_id == ItemRow.id)
            .where(
                ItemRow.tenant_id == tenant_id,
                ItemEnrichment.status.in_(
                    (
                        EnrichmentStatus.QUEUED.value,
                        EnrichmentStatus.RUNNING.value,
                        EnrichmentStatus.FAILED.value,
                    )
                ),
            )
            .group_by(ItemRow.source_id)
            .having(
                func.count() >= min_rows,
                func.sum(
                    case(
                        (ItemEnrichment.status == EnrichmentStatus.FAILED.value, 1),
                        else_=0,
                    )
                )
                == func.count(),
            )
        ).scalars()
    )
    if not source_ids:
        return []
    rows = session.execute(
        select(
            ItemRow.source_id,
            SourceRow.title,
            ItemEnrichment.error_class,
            ItemEnrichment.detail,
        )
        .join(SourceRow, SourceRow.id == ItemRow.source_id)
        .join(ItemEnrichment, ItemEnrichment.item_id == ItemRow.id)
        .where(
            ItemRow.source_id.in_(source_ids),
            ItemEnrichment.status == EnrichmentStatus.FAILED.value,
        )
        .order_by(ItemEnrichment.updated_at.desc(), ItemEnrichment.id.desc())
    ).all()
    by_source: dict[str, list[Any]] = {}
    for row in rows:
        by_source.setdefault(str(row.source_id), []).append(row)
    exhausted: list[ExhaustedSource] = []
    for source_id in sorted(by_source):
        failures = by_source[source_id]
        latest = failures[0]  # newest first: the failure being hit now
        excerpt = str(latest.detail or "")[:DETAIL_EXCERPT_CHARS]
        exhausted.append(
            ExhaustedSource(
                origin_id=source_id,
                title=str(latest.title or ""),
                failed_count=len(failures),
                error_class=latest.error_class,
                detail_excerpt=excerpt or None,
            )
        )
    return exhausted


def list_jobs(session: Session) -> list[dict[str, object]]:
    """Every declared job, with its source and last run summary.

    Ordered by id rather than recency: this feeds a listing a client pages
    through, and an unstable order makes clients re-announce entries.
    """
    rows = session.execute(select(JobRow).order_by(JobRow.id)).scalars()
    jobs: list[dict[str, object]] = []
    for row in rows:
        last = (
            session.execute(
                select(JobRunRow)
                .where(JobRunRow.job_id == row.id)
                .order_by(JobRunRow.started_at.desc().nullslast())
                .limit(1)
            )
            .scalars()
            .first()
        )
        jobs.append(
            {
                "id": row.id,
                "type": row.type,
                "source_id": row.config,
                "status": row.status,
                "priority": row.priority,
                "last_run_status": last.status if last is not None else None,
                "last_run_finished_at": last.finished_at if last is not None else None,
            }
        )
    return jobs


def list_job_runs(
    session: Session,
    *,
    job_id: str,
    limit: int = 50,
    cursor: tuple[str, str] | None = None,
) -> list[dict[str, object]]:
    """Recent runs for one job, newest first.

    Keyset-paginated on `(started_at, id)` when a cursor is given, for the same
    reason the item query is: runs are appended at the front of a newest-first
    ordering, so an offset cursor would skip or repeat rows as new runs land.
    The `id` tiebreaker matters because two runs of one job can share a
    `started_at` to the microsecond, and a cursor on the timestamp alone would
    either loop or drop the second one.
    """
    query = select(JobRunRow).where(JobRunRow.job_id == job_id)
    if cursor is not None:
        started_at, last_id = cursor
        # Strictly past the cursor in the newest-first direction. `id` is the
        # tiebreaker within one timestamp, so the comparison is per-pair.
        query = query.where(
            sa.or_(
                JobRunRow.started_at < started_at,
                sa.and_(
                    JobRunRow.started_at == started_at,
                    JobRunRow.id < last_id,
                ),
            )
        )
    rows = session.execute(
        query.order_by(
            JobRunRow.started_at.desc().nullslast(), JobRunRow.id.desc()
        ).limit(limit)
    ).scalars()
    return [
        {
            "id": row.id,
            "job_id": row.job_id,
            "status": row.status,
            "error_class": row.error_class,
            "metrics": json.loads(row.metrics or "{}"),
            "correlation_id": row.correlation_id,
            "retry_of": row.retry_of,
            "started_at": row.started_at,
            "finished_at": row.finished_at,
        }
        for row in rows
    ]


def system_snapshot(session: Session, *, tenant_id: str) -> dict[str, object]:
    """Compact system state, shaped for a conversational dashboard.

    Section 7.7 names the signals that matter: error_class distribution (a
    cluster of structure_changed means the world changed under us), how long
    pending items have been waiting, and fetch success rate. Those are what is
    reported here, rather than a generic row count.
    """

    def counts(
        statement: Select[tuple[Any, int]],
        *,
        every: type[StrEnum] | None = None,
    ) -> dict[str, int]:
        """Tally a grouped `select(value, count())` into a mapping.

        A comprehension rather than `dict(...)`: mypy cannot infer a value type
        from a Row sequence, and the empty-dict annotation it produces poisons
        every downstream use.

        `every` fills in the states that have no rows, so a consumer reads an
        explicit zero instead of a missing key. Omitting them cost us a real
        misreading: `pending` was absent while 4,576 `ready` items were present,
        and a client labelled the whole set "待处理" because the one status that
        would have contradicted that reading was not in the payload.
        """
        tallied = {
            str(key): int(total) for key, total in session.execute(statement).all()
        }
        if every is not None:
            for member in every:
                tallied.setdefault(str(member.value), 0)
        return tallied

    sources = counts(
        select(SourceRow.status, func.count())
        .where(SourceRow.tenant_id == tenant_id)
        .group_by(SourceRow.status),
        every=SourceStatus,
    )
    items = counts(
        select(ItemRow.publish_status, func.count())
        .where(ItemRow.tenant_id == tenant_id)
        .group_by(ItemRow.publish_status),
        every=PublishStatus,
    )
    runs_by_status = counts(
        select(JobRunRow.status, func.count()).group_by(JobRunRow.status),
        every=JobRunStatus,
    )
    enrichments = counts(
        select(ItemEnrichment.status, func.count())
        .where(ItemEnrichment.tenant_id == tenant_id)
        .group_by(ItemEnrichment.status),
        # Every state listed: a held item with no visible enrichment row is
        # the exact misreading the `every` discipline exists to prevent.
        every=EnrichmentStatus,
    )
    # error_class is nullable; the query drops the NULLs, and str() keeps the
    # key type uniform without asserting away the None mypy still sees.
    runs_by_error = counts(
        select(JobRunRow.error_class, func.count())
        .where(JobRunRow.error_class.is_not(None))
        .group_by(JobRunRow.error_class),
        # Every class is listed: "no blocked runs" is a fact worth stating,
        # and a missing key cannot be told apart from a query that forgot.
        every=ErrorClass,
    )
    recent = session.execute(
        select(JobRunRow.status)
        .where(JobRunRow.finished_at.is_not(None))
        .order_by(JobRunRow.finished_at.desc())
        .limit(50)
    ).scalars()
    recent_statuses = list(recent)
    succeeded = sum(1 for status in recent_statuses if status == "succeeded")

    return {
        "sources_by_status": sources,
        "items_by_publish_status": items,
        "enrichments_by_status": enrichments,
        "runs_by_status": runs_by_status,
        "runs_by_error_class": runs_by_error,
        # A rate over the last 50 finished runs rather than all time: a source
        # that broke an hour ago should move this number immediately.
        "recent_run_success_rate": (
            round(succeeded / len(recent_statuses), 3) if recent_statuses else None
        ),
        "recent_runs_considered": len(recent_statuses),
        "correlation_id": current(),
    }


def count_items(session: Session) -> int:
    return int(session.execute(select(func.count()).select_from(ItemRow)).scalar() or 0)
