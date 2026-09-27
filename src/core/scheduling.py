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
from typing import Any

from core.enums import ErrorClass
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
    error_class: ErrorClass | None = None,
    rng: Any = None,
) -> float:
    """Compute the next fetch interval in seconds.

    `observed_gaps` are historical intervals between a source's actual updates,
    in seconds, oldest first. The returned value is always clamped to the
    policy's min/max bounds, so a single outlier gap cannot schedule a fetch
    absurdly far out or spin an aggressive source.

    `error_class` wires the retry table in docs/domain/error-taxonomy.md into
    the interval (issue #5): a class with `allows_automatic_retry == False`
    (blocked / auth / structure_changed) does not back off -- the service
    already refused us or the parser is broken, and stretching the wait only
    delays the probe that would notice a fix. Retryable classes (network /
    parse) and an unknown class keep the exponential backoff.

    `rng` carries bounded jitter (±20%) on the backoff so same-tick failures
    retry spread out instead of spiking the upstream that just refused us.
    None keeps the computation deterministic (tests); production passes a
    `random.Random`.
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
    multiplier = _failure_multiplier(consecutive_failures, error_class, rng)
    return _clamp(base * multiplier, policy.min_seconds, policy.max_seconds)


def _failure_multiplier(
    consecutive_failures: int, error_class: ErrorClass | None, rng: Any
) -> float:
    """Backoff multiplier for a failure streak, per the retry table.

    A non-retryable class never grows the wait; a retryable (or unknown)
    one multiplies exponentially and, when an rng is supplied, by a factor
    in [0.8, 1.2]. Without an rng the exact exponential value is returned
    (deterministic; the historical behaviour).
    """
    if consecutive_failures <= 0:
        return 1.0
    if error_class is not None and not error_class.allows_automatic_retry:
        return 1.0
    multiplier = _backoff_multiplier(consecutive_failures)
    if rng is not None:
        multiplier *= rng.uniform(0.8, 1.2)
    return multiplier


def next_interval(
    policy: SchedulePolicy,
    observed_gaps: list[float] | None = None,
    *,
    consecutive_failures: int = 0,
    error_class: ErrorClass | None = None,
    rng: Any = None,
) -> float:
    """Next interval for a policy, dispatching on its type.

    `consecutive_failures` comes from run history with every decision, not from
    the policy: a policy parsed from JSON cannot carry a live counter, so a
    failure count stored there would always read as zero and the backoff would
    never engage for adaptive sources.

    `error_class` is the most recent failure's classification (issue #5): it
    routes the retry table -- non-retryable classes probe at the normal pace.
    `rng` adds bounded jitter to the backoff; None stays deterministic.
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
                    interval
                    * _failure_multiplier(consecutive_failures, error_class, rng),
                    float(MAX_BACKOFF_SECONDS),
                ),
            )
        return interval
    return adaptive_interval(
        observed_gaps or [],
        policy,
        consecutive_failures=consecutive_failures,
        error_class=error_class,
        rng=rng,
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
    """Exponential backoff factor for a failure streak of n >= 1.

    The zero/negative case never reaches here: `_failure_multiplier` returns
    1.0 for it (a clean source has no backoff to compute).
    """
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
    # The most recent failure's classification, carried out so a caller can say
    # *why* a source is waiting. `failing: 3 consecutive failures, class network`
    # is actionable; `failing: 3 consecutive failures` sends the reader back to
    # the run history to find out what actually broke (issue #4, mode 2: a source
    # in backoff used to render identically to a healthy one in the journal).
    error_class: ErrorClass | None = None


def decide_schedule(
    policy: SchedulePolicy,
    *,
    last_fetch_at: datetime | None,
    observed_update_gaps: list[float] | None = None,
    consecutive_failures: int = 0,
    error_class: ErrorClass | None = None,
    rng: Any = None,
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

    `error_class` and `rng` pass through to `next_interval` (issue #5): the
    retry table decides whether a failure streak backs off, and jitter
    spreads same-tick retries. Production supplies both; tests may omit
    them for the deterministic legacy behaviour.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    interval = next_interval(
        policy,
        observed_update_gaps,
        consecutive_failures=consecutive_failures,
        error_class=error_class,
        rng=rng,
    )

    if last_fetch_at is None:
        return ScheduleDecision(
            due=True,
            interval_seconds=interval,
            next_due_at=None,
            consecutive_failures=consecutive_failures,
            error_class=error_class,
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
            error_class=error_class,
        )

    if is_due(elapsed, interval):
        return ScheduleDecision(
            due=True,
            interval_seconds=interval,
            next_due_at=None,
            consecutive_failures=consecutive_failures,
            error_class=error_class,
        )

    return ScheduleDecision(
        due=False,
        interval_seconds=interval,
        next_due_at=last_fetch_at + timedelta(seconds=interval),
        consecutive_failures=consecutive_failures,
        error_class=error_class,
    )


def is_due(elapsed_seconds: float, interval_seconds: float) -> bool:
    """Whether a source last fetched `elapsed_seconds` ago is due.

    Uses a small tolerance so floating-point interval arithmetic cannot leave a
    source permanently one microsecond short of due.
    """
    return math.isclose(elapsed_seconds, interval_seconds, rel_tol=1e-9) or (
        elapsed_seconds >= interval_seconds
    )
