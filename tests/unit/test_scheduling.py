"""Tests for adaptive scheduling (A4).

The statistic is a pure function, so these tests are deterministic and need no
clock. The properties that matter: determinism, clamping to the policy bounds,
resistance to a single outlier gap, and backoff on consecutive failures.
"""

from __future__ import annotations

import pytest

from core.models import SchedulePolicy
from core.scheduling import (
    adaptive_interval,
    is_due,
    next_interval,
    observed_gaps,
)

HOUR = 3600.0


def adaptive(min_h: float = 1.0, max_h: float = 24.0) -> SchedulePolicy:
    return SchedulePolicy(
        type="adaptive",
        min_seconds=int(min_h * HOUR),
        max_seconds=int(max_h * HOUR),
    )


@pytest.mark.unit
def test_median_drives_the_interval_not_the_mean() -> None:
    # One long gap would drag a mean far out; the median stays representative.
    gaps = [HOUR, HOUR, HOUR, 200 * HOUR]
    result = adaptive_interval(gaps, adaptive(min_h=1, max_h=240))
    assert result == pytest.approx(HOUR)


@pytest.mark.unit
def test_even_count_uses_the_midpoint() -> None:
    result = adaptive_interval([2 * HOUR, 4 * HOUR], adaptive(min_h=1, max_h=24))
    assert result == pytest.approx(3 * HOUR)


@pytest.mark.unit
def test_result_is_clamped_to_policy_bounds() -> None:
    low = adaptive_interval([1.0], adaptive(min_h=6, max_h=24))
    high = adaptive_interval([1000 * HOUR], adaptive(min_h=1, max_h=12))
    assert low == pytest.approx(6 * HOUR)
    assert high == pytest.approx(12 * HOUR)


@pytest.mark.unit
def test_empty_history_falls_back_to_a_daily_cadence() -> None:
    result = adaptive_interval([], adaptive(min_h=1, max_h=24))
    assert result == pytest.approx(24 * HOUR)


@pytest.mark.unit
def test_non_positive_gaps_are_ignored() -> None:
    assert adaptive_interval([0.0, -5.0], adaptive()) == pytest.approx(24 * HOUR)


@pytest.mark.unit
def test_same_input_always_gives_same_output() -> None:
    gaps = [3 * HOUR, 7 * HOUR, 5 * HOUR]
    first = adaptive_interval(gaps, adaptive())
    second = adaptive_interval(list(reversed(gaps)), adaptive())
    assert first == second


@pytest.mark.unit
def test_consecutive_failures_back_off_exponentially() -> None:
    policy = adaptive(min_h=1, max_h=1000)
    gaps = [2 * HOUR]
    clean = adaptive_interval(gaps, policy, consecutive_failures=0)
    one = adaptive_interval(gaps, policy, consecutive_failures=1)
    two = adaptive_interval(gaps, policy, consecutive_failures=2)
    assert one == pytest.approx(clean * 2)
    assert two == pytest.approx(clean * 4)


@pytest.mark.unit
def test_backoff_is_capped() -> None:
    policy = adaptive(min_h=1, max_h=24)
    result = adaptive_interval([2 * HOUR], policy, consecutive_failures=99)
    assert result == pytest.approx(24 * HOUR)


@pytest.mark.unit
def test_adaptive_interval_rejects_a_fixed_policy() -> None:
    fixed = SchedulePolicy(type="fixed", interval_seconds=1800)
    with pytest.raises(ValueError, match="adaptive"):
        adaptive_interval([HOUR], fixed)


@pytest.mark.unit
def test_next_interval_dispatches_on_policy_type() -> None:
    fixed = SchedulePolicy(type="fixed", interval_seconds=1800)
    assert next_interval(fixed) == pytest.approx(1800.0)
    assert next_interval(adaptive(), [2 * HOUR]) == pytest.approx(2 * HOUR)


@pytest.mark.unit
def test_next_interval_uses_policy_failure_count() -> None:
    policy = SchedulePolicy(
        type="adaptive",
        min_seconds=int(HOUR),
        max_seconds=int(100 * HOUR),
        consecutive_failures=2,
    )
    assert next_interval(policy, [2 * HOUR]) == pytest.approx(8 * HOUR)


@pytest.mark.unit
def test_observed_gaps_sorts_and_drops_zero_deltas() -> None:
    # Sorted: 10, 60, 60, 100 -> gaps 50, 0, 40 -> the duplicate drops out.
    assert observed_gaps([100.0, 10.0, 60.0, 60.0]) == [50.0, 40.0]


@pytest.mark.unit
def test_observed_gaps_handles_short_input() -> None:
    assert observed_gaps([]) == []
    assert observed_gaps([42.0]) == []


@pytest.mark.unit
def test_is_due_respects_the_interval() -> None:
    assert is_due(3601.0, 3600.0)
    assert not is_due(3599.0, 3600.0)
    assert is_due(3600.0, 3600.0)


@pytest.mark.unit
def test_policy_validation_rejects_nonsense() -> None:
    with pytest.raises(ValueError):
        SchedulePolicy(type="fixed")
    with pytest.raises(ValueError):
        SchedulePolicy(type="adaptive", min_seconds=100, max_seconds=10)
    with pytest.raises(ValueError):
        SchedulePolicy(type="mystery")
