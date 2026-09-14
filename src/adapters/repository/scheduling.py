"""Inputs for the derived schedule, plus maintenance sweeps."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, delete, or_, select, update
from sqlalchemy.orm import Session

from adapters.db_models import AuditLog
from adapters.db_models import Item as ItemRow
from adapters.db_models import JobRun as JobRunRow
from adapters.db_models import Source as SourceRow
from adapters.repository._shared import _parse
from adapters.timestamps import fixed_width_utc as timestamp
from core.enums import (
    ErrorClass,
    JobRunStatus,
    SourceStatus,
)
from core.normalize import source_display_label
from core.paging import WalkState
from core.scheduling import observed_gaps


@dataclass(frozen=True, slots=True, kw_only=True)
class PagingState(WalkState):
    """Where the last walk of this source's origin left off.

    The stored view of core.paging.WalkState — the same fields, so a
    persisted state passes anywhere a walk state is expected.
    """


def paging_state(session: Session, *, source_id: str) -> PagingState:
    row = session.get(SourceRow, source_id)
    if row is None:
        return PagingState()
    return PagingState(
        seen_frontier=row.seen_frontier,
        floor_cursor=row.floor_cursor,
    )


def save_paging_state(
    session: Session,
    *,
    source_id: str,
    newest_id: int | None,
    floor_candidate: str | None,
) -> None:
    """Persist where this walk ended.

    Two anchors, each with its own direction rule:

    * **frontier** (`newest_id`) only moves forward. A re-walk of old history
      reports ids below it, and letting those win would drag the anchor
      backwards so every later cycle re-reads content it already has.
    * **floor** (`floor_candidate`) is the interval's lower end, passed only
      when a walk ended on budget without crossing the frontier. It is set
      once by the first walk -- that is what gives the interval its depth --
      and afterwards the frontier is the terminator, so a later candidate
      normally does not arise. When one does (a first walk that failed and is
      retried), it is accepted: cursors are opaque, so the host cannot compare
      positions and a candidate is the walk's own statement about where it
      stopped. It is never cleared.
    """

    row = session.get(SourceRow, source_id)
    if row is None:
        return
    if newest_id is not None and (
        row.seen_frontier is None or newest_id > row.seen_frontier
    ):
        row.seen_frontier = newest_id

    if floor_candidate:
        row.floor_cursor = floor_candidate
    row.updated_at = timestamp(datetime.now(UTC))


def source_display_labels(session: Session, source_ids: list[str]) -> dict[str, str]:
    """Display labels for the given sources, keyed by source id.

    Batched because a contents request can name a hundred items across several
    sources, and resolving a name per item would turn one page into one hundred
    queries. Falls back through core.source_display_label so a source never
    collected still gets something better than its hex id.
    """
    wanted = [source_id for source_id in set(source_ids) if source_id]
    if not wanted:
        return {}
    rows = session.execute(
        select(
            SourceRow.id, SourceRow.title, SourceRow.url, SourceRow.canonical_url
        ).where(SourceRow.id.in_(wanted))
    ).all()
    return {
        row.id: source_display_label(
            title=row.title,
            url=row.url,
            canonical_url=row.canonical_url,
            source_id=row.id,
        )
        for row in rows
    }


def set_source_title_if_absent(
    session: Session, *, source_id: str, title: str | None
) -> bool:
    """Record the name the origin gives itself, when we have none.

    Conditional on purpose. The title arrives from the feed or page, so an
    operator who renamed a source through the API would otherwise have that
    choice overwritten by the next collection; and a source whose origin
    changes its name keeps the one clients already recognise.

    Returns whether the row changed, so the caller can decide whether a commit
    is owed.
    """
    cleaned = (title or "").strip()
    if not cleaned:
        return False
    result = cast(
        "CursorResult[Any]",
        session.execute(
            update(SourceRow)
            .where(
                SourceRow.id == source_id,
                or_(SourceRow.title.is_(None), SourceRow.title == ""),
            )
            .values(title=cleaned, updated_at=timestamp(datetime.now(UTC)))
        ),
    )
    return (result.rowcount or 0) == 1


def update_gaps_for_source(
    session: Session, *, source_id: str, limit: int = 40
) -> list[float]:
    """Seconds between this source's recent publication times.

    Section 4 says to derive the update cadence from RawFetch history, but a
    RawFetch records when *we* fetched, not when the source *published*. Using
    the items' own publication timestamps measures the thing the policy
    actually wants — how often the source produces content — and is not
    distorted by our own fetch cadence, which is the feedback loop a
    fetch-derived interval would create.

    Returns the most recent `limit` gaps, newest first, or an empty list when
    there are too few items to measure anything.
    """
    stamps = list(
        session.execute(
            select(ItemRow.published_at)
            .where(
                ItemRow.source_id == source_id,
                ItemRow.published_at.is_not(None),
            )
            .order_by(ItemRow.published_at.desc())
            .limit(limit)
        ).scalars()
    )
    moments = [parsed for parsed in (_parse(value) for value in stamps) if parsed]
    if len(moments) < 2:
        return []
    ordered = sorted(moments)
    return observed_gaps([moment.timestamp() for moment in ordered])


def prune_audit_log(session: Session, *, older_than: datetime) -> int:
    """Delete audit rows recorded before `older_than`.

    ADR 0016: AuditLog was an append-forever fact source while the service was
    inner-network only. Public exposure turns every unauthorized probe into a
    row, so retention is part of the design now — 30 days by default
    (PEARSS_AUDIT_RETENTION_DAYS). The table deliberately has no occurred_at
    index: the retention cap keeps it small enough that the delete's scan is
    cheap, and an index would exist only to serve this delete.
    """
    result = cast(
        "CursorResult[Any]",
        session.execute(
            delete(AuditLog).where(AuditLog.occurred_at < timestamp(older_than))
        ),
    )
    return int(result.rowcount or 0)


def reap_orphaned_runs(session: Session, *, older_than: datetime) -> int:
    """Time out runs left `running` by a previous process.

    A run is claimed, then collection happens, then it is closed. If the
    process dies in the middle the row stays `running` forever: nothing will
    ever close it, and the run history silently accumulates zombies that look
    like work still in progress.

    Timed out rather than deleted, because the state machine treats a timeout
    as a terminal outcome and the row is real evidence that an attempt started
    and never came back.
    """
    cutoff = timestamp(older_than)
    result = cast(
        "CursorResult[Any]",
        session.execute(
            update(JobRunRow)
            .where(
                JobRunRow.status == JobRunStatus.RUNNING.value,
                or_(
                    JobRunRow.started_at.is_(None),
                    JobRunRow.started_at < cutoff,
                ),
            )
            .values(
                status=JobRunStatus.TIMED_OUT.value,
                finished_at=timestamp(datetime.now(UTC)),
                error_class=ErrorClass.NETWORK.value,
            )
        ),
    )
    return int(result.rowcount or 0)


def last_collect_at(session: Session, *, job_id: str) -> datetime | None:
    """When this source's collection job last finished, any outcome.

    Failures count: backing off requires knowing the last attempt happened,
    and ignoring failed runs would retry a blocked source on its normal
    cadence.
    """
    finished = (
        session.execute(
            select(JobRunRow.finished_at)
            .where(
                JobRunRow.job_id == job_id,
                JobRunRow.finished_at.is_not(None),
            )
            .order_by(JobRunRow.finished_at.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )
    return _parse(finished) if finished else None


def consecutive_failures(session: Session, *, job_id: str, limit: int = 20) -> int:
    """How many most recent runs failed, stopping at the first success.

    Walks back from the newest run and stops early: a source that recovered
    after three failures should not keep its backoff, so counting all failures
    ever would be wrong.
    """
    statuses = session.execute(
        select(JobRunRow.status)
        .where(JobRunRow.job_id == job_id)
        .order_by(JobRunRow.started_at.desc().nullslast())
        .limit(limit)
    ).scalars()
    count = 0
    for status in statuses:
        if status == JobRunStatus.SUCCEEDED.value:
            break
        if status in (
            JobRunStatus.FAILED.value,
            JobRunStatus.TIMED_OUT.value,
        ):
            count += 1
        # A queued or running run is neither a failure nor a recovery; skip it
        # rather than stopping, since it has not resolved.
    return count


def collectable_sources(session: Session, *, tenant_id: str) -> list[tuple[str, str]]:
    """(source_id, schedule_policy_json) for sources we should consider.

    Only `active`: a paused or disabled source is not collected, and that is
    the operator's explicit decision rather than something the scheduler
    should second-guess.
    """
    rows = session.execute(
        select(SourceRow.id, SourceRow.schedule_policy).where(
            SourceRow.tenant_id == tenant_id,
            SourceRow.status == SourceStatus.ACTIVE.value,
        )
    ).all()
    return [(str(row[0]), str(row[1])) for row in rows]
