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


def create_work(
    session: Session,
    *,
    kind: str,
    params: dict[str, Any],
    tenant_id: str,
    correlation_id: str | None = None,
    retry_of: str | None = None,
) -> WorkRecord:
    """Record a requested batch as `queued`. Executing it is not this function."""
    row = WorkRow(
        id=f"work-{secrets.token_hex(16)}",
        kind=kind,
        params_json=dumps(params),
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
    session: Session, work_id: str, *, limit: int | None = None
) -> list[UnitRecord]:
    """The work's ledger, ordered by target.

    Ordered so paging is stable and a caller diffing two reads sees a
    deterministic sequence; the key is (work_id, target), so this is the
    table's own order and costs nothing.
    """
    query = (
        sa.select(WorkUnitRow)
        .where(WorkUnitRow.work_id == work_id)
        .order_by(WorkUnitRow.target)
    )
    if limit is not None:
        query = query.limit(limit)
    return [_unit_record(row) for row in session.scalars(query)]


def counts_for_work(
    session: Session, work_id: str, *, total_targets: int
) -> dict[str, int]:
    """The work's statistics, folded from its rows.

    `total_targets` comes from the command (it is what the caller asked for),
    so `queued` can be derived as "targets the ledger has not reached yet"
    rather than tracked.
    """
    outcomes = [
        UnitOutcome(row.outcome)
        for row in session.scalars(
            sa.select(WorkUnitRow).where(WorkUnitRow.work_id == work_id)
        )
    ]
    return work_counts(total_targets=total_targets, outcomes=outcomes)


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


def _work_record(row: WorkRow) -> WorkRecord:
    return WorkRecord(
        id=row.id,
        kind=row.kind,
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
