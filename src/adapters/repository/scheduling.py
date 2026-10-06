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
from adapters.db_models import Origin as OriginRow
from adapters.db_models import Subscription as SubscriptionRow
from adapters.repository._shared import _parse
from adapters.timestamps import fixed_width_utc as timestamp
from core.enums import (
    ErrorClass,
    JobRunStatus,
    SourceStatus,
    SourceType,
)
from core.normalize import origin_display_label
from core.paging import WalkState
from core.scheduling import observed_gaps


@dataclass(frozen=True, slots=True, kw_only=True)
class PagingState(WalkState):
    """Where the last walk of this source's origin left off.

    The stored view of core.paging.WalkState — the same fields, so a
    persisted state passes anywhere a walk state is expected.
    """


def paging_state(session: Session, *, source_id: str) -> PagingState:
    row = session.get(OriginRow, source_id)
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

    row = session.get(OriginRow, source_id)
    if row is None:
        return
    if newest_id is not None and (
        row.seen_frontier is None or newest_id > row.seen_frontier
    ):
        row.seen_frontier = newest_id

    if floor_candidate:
        row.floor_cursor = floor_candidate
    row.updated_at = timestamp(datetime.now(UTC))


def origin_display_labels(session: Session, source_ids: list[str]) -> dict[str, str]:
    """Display labels for the given sources, keyed by source id.

    Batched because a contents request can name a hundred items across several
    sources, and resolving a name per item would turn one page into one hundred
    queries. Falls back through core.origin_display_label so a source never
    collected still gets something better than its hex id.
    """
    wanted = [source_id for source_id in set(source_ids) if source_id]
    if not wanted:
        return {}
    rows = session.execute(
        select(
            OriginRow.id, OriginRow.title, OriginRow.url, OriginRow.canonical_url
        ).where(OriginRow.id.in_(wanted))
    ).all()
    return {
        row.id: origin_display_label(
            title=row.title,
            url=row.url,
            canonical_url=row.canonical_url,
            origin_id=row.id,
        )
        for row in rows
    }


def set_origin_title_if_absent(
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
            update(OriginRow)
            .where(
                OriginRow.id == source_id,
                or_(OriginRow.title.is_(None), OriginRow.title == ""),
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
                ItemRow.origin_id == source_id,
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
    """When this source's collection was last attempted, any outcome.

    The start of the most recent *resolved* run: the interval counts from
    when the last attempt began, stamped with the caller's decision clock
    so it is always comparable to the next tick's clock. Reading the
    finish stamp instead made the anchor the wall-clock completion moment
    (issue #17), which sits outside an injected decision timeline and
    read as clock skew -- due immediately, defeating backoff.

    Unfinished runs are not evidence yet: a claimed-but-open run may still
    fail, and the reaper has not ruled on it.

    Failures count: backing off requires knowing the last attempt happened,
    and ignoring failed runs would retry a blocked source on its normal
    cadence.
    """
    started = (
        session.execute(
            select(JobRunRow.started_at)
            .where(
                JobRunRow.job_id == job_id,
                JobRunRow.started_at.is_not(None),
                JobRunRow.finished_at.is_not(None),
            )
            .order_by(JobRunRow.started_at.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )
    return _parse(started) if started else None


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


def last_failure_error_class(
    session: Session, *, job_id: str, limit: int = 20
) -> ErrorClass | None:
    """The classification of the most recent failure in the current streak.

    Walks the same window `consecutive_failures` walks and stops at the same
    boundaries, so the class and the count always describe the same streak
    (issue #5): the retry table keys on the class, and a stale class from a
    recovered streak must not suppress a current one's backoff. None when the
    streak has no classification -- legacy rows or a parse that never
    classified -- which the scheduler treats conservatively (backoff stays).
    """
    rows = session.execute(
        select(JobRunRow.status, JobRunRow.error_class)
        .where(JobRunRow.job_id == job_id)
        .order_by(JobRunRow.started_at.desc().nullslast())
        .limit(limit)
    ).all()
    for status, error_class in rows:
        if status == JobRunStatus.SUCCEEDED.value:
            break
        if status in (
            JobRunStatus.FAILED.value,
            JobRunStatus.TIMED_OUT.value,
        ):
            try:
                return ErrorClass(error_class)
            except ValueError:  # unreachable: the schema CHECK-constrains
                # error_class to the five enum values; a corrupt DB row must
                # not crash the tick, hence the guard.
                return None
        # Queued/running: skip, same convention as consecutive_failures.
    return None


def collectable_sources(session: Session, *, tenant_id: str) -> list[tuple[str, str]]:
    """(origin_id, schedule_policy_json) for origins we should consider.

    Policy comes from the subscription and identity from the origin: the two
    halves of ADR 0046's split, read through a join so a paused subscription is
    never collected however its origin is configured.

    Only `active`: a paused or disabled subscription is not collected, and that
    is the operator's explicit decision rather than something the scheduler
    should second-guess.

    `agent` sources are excluded by type: they are pushed to, not polled
    (ADR 0022). Nothing would collect them -- `select_for("agent", ...)` falls
    through to `select(url)` and matches no collector -- so scheduling one
    produces a failed run every tick. Unlike a genuinely broken source, that
    failure never clears, and it would show up as a permanent dip in the
    success rate rather than as an incident to fix.
    """
    rows = session.execute(
        select(OriginRow.id, SubscriptionRow.schedule_policy)
        .join(SubscriptionRow, SubscriptionRow.origin_id == OriginRow.id)
        .where(
            OriginRow.tenant_id == tenant_id,
            SubscriptionRow.status == SourceStatus.ACTIVE.value,
            OriginRow.type != SourceType.AGENT.value,
        )
    ).all()
    return [(str(row[0]), str(row[1])) for row in rows]
