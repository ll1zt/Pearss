"""The work ledger's statistics are a fold, not a counter.

Scenario register (normative source: SCENARIOS.md, AC-180..182; issue #187).

`counts` is derived from the ledger rows on every read. Storing a counter
beside them would be a second source of truth for the same fact -- the shape
that drifts the first time one writer forgets the other -- so the fold is the
definition, and these tests pin it: a ledger of N rows must always produce the
statistic of those N rows, whatever they are.
"""

from __future__ import annotations

import pytest

from core.enums import UnitOutcome, WorkStatus
from core.work import counts_from, terminal_status, work_counts

pytestmark = pytest.mark.unit


def test_work_counts_fold_from_the_ledger() -> None:
    """AC-182: counts are exactly what the ledger rows say."""
    outcomes = [
        UnitOutcome.OK,
        UnitOutcome.OK,
        UnitOutcome.REJECTED,
        UnitOutcome.FAILED,
    ]

    counts = counts_from(outcomes)

    assert counts == {"ok": 2, "skipped": 0, "rejected": 1, "failed": 1}
    assert sum(counts.values()) == len(outcomes)


def test_an_empty_ledger_folds_to_zeros() -> None:
    """A work that has not been touched yet reports zeros, not missing keys.

    The keys are the outcome vocabulary, not "whatever was seen": a caller
    branching on `counts["rejected"]` must not have to handle absence.
    """
    assert counts_from([]) == {"ok": 0, "skipped": 0, "rejected": 0, "failed": 0}


def test_queued_targets_are_the_ones_the_ledger_has_not_reached() -> None:
    """`queued` is targets minus rows: a target with no row has no outcome yet."""
    counts = work_counts(total_targets=5, outcomes=[UnitOutcome.OK, UnitOutcome.FAILED])

    assert counts["queued"] == 3
    assert counts["ok"] == 1
    assert counts["failed"] == 1
    assert counts["total"] == 5


def test_a_fully_terminated_work_has_nothing_queued() -> None:
    """The boundary the API reports as "terminal": every target has a row."""
    counts = work_counts(
        total_targets=2, outcomes=[UnitOutcome.SKIPPED, UnitOutcome.REJECTED]
    )

    assert counts["queued"] == 0
    assert counts["skipped"] == 1 and counts["rejected"] == 1


def test_terminal_status_is_the_fold_of_what_happened() -> None:
    """A finished work's label is derived, never tracked.

    "All of it worked / some of it worked / none of it worked" is a fact about
    the rows the executor just appended. A status the executor wrote alongside
    them would be a second source of truth, and the two could disagree.
    """
    everything = {"total": 3, "ok": 3, "skipped": 0, "rejected": 0, "failed": 0}
    some = {"total": 3, "ok": 1, "skipped": 0, "rejected": 1, "failed": 1}
    none = {"total": 2, "ok": 0, "skipped": 0, "rejected": 1, "failed": 1}

    assert terminal_status(everything) is WorkStatus.SUCCEEDED
    assert terminal_status(some) is WorkStatus.PARTIAL
    assert terminal_status(none) is WorkStatus.FAILED


def test_nothing_to_do_still_counts_as_success() -> None:
    """`skipped` succeeded at the only thing it had to do.

    A subscription work whose origin was already subscribed did what it was
    asked; reporting it as partial would make "worked" unreadable to a caller
    asking exactly that.
    """
    counts = {"total": 2, "ok": 0, "skipped": 2, "rejected": 0, "failed": 0}

    assert terminal_status(counts) is WorkStatus.SUCCEEDED
