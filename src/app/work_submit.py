"""Turning a command into a work: the one place that happens (issue #199).

`POST /v1/works` takes a command from the caller. The selection-shaped endpoints
(`:retry-enrichments`, `items:delete`, `:backfill-chain`) take a *selection* and
produce the targets themselves, because the caller cannot: naming the rows would
mean paging a chain and holding the list, which is the caller-side loop ADR 0046
rejects.

Both end in the same place -- a command value hashed into a fingerprint, a check
for a live work with that fingerprint, and an insert that can lose a race -- and
that place is here, once, so the de-duplication rule cannot be right in one entry
point and wrong in the other.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from adapters.correlation import current as current_correlation_id
from adapters.repository import counts_for_work, create_work, live_work_by_fingerprint
from core.tenancy import SINGLE_TENANT
from core.work import WorkRecord, command_fingerprint


def submit_command(
    session: Session, *, kind: str, targets: list[str], params: dict[str, Any]
) -> WorkRecord:
    """The work for this command value: create it, or return the live one.

    "Live" is the whole point of the fingerprint. Two submissions of the same
    command in flight are one batch, and the second caller gets the handle the
    first one got. A work that already finished does not count: asking for the
    same command again after it ran is a new run, which is what the partial
    unique index (queued/running only) encodes.
    """
    fingerprint = command_fingerprint(kind=kind, targets=targets, params=params)
    work = live_work_by_fingerprint(session, fingerprint)
    if work is not None:
        return work
    try:
        work = create_work(
            session,
            kind=kind,
            targets=targets,
            params=params,
            fingerprint=fingerprint,
            tenant_id=SINGLE_TENANT,
            correlation_id=current_correlation_id(),
        )
        session.commit()
    except IntegrityError:
        # Two submissions of the same command raced and the index picked one.
        # Answering with the winner is the point of hashing the command value:
        # the caller asked for one batch.
        session.rollback()
        work = live_work_by_fingerprint(session, fingerprint)
        if work is None:
            # The winning work finished between the collision and this read, so
            # the command is new again. Submitting it once more is correct; a
            # second collision here would be a third submission in the same
            # instant, which is a genuine error.
            work = create_work(
                session,
                kind=kind,
                targets=targets,
                params=params,
                fingerprint=fingerprint,
                tenant_id=SINGLE_TENANT,
                correlation_id=current_correlation_id(),
            )
            session.commit()
    return work


def handle_payload(session: Session, work: WorkRecord) -> dict[str, Any]:
    """The O(1) answer for a submitted command: the handle and the folded counts.

    The counts ride along because they are a fold over the ledger (ADR 0046),
    not a second source of truth: nothing here is a number that a row could
    disagree with.
    """
    return {
        "work_id": work.id,
        "kind": work.kind,
        "status": work.status.value,
        "counts": counts_for_work(session, work.id),
        "correlation_id": current_correlation_id(),
    }
