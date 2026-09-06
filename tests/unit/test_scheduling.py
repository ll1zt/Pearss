"""Tests for adaptive scheduling (A4).

The statistic is a pure function, so these tests are deterministic and need no
clock. The properties that matter: determinism, clamping to the policy bounds,
resistance to a single outlier gap, and backoff on consecutive failures.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.models import SchedulePolicy
from core.scheduling import (
    MAX_BACKOFF_SECONDS,
    adaptive_interval,
    decide_schedule,
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
def test_next_interval_applies_failure_backoff() -> None:
    policy = SchedulePolicy(
        type="adaptive", min_seconds=int(HOUR), max_seconds=int(100 * HOUR)
    )
    assert next_interval(policy, [2 * HOUR], consecutive_failures=2) == pytest.approx(
        8 * HOUR
    )


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


# --------------------------------------------------------------------------
# Derived schedule: the answer the scheduler actually needs
# --------------------------------------------------------------------------


def fixed(seconds: int = 1800) -> SchedulePolicy:
    return SchedulePolicy(type="fixed", interval_seconds=seconds)


def adaptive_policy(min_s: int = 900, max_s: int = 86400) -> SchedulePolicy:
    return SchedulePolicy(type="adaptive", min_seconds=min_s, max_seconds=max_s)


NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def minutes_ago(minutes: float) -> datetime:
    return NOW - timedelta(minutes=minutes)


@pytest.mark.unit
def test_never_fetched_source_is_due_immediately() -> None:
    # A freshly added source should be collected without first waiting out a
    # whole interval, or adding a feed would appear to do nothing.
    decision = decide_schedule(fixed(), last_fetch_at=None, now=NOW)
    assert decision.due
    assert decision.next_due_at is None


@pytest.mark.unit
def test_recent_source_is_not_due_and_says_when() -> None:
    decision = decide_schedule(fixed(1800), last_fetch_at=minutes_ago(10), now=NOW)
    assert not decision.due
    assert decision.next_due_at == minutes_ago(10) + timedelta(seconds=1800)


@pytest.mark.unit
def test_stale_source_is_due() -> None:
    decision = decide_schedule(fixed(1800), last_fetch_at=minutes_ago(45), now=NOW)
    assert decision.due


@pytest.mark.unit
def test_restart_after_downtime_recovers_without_configuration() -> None:
    # The property the derived design buys for free: APScheduler needs
    # misfire_grace_time tuned to approximate it.
    decision = decide_schedule(fixed(1800), last_fetch_at=minutes_ago(3 * 60), now=NOW)
    assert decision.due


@pytest.mark.unit
def test_fixed_policy_backs_off_after_failures() -> None:
    # A fixed interval must not hammer a source that is failing. next_interval
    # folds the backoff in for adaptive policies; fixed ones need it too.
    one = decide_schedule(
        fixed(600), last_fetch_at=minutes_ago(15), consecutive_failures=1, now=NOW
    )
    two = decide_schedule(
        fixed(600), last_fetch_at=minutes_ago(15), consecutive_failures=2, now=NOW
    )
    none = decide_schedule(fixed(600), last_fetch_at=minutes_ago(15), now=NOW)
    assert none.interval_seconds == 600
    assert one.interval_seconds == 1200
    assert two.interval_seconds == 2400
    # The observable effect of backoff is a later next attempt, which is what
    # stops a blocked source from being hammered.
    assert one.next_due_at is not None and two.next_due_at is not None
    assert two.next_due_at > one.next_due_at > NOW


@pytest.mark.unit
def test_adaptive_policy_backs_off_too() -> None:
    # Regression: the failure count arrives from run history with every
    # decision. A counter read from the policy object itself is always zero
    # for a JSON-parsed policy, which left adaptive sources without backoff.
    calm = decide_schedule(
        SchedulePolicy(type="adaptive", min_seconds=60, max_seconds=100000),
        last_fetch_at=minutes_ago(20),
        observed_update_gaps=[600.0],
        now=NOW,
    )
    failing = decide_schedule(
        SchedulePolicy(type="adaptive", min_seconds=60, max_seconds=100000),
        last_fetch_at=minutes_ago(20),
        observed_update_gaps=[600.0],
        consecutive_failures=3,
        now=NOW,
    )
    assert failing.interval_seconds == pytest.approx(calm.interval_seconds * 8)


@pytest.mark.unit
def test_derived_backoff_is_capped() -> None:
    decision = decide_schedule(
        fixed(600),
        last_fetch_at=minutes_ago(1),
        consecutive_failures=50,
        now=NOW,
    )
    assert decision.interval_seconds <= MAX_BACKOFF_SECONDS


@pytest.mark.unit
def test_future_last_fetch_is_treated_as_due() -> None:
    # Clock skew or a backup restored ahead of the wall clock. Waiting out a
    # negative interval would never resolve, so the source is collected.
    decision = decide_schedule(fixed(), last_fetch_at=minutes_ago(-30), now=NOW)
    assert decision.due


@pytest.mark.unit
def test_naive_inputs_are_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        decide_schedule(fixed(), last_fetch_at=None, now=datetime(2026, 6, 1))
    with pytest.raises(ValueError, match="timezone-aware"):
        decide_schedule(fixed(), last_fetch_at=datetime(2026, 6, 1), now=NOW)


@pytest.mark.unit
def test_decision_reports_the_failure_count_it_used() -> None:
    decision = decide_schedule(
        fixed(), last_fetch_at=None, consecutive_failures=4, now=NOW
    )
    assert decision.consecutive_failures == 4


@pytest.mark.unit
def test_adaptive_interval_follows_observed_cadence() -> None:
    # A source that publishes hourly should be fetched roughly hourly, not on a
    # fixed guess.
    slow = decide_schedule(
        adaptive_policy(min_s=60, max_s=86400),
        last_fetch_at=minutes_ago(1),
        observed_update_gaps=[3600.0] * 6,
        now=NOW,
    )
    fast = decide_schedule(
        adaptive_policy(min_s=60, max_s=86400),
        last_fetch_at=minutes_ago(1),
        observed_update_gaps=[120.0] * 6,
        now=NOW,
    )
    assert slow.interval_seconds == pytest.approx(3600.0)
    assert fast.interval_seconds == pytest.approx(120.0)
