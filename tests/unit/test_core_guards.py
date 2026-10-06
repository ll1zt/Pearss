"""Targeted kills for mutants the mutation baseline showed surviving.

These tests exist because `just mutate` proved the guards below had no
unit-level assertion: the ValueError branches in merge_state /
adaptive_interval / next_interval and the processing-gate branch in
item_state.transition were only ever executed through integration tests,
where a removed guard would be masked by other failures. Each test here
names the branch it pins.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.enums import ItemEvent, PublishStatus, TimeoutPolicy
from core.item_state import InvalidTransitionError, TransitionContext, transition
from core.models import SchedulePolicy
from core.reader_state import ReadState, TagChange, hash_password, merge_state
from core.scheduling import adaptive_interval, next_interval

# Declared for the whole module. Without it these tests are invisible: the
# recipe targets select by marker, so pytest deselects everything unmarked.
# Only the single test that happened to carry its own decorator had ever run.
pytestmark = pytest.mark.unit

HOUR = 3600.0
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# merge_state: the naive-datetime and empty-id guards
# --------------------------------------------------------------------------


def test_merge_state_rejects_a_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        merge_state(
            item_id="i",
            current=None,
            change=TagChange(is_read=True),
            at=datetime(2026, 6, 1, 12, 0),  # naive
        )


def test_read_state_rejects_a_naive_updated_at() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        ReadState(
            item_id="i",
            is_read=True,
            updated_at=datetime(2026, 6, 1, 12, 0),  # naive
        )


def test_read_state_rejects_an_empty_item_id() -> None:
    with pytest.raises(ValueError, match="item_id"):
        ReadState(item_id="")


def test_hash_password_rejects_an_empty_password() -> None:
    with pytest.raises(ValueError, match="password"):
        hash_password("")


# --------------------------------------------------------------------------
# scheduling: the policy-type and missing-bound guards
# --------------------------------------------------------------------------


def test_adaptive_interval_rejects_a_non_adaptive_policy() -> None:
    fixed = SchedulePolicy(
        type="fixed", interval_seconds=1800, min_seconds=900, max_seconds=7200
    )

    with pytest.raises(ValueError, match="adaptive policy"):
        adaptive_interval([HOUR], fixed)


def test_adaptive_interval_rejects_a_policy_without_bounds() -> None:
    """A policy that skipped __post_init__ (hand-built / deserialised).

    SchedulePolicy's constructor refuses None bounds itself, so this guard is
    only reachable for duck-typed inputs -- which is exactly the accident it
    exists to catch: None leaking into min()/max() would silently corrupt the
    interval.
    """
    broken = cast(
        "Any",
        SimpleNamespace(type="adaptive", min_seconds=None, max_seconds=None),
    )

    with pytest.raises(ValueError, match="min_seconds and max_seconds"):
        adaptive_interval([HOUR], broken)


def test_next_interval_rejects_a_fixed_policy_without_an_interval() -> None:
    broken = cast("Any", SimpleNamespace(type="fixed", interval_seconds=None))

    with pytest.raises(ValueError, match="interval_seconds"):
        next_interval(broken)


def test_next_interval_backs_off_for_a_fixed_policy_on_failures() -> None:
    """The fixed-policy backoff branch, unit-level.

    The adaptive backoff was unit-covered; this branch (fixed + failures)
    was reachable only through integration tests, where its removal would be
    masked by the adaptive path also engaging.
    """
    fixed = SchedulePolicy(type="fixed", interval_seconds=1800)

    clean = next_interval(fixed)
    failing = next_interval(fixed, consecutive_failures=3)

    assert failing > clean, "consecutive failures must lengthen a fixed interval"


# --------------------------------------------------------------------------
# item_state: the processing gate, unit-level
# --------------------------------------------------------------------------


def test_mark_ready_is_refused_while_processing_is_incomplete() -> None:
    """The gate: a chained item stays pending until its products exist.

    The mutation baseline showed this branch was untested at unit level --
    an integration test exercises the whole pipeline, so a guard removed here
    would be masked by every other failure the pipeline produced.
    """
    with pytest.raises(InvalidTransitionError, match="gate stays closed"):
        transition(
            PublishStatus.PENDING,
            ItemEvent.MARK_READY,
            TransitionContext(processing_complete=False),
        )


def test_mark_ready_succeeds_once_processing_completes() -> None:
    result = transition(
        PublishStatus.PENDING,
        ItemEvent.MARK_READY,
        TransitionContext(processing_complete=True),
    )

    assert result.status is PublishStatus.READY


def test_a_timeout_holds_when_the_policy_says_hold() -> None:
    result = transition(
        PublishStatus.PENDING,
        ItemEvent.TIMEOUT,
        TransitionContext(timeout_policy=TimeoutPolicy.HOLD),
    )

    assert result.status is PublishStatus.PENDING


@pytest.mark.unit
def test_row_to_item_round_trips_the_author() -> None:
    """A stored byline must survive the row -> domain rebuild.

    row_to_item initially omitted author, so /v1/items served "" for every
    row even though the column held the value -- a one-way fix that only
    covered writes. This pins the read side too.
    """
    from adapters.db_models import Item as ItemRow
    from adapters.repository._shared import row_to_item
    from core.enums import PublishStatus

    row = ItemRow(
        id="i" * 64,
        origin_id="s" * 32,
        source_id="s" * 32,
        guid="g" * 64,
        content_hash="h" * 32,
        title="T",
        url="https://example.com/a",
        publish_status=PublishStatus.PUBLISHED.value,
        tenant_id="default",
        created_at="2026-09-13T00:00:00Z",
        updated_at="2026-09-13T00:00:00Z",
        author="Ada Example",
    )
    assert row_to_item(row).author == "Ada Example"
