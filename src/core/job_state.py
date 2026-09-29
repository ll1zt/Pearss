"""JobRun state machine.

The executable form of the JobRun table in docs/domain/state-machines.md.
Transitions live here rather than at the call sites because the rules are what
make a run history trustworthy: a run that could be reopened, or that could
carry an error_class while succeeding, stops being evidence about what
happened.

Two rules drive most of the design:

- terminal states are immutable, and a retry is a NEW run linked by `retry_of`
  rather than a reset. Overwriting a terminal run would destroy the record of
  the failure that motivated the retry.
- `error_class` belongs only to a failure. A succeeded run with an error class,
  or a failed run without one, makes the taxonomy unqueryable — and the
  taxonomy is what adaptive backoff and the diagnostics read.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from core.enums import ErrorClass, JobRunStatus

TERMINAL_STATUSES = frozenset(
    {
        JobRunStatus.SUCCEEDED,
        JobRunStatus.FAILED,
        JobRunStatus.TIMED_OUT,
        JobRunStatus.CANCELLED,
    }
)


class JobRunEvent(StrEnum):
    """Events that move a run along its lifecycle."""

    CLAIMED = "claimed"
    SUCCEED = "succeed"
    FAIL = "fail"
    TIMEOUT = "timeout"
    CANCEL = "cancel"


class InvalidJobRunTransitionError(Exception):
    """A transition the state machine does not permit."""

    def __init__(self, current: JobRunStatus, event: JobRunEvent, reason: str) -> None:
        super().__init__(
            f"cannot apply {event.value} to a run in {current.value}: {reason}"
        )
        self.current = current
        self.event = event
        self.reason = reason


@dataclass(frozen=True, slots=True, kw_only=True)
class JobRunContext:
    """Evidence a transition may require.

    `error_class` is mandatory for a failure and forbidden elsewhere; the
    machine enforces that rather than trusting the caller to remember.
    """

    error_class: ErrorClass | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class JobRunTransition:
    status: JobRunStatus
    error_class: ErrorClass | None
    # Whether the run reached a state from which it can never move again.
    terminal: bool


def transition(
    current: JobRunStatus,
    event: JobRunEvent,
    context: JobRunContext | None = None,
) -> JobRunTransition:
    """Apply `event` to a run in `current`.

    Raises InvalidJobRunTransitionError for anything the documented table does
    not list, including every event on a terminal run.
    """
    ctx = context or JobRunContext()

    if is_terminal(current):
        raise InvalidJobRunTransitionError(
            current,
            event,
            "terminal runs never move; a retry is a new run linked by retry_of",
        )

    match (current, event):
        case (JobRunStatus.QUEUED, JobRunEvent.CLAIMED):
            return _running(JobRunStatus.RUNNING, None)
        case (JobRunStatus.QUEUED, JobRunEvent.CANCEL):
            # An unclaimed run has nothing to interrupt.
            return _running(JobRunStatus.CANCELLED, None)

        case (JobRunStatus.RUNNING, JobRunEvent.SUCCEED):
            if ctx.error_class is not None:
                raise InvalidJobRunTransitionError(
                    current,
                    event,
                    "a succeeded run cannot carry an error_class",
                )
            return _running(JobRunStatus.SUCCEEDED, None)

        case (JobRunStatus.RUNNING, JobRunEvent.FAIL):
            if ctx.error_class is None:
                raise InvalidJobRunTransitionError(
                    current,
                    event,
                    "a failed run must record an error_class",
                )
            return _running(JobRunStatus.FAILED, ctx.error_class)

        case (JobRunStatus.RUNNING, JobRunEvent.TIMEOUT):
            # The table allows an error_class here but does not require one:
            # a timeout is itself the diagnosis.
            return _running(JobRunStatus.TIMED_OUT, ctx.error_class)

        case (JobRunStatus.RUNNING, JobRunEvent.CANCEL):
            raise InvalidJobRunTransitionError(
                current,
                event,
                "cancelling a running run requires the task to support safe "
                "interruption, which collection does not yet",
            )

        case _:
            raise InvalidJobRunTransitionError(
                current, event, "the state machine does not define this transition"
            )


def _running(status: JobRunStatus, error_class: ErrorClass | None) -> JobRunTransition:
    return JobRunTransition(
        status=status,
        error_class=error_class,
        terminal=is_terminal(status),
    )


def is_terminal(status: JobRunStatus) -> bool:
    return status in TERMINAL_STATUSES


@dataclass(frozen=True, slots=True, kw_only=True)
class JobRunView:
    """The fields of a run that the state machine touches."""

    id: str
    job_id: str
    status: JobRunStatus
    correlation_id: str
    error_class: ErrorClass | None = None
    retry_of: str | None = None

    def __post_init__(self) -> None:
        if not self.correlation_id:
            raise ValueError("every JobRun requires a correlation_id")
        if self.status is JobRunStatus.SUCCEEDED and self.error_class is not None:
            raise ValueError("a succeeded run cannot carry an error_class")
        if self.status is JobRunStatus.FAILED and self.error_class is None:
            raise ValueError("a failed run must record an error_class")
