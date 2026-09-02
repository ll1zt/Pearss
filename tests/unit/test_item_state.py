"""Tests for the Item publish state machine.

Each legal transition in docs/domain/state-machines.md gets a case, and each
invariant gets one. The illegal transitions matter as much as the legal ones:
silently accepting one is how published content ends up mutated.
"""

from __future__ import annotations

import pytest

from core.enums import ItemEvent, PublishStatus, TimeoutPolicy
from core.item_state import (
    InvalidTransitionError,
    TransitionContext,
    is_content_frozen,
    is_visible,
    transition,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("event", "context"),
    [
        (ItemEvent.MARK_READY, TransitionContext(processing_complete=True)),
        (ItemEvent.ENRICHMENT_FAILED, TransitionContext()),
        (ItemEvent.TIMEOUT, TransitionContext()),
    ],
)
def test_legal_pending_transitions(
    event: ItemEvent, context: TransitionContext
) -> None:
    result = transition(PublishStatus.PENDING, event, context)
    assert result.status in {PublishStatus.PENDING, PublishStatus.READY}


@pytest.mark.unit
def test_pending_becomes_ready_when_processing_completes() -> None:
    result = transition(
        PublishStatus.PENDING,
        ItemEvent.MARK_READY,
        TransitionContext(processing_complete=True),
    )
    assert result.status is PublishStatus.READY


@pytest.mark.unit
def test_gate_stays_closed_while_processing_incomplete() -> None:
    with pytest.raises(InvalidTransitionError, match="gate stays closed"):
        transition(
            PublishStatus.PENDING,
            ItemEvent.MARK_READY,
            TransitionContext(processing_complete=False),
        )


@pytest.mark.unit
def test_enrichment_failure_keeps_item_pending() -> None:
    result = transition(PublishStatus.PENDING, ItemEvent.ENRICHMENT_FAILED)
    assert result.status is PublishStatus.PENDING
    assert not is_visible(result.status)


@pytest.mark.unit
def test_timeout_holds_by_default() -> None:
    result = transition(PublishStatus.PENDING, ItemEvent.TIMEOUT)
    assert result.status is PublishStatus.PENDING
    assert result.audit_note is not None


@pytest.mark.unit
def test_timeout_can_publish_original_when_policy_says_so() -> None:
    result = transition(
        PublishStatus.PENDING,
        ItemEvent.TIMEOUT,
        TransitionContext(timeout_policy=TimeoutPolicy.PUBLISH_ORIGINAL),
    )
    assert result.status is PublishStatus.READY


@pytest.mark.unit
def test_force_publish_requires_confirmation() -> None:
    with pytest.raises(InvalidTransitionError, match="confirmation"):
        transition(PublishStatus.PENDING, ItemEvent.FORCE_PUBLISH)


@pytest.mark.unit
def test_force_publish_requires_a_reason() -> None:
    with pytest.raises(InvalidTransitionError, match="reason"):
        transition(
            PublishStatus.PENDING,
            ItemEvent.FORCE_PUBLISH,
            TransitionContext(force_publish_confirmed=True),
        )


@pytest.mark.unit
def test_force_publish_records_an_audit_note() -> None:
    result = transition(
        PublishStatus.PENDING,
        ItemEvent.FORCE_PUBLISH,
        TransitionContext(
            force_publish_confirmed=True,
            force_publish_reason="translation service is down",
        ),
    )
    assert result.status is PublishStatus.READY
    assert result.audit_note is not None
    assert "translation service is down" in result.audit_note


@pytest.mark.unit
def test_first_publish_freezes_content() -> None:
    result = transition(PublishStatus.READY, ItemEvent.FIRST_PUBLISH)
    assert result.status is PublishStatus.PUBLISHED
    assert is_content_frozen(result.status)
    assert is_visible(result.status)


@pytest.mark.unit
def test_published_accepts_only_read_state_changes() -> None:
    result = transition(PublishStatus.PUBLISHED, ItemEvent.READ_STATE_CHANGED)
    assert result.status is PublishStatus.PUBLISHED

    for event in (
        ItemEvent.MARK_READY,
        ItemEvent.ENRICHMENT_FAILED,
        ItemEvent.TIMEOUT,
        ItemEvent.FORCE_PUBLISH,
        ItemEvent.WITHDRAW,
    ):
        with pytest.raises(InvalidTransitionError):
            transition(PublishStatus.PUBLISHED, event)


@pytest.mark.unit
def test_ready_can_be_withdrawn_before_exposure() -> None:
    result = transition(
        PublishStatus.READY,
        ItemEvent.WITHDRAW,
        TransitionContext(withdraw_reason="processor output was wrong"),
    )
    assert result.status is PublishStatus.PENDING


@pytest.mark.unit
def test_ready_cannot_be_withdrawn_after_exposure() -> None:
    with pytest.raises(InvalidTransitionError, match="already exposed"):
        transition(
            PublishStatus.READY,
            ItemEvent.WITHDRAW,
            TransitionContext(
                exposed_to_any_client=True,
                withdraw_reason="too late",
            ),
        )


@pytest.mark.unit
def test_withdraw_requires_a_reason() -> None:
    with pytest.raises(InvalidTransitionError, match="reason"):
        transition(PublishStatus.READY, ItemEvent.WITHDRAW)


@pytest.mark.unit
def test_pending_is_never_visible() -> None:
    assert not is_visible(PublishStatus.PENDING)
    assert is_visible(PublishStatus.READY)
    assert is_visible(PublishStatus.PUBLISHED)


@pytest.mark.unit
def test_invalid_transition_carries_context() -> None:
    with pytest.raises(InvalidTransitionError) as excinfo:
        transition(PublishStatus.PUBLISHED, ItemEvent.MARK_READY)
    assert excinfo.value.current is PublishStatus.PUBLISHED
    assert excinfo.value.event is ItemEvent.MARK_READY
