"""Pure scheduling computations.

These are pure functions of their inputs, which is what makes them the natural
unit-test surface for section A4. Nothing here reads the clock, the database
or the network: callers supply the observed history and the result is
deterministic.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from core.models import SchedulePolicy

# Ceiling for fixed-interval backoff, unless the configured interval is longer:
# failures must never make a source fetch more frequently than its policy.
MAX_BACKOFF_SECONDS = 24 * 60 * 60

# Backoff growth base. Exponential so that a persistently failing source stops
# consuming attempts quickly, while a transient blip recovers within a few runs.
BACKOFF_BASE = 2.0

# Applied when there is no usable update history: assume a daily cadence rather
# than guessing something aggressive that would hammer a quiet feed.
DEFAULT_INTERVAL_SECONDS = 24 * 60 * 60


def adaptive_interval(
    observed_gaps: list[float],
    policy: SchedulePolicy,
    *,
    consecutive_failures: int = 0,
) -> float:
    """Compute the next fetch interval in seconds.

    `observed_gaps` are historical intervals between a source's actual updates,
    in seconds, oldest first. The returned value is always clamped to the
    policy's min/max bounds, so a single outlier gap cannot schedule a fetch
    absurdly far out or spin an aggressive source.

    The result is deterministic: identical inputs always produce an identical
    interval, including in the presence of ties.
    """
    if policy.type != "adaptive":
        raise ValueError(
            f"adaptive_interval requires an adaptive policy, got {policy.type!r}"
        )
    if policy.min_seconds is None or policy.max_seconds is None:
        # Unreachable via SchedulePolicy.__post_init__, but a hand-built or
        # deserialised policy could skip validation, and None here would
        # otherwise leak into min()/max() and silently corrupt the interval.
        raise ValueError("adaptive policy requires min_seconds and max_seconds")

    base = _base_interval(observed_gaps)
    backoff = _backoff_multiplier(consecutive_failures)
    return _clamp(base * backoff, policy.min_seconds, policy.max_seconds)


def next_interval(
    policy: SchedulePolicy,
    observed_gaps: list[float] | None = None,
    *,
    consecutive_failures: int = 0,
) -> float:
    """Next interval for a policy, dispatching on its type.

    `consecutive_failures` comes from run history with every decision, not from
    the policy: a policy parsed from JSON cannot carry a live counter, so a
    failure count stored there would always read as zero and the backoff would
    never engage for adaptive sources.
    """
    if policy.type == "fixed":
        if policy.interval_seconds is None:
            raise ValueError("fixed policy requires interval_seconds")
        interval = float(policy.interval_seconds)
        if consecutive_failures > 0:
            # Backoff may lengthen the wait but never shorten it below the
            # configured interval: for a weekly source, the 24h ceiling would
            # otherwise make a failure fetch seven times more often.
            interval = max(
                interval,
                min(
                    interval * _backoff_multiplier(consecutive_failures),
                    float(MAX_BACKOFF_SECONDS),
                ),
            )
        return interval
    return adaptive_interval(
        observed_gaps or [],
        policy,
        consecutive_failures=consecutive_failures,
    )


def _base_interval(observed_gaps: list[float]) -> float:
    """Representative update cadence for a source.

    The median, not the mean: one long gap (a holiday, a site outage) would
    drag a mean far from the typical cadence and quietly stop the source from
    being fetched often enough to notice it resumed.
    """
    usable = sorted(g for g in observed_gaps if g > 0)
    if not usable:
        return float(DEFAULT_INTERVAL_SECONDS)

    mid = len(usable) // 2
    if len(usable) % 2 == 1:
        return float(usable[mid])
    return (usable[mid - 1] + usable[mid]) / 2.0


def _backoff_multiplier(consecutive_failures: int) -> float:
    """Exponential backoff factor for a run of consecutive failures."""
    if consecutive_failures <= 0:
        return 1.0
    return min(BACKOFF_BASE**consecutive_failures, float(MAX_BACKOFF_SECONDS))


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(value, high))


def observed_gaps(timestamps: list[float]) -> list[float]:
    """Convert ascending update timestamps into the gaps between them.

    Kept pure and separate from any clock so tests can feed recorded history.
    """
    ordered = sorted(timestamps)
    # pairwise() yields nothing for fewer than two points, so a single
    # timestamp needs no special case.
    return [
        later - earlier
        for earlier, later in itertools.pairwise(ordered)
        if later > earlier
    ]


@dataclass(frozen=True, slots=True, kw_only=True)
class ScheduleDecision:
    """What the scheduler concluded about one source."""

    due: bool
    interval_seconds: float
    # When the next attempt becomes due. None when `due` is True, since the
    # answer to "when" is "now".
    next_due_at: datetime | None
    consecutive_failures: int


def decide_schedule(
    policy: SchedulePolicy,
    *,
    last_fetch_at: datetime | None,
    observed_update_gaps: list[float] | None = None,
    consecutive_failures: int = 0,
    now: datetime,
) -> ScheduleDecision:
    """Whether a source should be collected now, and if not, when.

    Pure by design: the answer is computed from the policy and the recorded
    history rather than from a stored next-run time, so changing a policy,
    backing off after a failure, or restoring the database from backup all
    become self-correcting. A stored schedule would need someone to remember
    to update it in each of those cases.

    `last_fetch_at` is None for a source that has never been fetched, which is
    treated as due: a newly added source should be collected without waiting
    out a full interval first.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    interval = next_interval(
        policy, observed_update_gaps, consecutive_failures=consecutive_failures
    )

    if last_fetch_at is None:
        return ScheduleDecision(
            due=True,
            interval_seconds=interval,
            next_due_at=None,
            consecutive_failures=consecutive_failures,
        )

    if last_fetch_at.tzinfo is None:
        raise ValueError("last_fetch_at must be timezone-aware")

    elapsed = (now - last_fetch_at).total_seconds()
    if elapsed < 0:
        # A fetch recorded in the future means clock skew or a restored backup
        # ahead of the wall clock. Treat it as due rather than waiting out a
        # negative interval, which would never resolve.
        return ScheduleDecision(
            due=True,
            interval_seconds=interval,
            next_due_at=None,
            consecutive_failures=consecutive_failures,
        )

    if is_due(elapsed, interval):
        return ScheduleDecision(
            due=True,
            interval_seconds=interval,
            next_due_at=None,
            consecutive_failures=consecutive_failures,
        )

    return ScheduleDecision(
        due=False,
        interval_seconds=interval,
        next_due_at=last_fetch_at + timedelta(seconds=interval),
        consecutive_failures=consecutive_failures,
    )


def is_due(elapsed_seconds: float, interval_seconds: float) -> bool:
    """Whether a source last fetched `elapsed_seconds` ago is due.

    Uses a small tolerance so floating-point interval arithmetic cannot leave a
    source permanently one microsecond short of due.
    """
    return math.isclose(elapsed_seconds, interval_seconds, rel_tol=1e-9) or (
        elapsed_seconds >= interval_seconds
    )
