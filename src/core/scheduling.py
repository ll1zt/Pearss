"""Pure scheduling computations.

These are pure functions of their inputs, which is what makes them the natural
unit-test surface for section A4. Nothing here reads the clock, the database
or the network: callers supply the observed history and the result is
deterministic.
"""

from __future__ import annotations

import itertools
import math

from core.models import SchedulePolicy

# Longest backoff we will schedule. Beyond about a day, a fixed-interval source
# is indistinguishable from one that has been abandoned, and the operator
# should be looking at it rather than the scheduler quietly deferring it.
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
) -> float:
    """Next interval for a policy, dispatching on its type."""
    if policy.type == "fixed":
        if policy.interval_seconds is None:
            raise ValueError("fixed policy requires interval_seconds")
        return float(policy.interval_seconds)
    return adaptive_interval(
        observed_gaps or [],
        policy,
        consecutive_failures=policy.consecutive_failures,
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


def is_due(elapsed_seconds: float, interval_seconds: float) -> bool:
    """Whether a source last fetched `elapsed_seconds` ago is due.

    Uses a small tolerance so floating-point interval arithmetic cannot leave a
    source permanently one microsecond short of due.
    """
    return math.isclose(elapsed_seconds, interval_seconds, rel_tol=1e-9) or (
        elapsed_seconds >= interval_seconds
    )
