"""Work and work-unit persistence: the ledger behind ADR 0046.

The rules live in `core.work` (pure); this module applies them to rows. Two
shape decisions it inherits:

- `counts_for_work` **folds the ledger** through `core.work.work_counts` rather
  than reading a counter column. The rows are the fact; a counter beside them
  would be a second answer to one question.
- `append_unit` writes one row per target and lets the composite key refuse a
  repeat. It does not check first: a `SELECT`-then-`INSERT` would lose exactly
  the race the key exists to prevent, and would turn a database guarantee into
  an application convention.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import UTC, datetime
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from adapters.db_models import Work as WorkRow
from adapters.db_models import WorkUnit as WorkUnitRow
from adapters.repository._shared import dumps, timestamp
from core.enums import ErrorClass, UnitOutcome, WorkStatus
from core.work import UnitRecord, WorkRecord, work_counts

LOGGER = logging.getLogger(__name__)


def create_work(
    session: Session,
    *,
    kind: str,
    targets: list[str],
    params: dict[str, Any],
    fingerprint: str,
    tenant_id: str,
    correlation_id: str | None = None,
    retry_of: str | None = None,
) -> WorkRecord:
    """Record a requested batch as `queued`. Executing it is not this function.

    `fingerprint` is the command value's digest and is required: a work without
    one could not be recognised as a repeat, which is the property the partial
    unique index enforces.
    """
    row = WorkRow(
        id=f"work-{secrets.token_hex(16)}",
        kind=kind,
        targets_json=dumps(targets),
        params_json=dumps(params),
        fingerprint=fingerprint,
        status=WorkStatus.QUEUED.value,
        correlation_id=correlation_id,
        retry_of=retry_of,
        tenant_id=tenant_id,
        created_at=timestamp(datetime.now(UTC)),
    )
    session.add(row)
    session.flush()
    return _work_record(row)


def work_by_id(session: Session, work_id: str) -> WorkRecord | None:
    row = session.get(WorkRow, work_id)
    return None if row is None else _work_record(row)


def live_work_by_fingerprint(session: Session, fingerprint: str) -> WorkRecord | None:
    """The queued/running work for this command value, if one is in flight.

    "In flight" is the whole condition: a finished work does not block the same
    command from being submitted again, which is what a caller wanting a fresh
    run of the same batch needs (ADR 0046; a plain unique constraint would have
    forbidden that forever).
    """
    row = session.scalars(
        sa.select(WorkRow)
        .where(
            WorkRow.fingerprint == fingerprint,
            WorkRow.status.in_((WorkStatus.QUEUED.value, WorkStatus.RUNNING.value)),
        )
        .order_by(WorkRow.created_at.desc())
        .limit(1)
    ).first()
    return None if row is None else _work_record(row)


def _claimable(*, stale_before: datetime) -> Any:
    """Which works this host may take, as one expression used twice.

    `queued` is always claimable. `running` is claimable only while it looks
    abandoned -- see `claim_next_work` for why that is a lease and not a status.
    """
    last_activity = (
        sa.select(sa.func.max(WorkUnitRow.at))
        .where(WorkUnitRow.work_id == WorkRow.id)
        .scalar_subquery()
    )
    return sa.or_(
        WorkRow.status == WorkStatus.QUEUED.value,
        sa.and_(
            WorkRow.status == WorkStatus.RUNNING.value,
            # Fixed-width stamps compare lexicographically, which is what makes
            # `<` on a Text column mean "earlier" at all.
            sa.func.coalesce(last_activity, WorkRow.created_at)
            < timestamp(stale_before),
        ),
    )


def claim_next_work(
    session: Session, *, kinds: frozenset[str], stale_before: datetime
) -> WorkRecord | None:
    """Claim the oldest work this host can execute, or None.

    `kinds` is the set of executors that exist. A kind with no executor stays
    queued rather than being claimed and failed, so submitting a `delete` work
    before its executor ships is harmless.

    `running` works are claimable too, and that is the resume path: a work whose
    executor died mid-way keeps the ledger rows it wrote, so re-entering it
    continues from where it stopped instead of starting over. But `running` also
    covers a work that is simply *slow*, and re-entering one of those re-runs
    targets the first executor is fetching right now -- a second process, or a
    worker split out of the app, would double every request and only find out at
    the ledger's primary key. So the claim is a lease: a `running` work is taken
    only when its last ledger row (or `created_at`, for a pass that died before
    writing one) is older than `stale_before`.

    Every unit write is a heartbeat, which is what makes that a statement about
    liveness rather than a guess: a pass that is fetching is writing rows, and a
    pass that stopped writing for the whole window is presumed gone. The caller
    owns the window (`app.work_worker.work_lease_seconds`), so the policy stays
    out of the repository.

    The claim itself is a conditional UPDATE carrying the same predicate, so two
    workers cannot both take the same row.
    """
    row = session.scalars(
        sa.select(WorkRow)
        .where(
            WorkRow.kind.in_(tuple(sorted(kinds))),
            _claimable(stale_before=stale_before),
        )
        .order_by(WorkRow.created_at, WorkRow.id)
        .limit(1)
    ).first()
    if row is None:
        return None
    claimed = cast(
        "CursorResult[Any]",
        session.execute(
            sa.update(WorkRow)
            .where(WorkRow.id == row.id, _claimable(stale_before=stale_before))
            .values(status=WorkStatus.RUNNING.value)
        ),
    )
    if (claimed.rowcount or 0) == 0:
        return None
    session.flush()
    # One line per claim, event-driven: an idle worker writes nothing, so
    # without this "is the queue moving?" is answered only by the finish line
    # (issue #296).
    LOGGER.info(
        "claimed work %s (kind=%s, %d target(s))",
        row.id,
        row.kind,
        len(json.loads(row.targets_json)),
    )
    return work_by_id(session, row.id)


def append_unit(
    session: Session,
    *,
    work_id: str,
    target: str,
    outcome: UnitOutcome,
    error_class: ErrorClass | None = None,
    detail: str = "",
    ref: str | None = None,
) -> UnitRecord:
    """Write one target's outcome.

    Raises `IntegrityError` when the target already has a row for this work:
    that is the ledger's whole guarantee, and translating it here would hide the
    fact that the caller decided to act twice on one target.
    """
    row = WorkUnitRow(
        work_id=work_id,
        target=target,
        outcome=outcome.value,
        error_class=error_class.value if error_class is not None else None,
        detail=detail,
        ref=ref,
        at=timestamp(datetime.now(UTC)),
    )
    session.add(row)
    session.flush()
    return _unit_record(row)


def units_for_work(
    session: Session,
    work_id: str,
    *,
    outcome: UnitOutcome | None = None,
    after_target: str | None = None,
    limit: int | None = None,
) -> list[UnitRecord]:
    """The work's ledger, ordered by target, optionally filtered and paged.

    Ordered by `target` -- the second half of the key -- so paging is keyset and
    stable while the executor appends rows: an offset would re-serve or skip
    rows depending on where a newly appended one landed. `after_target` is the
    decoded cursor.
    """
    query = sa.select(WorkUnitRow).where(WorkUnitRow.work_id == work_id)
    if outcome is not None:
        query = query.where(WorkUnitRow.outcome == outcome.value)
    if after_target is not None:
        query = query.where(WorkUnitRow.target > after_target)
    query = query.order_by(WorkUnitRow.target)
    if limit is not None:
        query = query.limit(limit)
    return [_unit_record(row) for row in session.scalars(query)]


def counts_for_work(session: Session, work_id: str) -> dict[str, int]:
    """The work's statistics, folded from its rows.

    `total` comes from the work row itself (the command's targets), not from a
    caller-supplied number, so the statistics cannot disagree with the work
    they describe.
    """
    work = work_by_id(session, work_id)
    if work is None:
        raise LookupError(f"no work {work_id!r}")
    outcomes = [
        UnitOutcome(row.outcome)
        for row in session.scalars(
            sa.select(WorkUnitRow).where(WorkUnitRow.work_id == work_id)
        )
    ]
    return work_counts(total_targets=len(work.targets), outcomes=outcomes)


def claim_work(session: Session, work_id: str) -> WorkRecord | None:
    """Move a queued work to `running`, or return None if it is already started.

    Conditional on the current status (`WHERE status = 'queued'`), so two
    workers racing for the same work cannot both win.
    """
    # session.execute is typed as returning Result[Any], but a DML statement
    # yields a CursorResult at runtime, which is where rowcount lives. The cast
    # is narrowed to this one statement (feed.py's idiom) rather than disabling
    # the check for the module.
    result = cast(
        "CursorResult[Any]",
        session.execute(
            sa.update(WorkRow)
            .where(WorkRow.id == work_id, WorkRow.status == WorkStatus.QUEUED.value)
            .values(status=WorkStatus.RUNNING.value)
        ),
    )
    if (result.rowcount or 0) == 0:
        return None
    return work_by_id(session, work_id)


def finish_work(
    session: Session, work_id: str, *, status: WorkStatus, now: datetime | None = None
) -> WorkRecord | None:
    """Close a work with a terminal status, stamping `finished_at`."""
    row = session.get(WorkRow, work_id)
    if row is None:
        return None
    row.status = status.value
    row.finished_at = timestamp(now or datetime.now(UTC))
    session.flush()
    return _work_record(row)


def prune_work_ledger(session: Session, *, older_than: datetime) -> int:
    """Delete finished work recorded before `older_than`, its units with it.

    The ledger is an operational record rather than a fact source (ADR 0046
    D-F), so it has a retention window -- 30 days by default
    (`PEARSS_WORK_RETENTION_DAYS`) -- because a table that only grows is a
    second database.

    `finished_at is not null` is the load-bearing half of the predicate. A work
    with no end is *live*: its row is what a running executor writes its units
    against, and deleting it would make a job still in progress look like one
    that vanished. Age alone therefore cannot delete it -- a queued work is
    never stale, however old, and a row that somehow reached a terminal status
    without a finish stamp leaks rather than risks being deleted mid-run.

    The comparison is lexicographic on the fixed-width stamp
    (`adapters.timestamps.fixed_width_utc`), which is what makes `<` on a Text
    column mean "earlier" at all.

    The units are deliberately not deleted here: `work_unit.work_id` is
    `ON DELETE CASCADE`, so one statement says what belongs to a work instead of
    two that can drift apart. Returns the number of works deleted.
    """
    result = cast(
        "CursorResult[Any]",
        session.execute(
            sa.delete(WorkRow).where(
                WorkRow.finished_at.is_not(None),
                WorkRow.finished_at < timestamp(older_than),
            )
        ),
    )
    return int(result.rowcount or 0)


def _work_record(row: WorkRow) -> WorkRecord:
    return WorkRecord(
        id=row.id,
        kind=row.kind,
        targets=_string_list(row.targets_json),
        params=_loads(row.params_json),
        status=WorkStatus(row.status),
        retry_of=row.retry_of,
        created_at=datetime.fromisoformat(row.created_at.replace("Z", "+00:00")),
        finished_at=(
            None
            if not row.finished_at
            else datetime.fromisoformat(row.finished_at.replace("Z", "+00:00"))
        ),
    )


def _unit_record(row: WorkUnitRow) -> UnitRecord:
    return UnitRecord(
        target=row.target,
        outcome=UnitOutcome(row.outcome),
        error_class=None if not row.error_class else ErrorClass(row.error_class),
        detail=row.detail,
        ref=row.ref,
        at=datetime.fromisoformat(row.at.replace("Z", "+00:00")),
    )


def _loads(value: str) -> dict[str, Any]:
    loaded = json.loads(value)
    return loaded if isinstance(loaded, dict) else {}


def _string_list(value: str) -> list[str]:
    loaded = json.loads(value)
    if not isinstance(loaded, list):
        return []
    return [str(item) for item in loaded]
