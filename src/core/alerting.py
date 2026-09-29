"""When pearss should call for a human or an agent (ADR 0034).

The lesson behind this module is a 15-hour silent outage: 56 of 56 sources were
failing, `recent_run_success_rate` said `0.0`, and **nothing ever said so** —
the data was on an endpoint nobody was polling. The fix is not a better
dashboard; it is that the system speaks up when a person (or an agent) has to
act.

This module is the *decision*: given the aggregate state, is intervention
needed, and what should the message say. It performs no I/O — sending belongs to
`adapters.alerting`, and the split keeps `core/` free of frameworks (AGENTS.md).

Two rules shape everything here:

- **Alert on edges, not levels.** A state that persists must produce one message,
  not one per tick. Fifty-six sources failing at once turned into fifty-six
  notifications is how alerting gets muted, and a muted channel is the original
  bug with extra steps.
- **Only report what needs a person.** "A fetch failed" is normal operation and
  the retry policy's business. "Every source is failing", "a source has
  exhausted its retries", "the loop is dead" are things nothing else can fix.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

# Thresholds, as defaults rather than constants so a deployment can tune them and
# so tests can exercise both sides of each boundary without monkeypatching.
#
# `success_rate_threshold`: below this, collection is broadly broken rather than
# a single source having a bad day.
# `min_runs`: the rate is computed over however many runs have finished, so with
# one run it is 0.0 or 1.0 — a brand-new deployment would alert on its first
# collection attempt. An alert that fires when nothing is wrong is how alerting
# gets turned off.
SUCCESS_RATE_THRESHOLD: Final = 0.5
MIN_RUNS: Final = 10
#: A persistent outage re-notifies at most this often. Recovery is never delayed
#: (see `AlertState.should_send`): it is the one message worth having promptly.
MIN_INTERVAL_SECONDS: Final = 3600.0


@dataclass(frozen=True, slots=True, kw_only=True)
class AlertDefaults:
    """The tunables, bundled so callers can pass them through in one go."""

    success_rate_threshold: float = SUCCESS_RATE_THRESHOLD
    min_runs: int = MIN_RUNS
    min_interval_seconds: float = MIN_INTERVAL_SECONDS


ALERT_DEFAULTS: Final = AlertDefaults()


@dataclass(frozen=True, slots=True, kw_only=True)
class HealthVerdict:
    """Core's answer to "does someone need to act, and why".

    `reason` is a human sentence, not a code: it lands in a notification a
    person reads at an inconvenient hour, and `degraded` alone would make them
    go and look up what it means.

    `dominant_error_class` and `dominant_error_count` are carried so the message
    can name the direction. In the motivating incident, "all network, all in the
    same minute" pointed straight at a shared dependency instead of 56 sources.
    """

    degraded: bool
    reason: str = ""
    success_rate: float | None = None
    runs_considered: int = 0
    dominant_error_class: str | None = None
    dominant_error_count: int = 0
    # The two conditions ADR 0034 named and left unimplemented (issue #61).
    # Both arrive as *inputs*: each fact is detected where it lives -- a chain's
    # rows in the database, a loop's task object -- and this module only decides
    # what to say about them. A loop cannot report its own death, so the liveness
    # fact comes from whichever *other* loop is still running.
    exhausted_sources: tuple[str, ...] = ()
    dead_loops: tuple[str, ...] = ()


def decide_health(
    snapshot: dict[str, object],
    *,
    success_rate_threshold: float = SUCCESS_RATE_THRESHOLD,
    min_runs: int = MIN_RUNS,
    min_interval_seconds: float = MIN_INTERVAL_SECONDS,
    exhausted_sources: Sequence[str] = (),
    dead_loops: Sequence[str] = (),
) -> HealthVerdict:
    """Whether the aggregate state needs intervention (ADR 0034).

    Reads the same shape `system_snapshot` produces, so the numbers in a
    notification are the numbers the operator (or agent) would have fetched by
    hand — one source of truth, not a second computation that can disagree.

    Currently one criterion: a collapsed recent success rate over enough runs.
    The other two conditions in the ADR (retries exhausted on a source, a dead
    background loop) are detected where those facts live — the worker and the
    task itself — and report through the same sender.
    """
    del min_interval_seconds  # the interval governs sending, not the verdict
    rate = snapshot.get("recent_run_success_rate")
    raw_considered = snapshot.get("recent_runs_considered")
    # Read through an explicit isinstance rather than a coercion: the snapshot is
    # a plain dict crossing from the adapter layer, so the type guard is the
    # honest way to narrow it.
    considered = raw_considered if isinstance(raw_considered, int) else 0
    if not isinstance(rate, (int, float)):
        # No finished runs yet (`None`), or a shape we do not understand: there
        # is nothing to conclude, and concluding "fine" from a missing number
        # would be the same silence this module exists to end. Reporting
        # "degraded" on unreadable input would be worse still -- it would fire
        # on every fresh deployment.
        return HealthVerdict(degraded=False, runs_considered=considered)
    rate = float(rate)
    dominant_class, dominant_count = _dominant_error(snapshot)
    # Conditions that are true regardless of the rate: a chain that can never
    # finish, and a loop that stopped. Both are worse than a low rate -- a
    # source whose every queued product failed is not "slow", it is stuck, and
    # nothing will retry it without a person.
    extra: list[str] = []
    if exhausted_sources:
        extra.append(
            f"{len(exhausted_sources)} source(s) with a chain that cannot "
            f"finish: {', '.join(sorted(exhausted_sources))}"
        )
    if dead_loops:
        extra.append(f"background loop(s) not running: {', '.join(dead_loops)}")
    if considered < min_runs:
        return HealthVerdict(
            degraded=bool(extra),
            reason="; ".join(extra),
            success_rate=rate,
            runs_considered=considered,
            dominant_error_class=dominant_class,
            dominant_error_count=dominant_count,
            exhausted_sources=tuple(sorted(exhausted_sources)),
            dead_loops=tuple(sorted(dead_loops)),
        )
    if rate >= success_rate_threshold:
        return HealthVerdict(
            degraded=bool(extra),
            reason="; ".join(extra),
            success_rate=rate,
            runs_considered=considered,
            dominant_error_class=dominant_class,
            dominant_error_count=dominant_count,
            exhausted_sources=tuple(sorted(exhausted_sources)),
            dead_loops=tuple(sorted(dead_loops)),
        )

    dominant_class, dominant_count = _dominant_error(snapshot)
    reason = (
        f"collection is failing: {rate:.0%} of the last {considered} runs "
        f"succeeded (threshold {success_rate_threshold:.0%})"
    )
    if dominant_class:
        reason += f"; dominant failure is {dominant_class} ({dominant_count} runs)"
    if extra:
        reason += "; " + "; ".join(extra)
    return HealthVerdict(
        degraded=True,
        reason=reason,
        success_rate=rate,
        runs_considered=considered,
        dominant_error_class=dominant_class,
        dominant_error_count=dominant_count,
        exhausted_sources=tuple(sorted(exhausted_sources)),
        dead_loops=tuple(sorted(dead_loops)),
    )


def _dominant_error(snapshot: dict[str, object]) -> tuple[str | None, int]:
    """The most common error class in the snapshot, and its count.

    `runs_by_error_class` is produced with every class present (see
    `system_snapshot`'s `every=` discipline), so a zero count here is a real
    zero rather than a missing key.
    """
    by_class = snapshot.get("runs_by_error_class")
    if not isinstance(by_class, dict) or not by_class:
        return None, 0
    best_class: str | None = None
    best_count = 0
    for name, count in by_class.items():
        if not isinstance(count, int):
            # A shape we do not understand: skip it rather than guess. Falling
            # back to zero would be indistinguishable from "no failures", which
            # is the reading this module exists to avoid manufacturing.
            continue
        if count > best_count:
            best_class, best_count = str(name), count
    return best_class, best_count


def alert_body(verdict: HealthVerdict) -> str:
    """The notification text for a verdict, built from fixed fields.

    Constructed field by field on purpose rather than by interpolating an
    exception or a row: AGENTS.md forbids tokens, cookies, authorization headers
    and private URLs in anything that leaves the host, and a notification body is
    exactly that. Counting and classifying — never quoting — is what keeps that
    promise enforceable.
    """
    if not verdict.degraded:
        return "collection recovered: runs are succeeding again"
    lines = [verdict.reason or "collection needs attention"]
    if verdict.success_rate is not None:
        lines.append(
            f"success rate {verdict.success_rate:.0%} "
            f"over {verdict.runs_considered} runs"
        )
    if verdict.dominant_error_class:
        lines.append(
            f"dominant error class: {verdict.dominant_error_class} "
            f"({verdict.dominant_error_count} runs)"
        )
    for source in verdict.exhausted_sources:
        lines.append(f"stuck chain: source {source}")
    for loop in verdict.dead_loops:
        lines.append(f"not running: {loop}")
    lines.append("action: check the shared dependency (proxy, credentials, network)")
    return "\n".join(lines)


@dataclass(slots=True)
class AlertState:
    """Edge detection and rate limiting for one process (ADR 0034).

    Deliberately in memory, not in the database. What it tracks is "have we
    already said this", which is a property of this process's alerting, and
    storing it would tie the alert path to the very database whose failure is
    one of the things worth alerting about.
    """

    degraded: bool = False
    last_sent_at: float | None = None

    def should_send(
        self, verdict: HealthVerdict, *, now: float, min_interval_seconds: float
    ) -> bool:
        """Whether this verdict warrants a message, and update the state.

        Three cases, and the middle one is the point:

        - A change of state (healthy -> degraded, or the reverse) always sends:
          the transition is the news.
        - A *continuing* degradation sends again only once the interval has
          passed, so a long outage is not a per-tick drumbeat.
        - Recovery ignores the interval entirely: it is the one message an
          operator wants immediately, and withholding it to honour a rate limit
          meant for repeats would be a strange kind of politeness.
        """
        was_degraded = self.degraded
        self.degraded = verdict.degraded

        if verdict.degraded == was_degraded:
            if not verdict.degraded:
                return False
            # Still degraded: re-notify only after the interval.
            if self.last_sent_at is None:
                return True
            if now - self.last_sent_at < min_interval_seconds:
                return False
            self.last_sent_at = now
            return True

        self.last_sent_at = now
        return True
