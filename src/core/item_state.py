"""Item publish state machine.

This is the executable form of the transition table in
docs/domain/state-machines.md. Concentrating transitions here — rather than in
route handlers or ORM hooks — is what makes the "each item is published once"
guarantee testable without a database.

The invariant that drives every rule here: RSS clients cache the first version
of an item they fetch. Replacing content after publication does not correct
what the user already sees, so `published` is frozen and the gate exists to
prevent publishing too early rather than to allow correcting afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.enums import (
    ItemEvent,
    PublishStatus,
    TimeoutPolicy,
)


class InvalidTransitionError(Exception):
    """Raised when a transition is not legal for the item's current state.

    Callers should surface this as HTTP 409 Conflict: the request was
    well-formed, but the resource is not in a state that permits it.
    """

    def __init__(self, current: PublishStatus, event: ItemEvent, reason: str) -> None:
        super().__init__(
            f"cannot apply {event.value} to an item in {current.value}: {reason}"
        )
        self.current = current
        self.event = event
        self.reason = reason


@dataclass(frozen=True, slots=True, kw_only=True)
class TransitionContext:
    """Everything a transition decision needs beyond the current state.

    `force_publish` and timeout handling are conditional on confirmation or
    policy, so those conditions are passed in explicitly. Keeping them out of
    the state machine's own attributes means the machine stays a pure function
    of (state, event, context).
    """

    # True when an enrichment chain is configured and every required
    # enrichment succeeded, or when no chain is configured at all.
    processing_complete: bool = True
    # What to do when processing exceeds its budget.
    timeout_policy: TimeoutPolicy = TimeoutPolicy.HOLD
    # `force_publish` is only legal once the user has confirmed through MCP
    # elicitation and supplied a reason. Without both, the transition is
    # rejected rather than quietly recorded.
    force_publish_confirmed: bool = False
    force_publish_reason: str | None = None
    # `withdraw` is legal only while no client has seen the item.
    exposed_to_any_client: bool = False
    withdraw_reason: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class TransitionResult:
    """Outcome of applying an event."""

    status: PublishStatus
    # Set when the transition is one that must be written to the audit log:
    # a timeout downgrade or a user-confirmed override of the gate.
    audit_note: str | None = None


def transition(
    current: PublishStatus,
    event: ItemEvent,
    context: TransitionContext | None = None,
) -> TransitionResult:
    """Apply `event` to an item in `current`, returning the resulting state.

    Raises `InvalidTransitionError` for any transition the state machine does
    not list, so an unmodelled change fails loudly instead of silently
    mutating published content.
    """
    ctx = context or TransitionContext()

    match (current, event):
        case (PublishStatus.PENDING, ItemEvent.MARK_READY):
            if not ctx.processing_complete:
                raise InvalidTransitionError(
                    current,
                    event,
                    "processing chain is not complete; the gate stays closed",
                )
            return TransitionResult(status=PublishStatus.READY)

        case (PublishStatus.PENDING, ItemEvent.ENRICHMENT_FAILED):
            # Stays pending. The whole point of the gate: a failed enrichment
            # must not leak the unprocessed item into a feed.
            return TransitionResult(status=PublishStatus.PENDING)

        case (PublishStatus.PENDING, ItemEvent.TIMEOUT):
            if ctx.timeout_policy is TimeoutPolicy.PUBLISH_ORIGINAL:
                return TransitionResult(
                    status=PublishStatus.READY,
                    audit_note="timeout: downgraded to publish_original",
                )
            return TransitionResult(
                status=PublishStatus.PENDING,
                audit_note="timeout: held pending per policy",
            )

        case (PublishStatus.PENDING, ItemEvent.FORCE_PUBLISH):
            if not ctx.force_publish_confirmed:
                raise InvalidTransitionError(
                    current,
                    event,
                    "force_publish requires user confirmation",
                )
            if not ctx.force_publish_reason:
                raise InvalidTransitionError(
                    current,
                    event,
                    "force_publish requires a recorded reason",
                )
            return TransitionResult(
                status=PublishStatus.READY,
                audit_note=f"force_publish override: {ctx.force_publish_reason}",
            )

        case (PublishStatus.READY, ItemEvent.FIRST_PUBLISH):
            return TransitionResult(status=PublishStatus.PUBLISHED)

        case (PublishStatus.READY, ItemEvent.WITHDRAW):
            if ctx.exposed_to_any_client:
                raise InvalidTransitionError(
                    current,
                    event,
                    "item was already exposed to a client; it cannot be withdrawn",
                )
            if not ctx.withdraw_reason:
                raise InvalidTransitionError(
                    current,
                    event,
                    "withdraw requires a recorded reason",
                )
            return TransitionResult(
                status=PublishStatus.PENDING,
                audit_note=f"withdrawn before exposure: {ctx.withdraw_reason}",
            )

        case (PublishStatus.PUBLISHED, ItemEvent.READ_STATE_CHANGED):
            # Consumption state changes never touch published content.
            return TransitionResult(status=PublishStatus.PUBLISHED)

        case _:
            raise InvalidTransitionError(
                current,
                event,
                "the state machine does not define this transition",
            )


def is_visible(status: PublishStatus) -> bool:
    """Whether an item in `status` may appear in RSS or the Reader API.

    Pending items are invisible everywhere: a feed that later swaps in
    different content under the same GUID only corrupts the client's cache.
    """
    return status is not PublishStatus.PENDING


def is_content_frozen(status: PublishStatus) -> bool:
    """Whether the item's body, title, URL, GUID and content hash are locked."""
    return status is PublishStatus.PUBLISHED
