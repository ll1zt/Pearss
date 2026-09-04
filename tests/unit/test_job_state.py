"""Tests for the JobRun state machine.

docs/domain/state-machines.md requires a test for every legal and illegal
transition, and the JobRun rules are what make run history trustworthy
evidence: the adaptive backoff and the error taxonomy both read these rows.
"""

from __future__ import annotations

import pytest

from core.enums import ErrorClass, JobRunStatus
from core.job_state import (
    TERMINAL_STATUSES,
    InvalidJobRunTransitionError,
    JobRunContext,
    JobRunEvent,
    JobRunView,
    is_terminal,
    next_run_status,
    transition,
    with_status,
)


def view(
    status: JobRunStatus = JobRunStatus.QUEUED,
    **overrides: object,
) -> JobRunView:
    defaults: dict[str, object] = {
        "id": "run-1",
        "job_id": "job-1",
        "status": status,
        "correlation_id": "corr-1",
    }
    defaults.update(overrides)
    return JobRunView(**defaults)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Legal transitions
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_queued_to_running_on_claim() -> None:
    result = transition(JobRunStatus.QUEUED, JobRunEvent.CLAIMED)
    assert result.status is JobRunStatus.RUNNING
    assert not result.terminal


@pytest.mark.unit
@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (JobRunEvent.SUCCEED, JobRunStatus.SUCCEEDED),
        (JobRunEvent.TIMEOUT, JobRunStatus.TIMED_OUT),
    ],
)
def test_running_to_its_terminal_states(
    event: JobRunEvent, expected: JobRunStatus
) -> None:
    result = transition(JobRunStatus.RUNNING, event)
    assert result.status is expected
    assert result.terminal


@pytest.mark.unit
def test_failure_records_the_error_class() -> None:
    result = transition(
        JobRunStatus.RUNNING,
        JobRunEvent.FAIL,
        JobRunContext(error_class=ErrorClass.NETWORK),
    )
    assert result.status is JobRunStatus.FAILED
    assert result.error_class is ErrorClass.NETWORK


@pytest.mark.unit
def test_queued_run_can_be_cancelled() -> None:
    result = transition(JobRunStatus.QUEUED, JobRunEvent.CANCEL)
    assert result.status is JobRunStatus.CANCELLED
    assert result.terminal


@pytest.mark.unit
def test_timeout_may_carry_an_error_class_but_need_not() -> None:
    bare = transition(JobRunStatus.RUNNING, JobRunEvent.TIMEOUT)
    with_class = transition(
        JobRunStatus.RUNNING,
        JobRunEvent.TIMEOUT,
        JobRunContext(error_class=ErrorClass.NETWORK),
    )
    assert bare.error_class is None
    assert with_class.error_class is ErrorClass.NETWORK


# --------------------------------------------------------------------------
# Illegal transitions
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("status", sorted(TERMINAL_STATUSES, key=str))
@pytest.mark.parametrize("event", list(JobRunEvent))
def test_terminal_runs_never_move(status: JobRunStatus, event: JobRunEvent) -> None:
    # The documented rule that a retry is a new run linked by retry_of, not a
    # reopening. Without it, a failure record disappears the moment someone
    # retries.
    with pytest.raises(InvalidJobRunTransitionError, match="terminal"):
        transition(status, event)


@pytest.mark.unit
def test_failure_without_an_error_class_is_refused() -> None:
    # A failed run with no class makes the taxonomy unqueryable, and the
    # backoff computation reads exactly that.
    with pytest.raises(InvalidJobRunTransitionError, match="must record"):
        transition(JobRunStatus.RUNNING, JobRunEvent.FAIL)


@pytest.mark.unit
def test_success_with_an_error_class_is_refused() -> None:
    with pytest.raises(InvalidJobRunTransitionError, match="cannot carry"):
        transition(
            JobRunStatus.RUNNING,
            JobRunEvent.SUCCEED,
            JobRunContext(error_class=ErrorClass.PARSE),
        )


@pytest.mark.unit
def test_queued_run_cannot_succeed_or_fail_directly() -> None:
    for event in (JobRunEvent.SUCCEED, JobRunEvent.FAIL, JobRunEvent.TIMEOUT):
        with pytest.raises(InvalidJobRunTransitionError):
            transition(JobRunStatus.QUEUED, event)


@pytest.mark.unit
def test_running_run_cannot_be_claimed_again() -> None:
    with pytest.raises(InvalidJobRunTransitionError):
        transition(JobRunStatus.RUNNING, JobRunEvent.CLAIMED)


@pytest.mark.unit
def test_cancelling_a_running_run_is_refused_for_now() -> None:
    # The table allows it only where the task supports safe interruption. HTTP
    # collection does not yet, so refusing is the honest answer rather than
    # pretending the work can be stopped mid-fetch.
    with pytest.raises(InvalidJobRunTransitionError, match="interruption"):
        transition(JobRunStatus.RUNNING, JobRunEvent.CANCEL)


# --------------------------------------------------------------------------
# View invariants
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_correlation_id_is_mandatory() -> None:
    with pytest.raises(ValueError, match="correlation_id"):
        view(correlation_id="")


@pytest.mark.unit
def test_view_rejects_failed_without_error_class() -> None:
    with pytest.raises(ValueError, match="must record"):
        view(JobRunStatus.FAILED)


@pytest.mark.unit
def test_view_rejects_succeeded_with_error_class() -> None:
    with pytest.raises(ValueError, match="cannot carry"):
        view(JobRunStatus.SUCCEEDED, error_class=ErrorClass.NETWORK)


@pytest.mark.unit
def test_with_status_returns_an_updated_copy() -> None:
    run = view()
    moved = with_status(run, JobRunEvent.CLAIMED)
    assert moved.status is JobRunStatus.RUNNING
    # Frozen: the original is untouched, so a caller cannot accidentally
    # mutate the record it already persisted.
    assert run.status is JobRunStatus.QUEUED
    assert moved.id == run.id and moved.job_id == run.job_id


@pytest.mark.unit
def test_with_status_carries_the_error_class_onto_the_view() -> None:
    running = with_status(view(), JobRunEvent.CLAIMED)
    failed = with_status(
        running, JobRunEvent.FAIL, JobRunContext(error_class=ErrorClass.BLOCKED)
    )
    assert failed.status is JobRunStatus.FAILED
    assert failed.error_class is ErrorClass.BLOCKED


@pytest.mark.unit
def test_next_run_status_is_the_short_form() -> None:
    assert (
        next_run_status(JobRunStatus.QUEUED, JobRunEvent.CLAIMED)
        is JobRunStatus.RUNNING
    )


@pytest.mark.unit
def test_terminal_predicate_matches_the_table() -> None:
    assert is_terminal(JobRunStatus.SUCCEEDED)
    assert is_terminal(JobRunStatus.CANCELLED)
    assert not is_terminal(JobRunStatus.RUNNING)
    assert not is_terminal(JobRunStatus.QUEUED)
