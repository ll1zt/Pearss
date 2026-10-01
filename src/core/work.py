"""The work ledger's domain shape: statuses, records, and the counts fold.

A **work** is one requested batch -- "do this to these targets" -- and its
**units** are the per-target outcomes. Nothing here touches a database: the
ledger's statistics are a *pure fold* over the unit outcomes (ADR 0046), so the
same numbers are reachable without reading a stored counter. That is the point
of the fold: a counter beside the rows is a second source of truth for the same
fact, and it drifts the first time one writer forgets the other.

`rejected` vs `failed` carries the retry decision: a rejected target was
refused by this deployment before anything ran (same-host policy, missing
capability, over the request ceiling), so retrying it changes nothing; a failed
target was attempted and the world said no, so it is the one worth re-sending.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from core.enums import ErrorClass, UnitOutcome, WorkStatus

# A work in one of these will not change again. Derived from the status
# vocabulary rather than listed twice, so a new terminal state is one edit.
TERMINAL_STATUSES: frozenset[WorkStatus] = frozenset(
    {
        WorkStatus.SUCCEEDED,
        WorkStatus.PARTIAL,
        WorkStatus.FAILED,
        WorkStatus.CANCELLED,
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkRecord:
    """One work as the domain sees it.

    `targets` is the command's input and is a field of its own rather than a
    key inside `params`: it is *what* the work is about, while `params` holds
    the kind's own knobs (which source, which chain). Keeping them apart is
    also what lets `counts` derive `total` from the row instead of from a
    caller-supplied number.
    """

    id: str
    kind: str
    targets: list[str]
    params: dict[str, Any]
    status: WorkStatus
    retry_of: str | None
    created_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True, slots=True, kw_only=True)
class UnitRecord:
    """One target's outcome inside a work (a ledger row)."""

    target: str
    outcome: UnitOutcome
    error_class: ErrorClass | None
    detail: str
    ref: str | None
    at: datetime


def command_fingerprint(
    *, kind: str, targets: Sequence[str], params: Mapping[str, Any]
) -> str:
    """The digest that makes "the same command" recognisable (ADR 0046 law 7).

    Derived from the command's *value* and nothing else -- never from time or a
    random source -- because its job is to answer "is this the same request
    again?" for a caller that re-issues it after a timeout. Target order is
    part of the value: the ledger and the response both report that order, so a
    permutation is a different command.

    It is not a security boundary: it decides whether a live work is reused,
    and a caller who can submit a command can already compute its digest.
    """
    canonical = json.dumps(
        {"kind": kind, "targets": list(targets), "params": dict(params)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def counts_from(outcomes: Iterable[UnitOutcome]) -> dict[str, int]:
    """Count each outcome, zero-filled across the whole vocabulary.

    Every key is always present so a caller branching on `counts["rejected"]`
    never has to handle absence: "none were rejected" and "the key is missing"
    are the same fact and should read the same.
    """
    counts = {outcome.value: 0 for outcome in UnitOutcome}
    for outcome in outcomes:
        counts[outcome.value] += 1
    return counts


def work_counts(
    *, total_targets: int, outcomes: Iterable[UnitOutcome]
) -> dict[str, int]:
    """The work's counts: the fold, plus what the ledger has not reached yet.

    `queued` is derived (targets minus rows) rather than tracked: a target with
    no row has no outcome yet, which is exactly what "queued" means. A work
    whose ledger covers every target therefore reports `queued == 0`.
    """
    outcomes = list(outcomes)
    counts = counts_from(outcomes)
    counts["queued"] = max(0, total_targets - len(outcomes))
    counts["total"] = total_targets
    return counts


def terminal_status(counts: Mapping[str, int]) -> WorkStatus:
    """How a finished work should be labelled, from its counts alone.

    Derived rather than tracked: "all of it worked" / "some of it worked" /
    "none of it worked" is a fact about the ledger, and a status an executor
    writes could disagree with the rows it just appended.

    `skipped` counts as success -- there was nothing to do -- while `rejected`
    does not: the work did not do what it was asked, which is what a caller
    needs to see.
    """
    done = counts["ok"] + counts["skipped"]
    if counts["total"] > 0 and done == counts["total"]:
        return WorkStatus.SUCCEEDED
    if done > 0:
        return WorkStatus.PARTIAL
    return WorkStatus.FAILED
