"""Model health across rows: prefer the first, demote what fails, recover.

A provider chain is tried in order, and the order used to be the whole memory:
a model that had just failed was asked first again on the next row, every
sweep, forever, while the model that answered waited behind it. The live
symptom (issue #227's journal) is a stalled model spending the row's budget on
every attempt -- 17 `processor deadline expired` rows in 24h, each one paying
for the same broken model before reaching the healthy one.

Why the state lives here and not in the processor: processors run in a
sandboxed child spawned per row (`app/processor_runtime`), so a counter inside
one cannot outlive it. The host is the only process that sees the whole queue --
the same argument that puts fetch pacing here (`app/fetch_pacing`, ADR 0029).
The state is in memory on purpose: a restart forgetting which model was
misbehaving costs one extra failed attempt, while a table would be a second
fact source beside the provider's own behaviour (ADR 0009's reasoning).

What that means for the contract (ADR 0048): the policy travels *into* the
child as data (`item_ctx["model_policy"]`), and the child reports per model what
happened (`model_events`). A processor's `process()` signature is frozen
(ADR 0017), so data is the only channel -- and it is the only way the host can
learn that "model A answered 503 and model B served the row".

The cooldown escalates because the failures it answers are episodes, not
events: 5 minutes, then 10, then 20, capped at an hour. A model that answers
*every* request in a row resets to the floor, so a recovered provider is trusted
straight away while a provider that keeps failing is asked less and less.

A row is folded by its failure *share*, not by whether anything failed (#268):
a model that failed half or more of the requests it was asked is degraded and
its cooldown escalates, while a single failure among several answers is a blip
that leaves the chain order alone. Folding "answered anything" to healthy -- the
old rule -- is what let a chronically half-broken model sit at the head of the
chain: it answered *something* on every row, so every escalation was cleared,
and it was asked first again five minutes later, forever.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from typing import Any, Final

from app.hardening import env_int

LOGGER = logging.getLogger(__name__)

# Five minutes: long enough that a rate-limited or restarting provider is not
# hammered by the sweeps that follow (the sweep cadence is a minute), short
# enough that a blip costs the row nothing but a wait.
DEFAULT_COOLDOWN_SECONDS: Final = 300.0
# An hour: past this a model is effectively out of the chain until its health
# is looked at, which is where an operator wants to be told about it.
DEFAULT_COOLDOWN_MAX_SECONDS: Final = 3600.0
# Two failed attempts per model per row. The row (one child spawn) is the unit,
# not the block: a twelve-paragraph document must not retry an outage twelve
# times, and after two failures the fallback is the better use of the budget.
DEFAULT_ATTEMPTS_PER_MODEL: Final = 2
# The share of a row's requests to one model that may fail before the model is
# treated as degraded. Half: "this model fails as often as it answers" is a fact
# about the model, while one bad block out of three is a fact about that block.
# The gate is what separates a blip (leave the chain order alone) from a chronic
# half-failure (demote, and escalate while it keeps happening, #268).
DEFAULT_FAILURE_RATIO: Final = 0.5
# How many recent rows the diagnostic projection folds into its per-model
# window (#269). Ten sweeps is enough to see a trend and few enough that the
# cell stays small; it is a display window, not a rule, so it is a constant
# rather than a knob -- changing it changes what an operator reads, not what the
# chain does.
HEALTH_WINDOW_SAMPLES: Final = 10

COOLDOWN_ENV: Final = "PEARSS_MODEL_COOLDOWN_SECONDS"
COOLDOWN_MAX_ENV: Final = "PEARSS_MODEL_COOLDOWN_MAX_SECONDS"
ATTEMPTS_ENV: Final = "PEARSS_MODEL_ATTEMPTS_PER_MODEL"
RATIO_ENV: Final = "PEARSS_MODEL_FAILURE_RATIO"


def _env_seconds(name: str, default: float, *, allow_zero: bool = False) -> float:
    """Read a number of seconds, falling back on garbage.

    `allow_zero` is for the cooldown, where 0 is a meaningful setting: "do not
    cool down at all". A deployment that would rather keep retrying than wait
    out a cooldown can say so, and a test that is about something else (the row
    budget, say) can isolate itself from model health without pretending the
    provider is healthy.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        LOGGER.warning("%s is not a number (%r); using %s", name, raw, default)
        return default
    if value > 0:
        return value
    return 0.0 if allow_zero and value == 0 else default


def _env_ratio(name: str, default: float) -> float:
    """Read a failure-ratio threshold in (0, 1], falling back on garbage.

    Out of range is treated like unparseable rather than clamped: a ratio of 0
    would demote every model that failed once, and one above 1 would demote none
    of them -- silently turning the knob into a different feature.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        LOGGER.warning("%s is not a number (%r); using %s", name, raw, default)
        return default
    if not 0.0 < value <= 1.0:
        LOGGER.warning("%s is outside (0, 1] (%r); using %s", name, raw, default)
        return default
    return value


class ModelHealth:
    """Per-model failure bookkeeping, safe to call from the worker's threads.

    One entry point per direction: `policy()` says what a row may ask,
    `record()` folds a row's report back in. Both take the lock, because the
    worker runs its rows in a thread pool and a lost update here means a model
    that keeps being preferred after it broke.
    """

    def __init__(
        self,
        *,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
        cooldown_max_seconds: float = DEFAULT_COOLDOWN_MAX_SECONDS,
        attempts_per_model: int = DEFAULT_ATTEMPTS_PER_MODEL,
        failure_ratio: float = DEFAULT_FAILURE_RATIO,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cooldown_seconds = cooldown_seconds
        self._cooldown_max_seconds = max(cooldown_max_seconds, cooldown_seconds)
        self._attempts_per_model = max(1, attempts_per_model)
        self._failure_ratio = failure_ratio
        self._clock = clock
        self._lock = threading.Lock()
        # Consecutive failed episodes per model, and until when it is skipped.
        self._failures: dict[str, int] = {}
        self._cooling_until: dict[str, float] = {}
        # What the diagnostic view reports (ADR 0051): the span of the current
        # episode, why it started, and when.
        self._cooldown_span: dict[str, float] = {}
        self._last_failure: dict[str, tuple[str, float]] = {}
        # First-seen order, which is the order the chain asked in. Enough to
        # answer "which model would the next row ask?" without the worker
        # knowing the deployment's configured chain.
        self._order: list[str] = []
        # The recent rows each model was asked in, for the diagnostic window
        # (#269): a bounded fold of (asked, failed) samples per model.
        self._window: dict[str, deque[tuple[int, int]]] = {}

    @classmethod
    def from_env(cls, *, clock: Callable[[], float] = time.monotonic) -> ModelHealth:
        return cls(
            cooldown_seconds=_env_seconds(
                COOLDOWN_ENV, DEFAULT_COOLDOWN_SECONDS, allow_zero=True
            ),
            cooldown_max_seconds=_env_seconds(
                COOLDOWN_MAX_ENV, DEFAULT_COOLDOWN_MAX_SECONDS
            ),
            attempts_per_model=env_int(ATTEMPTS_ENV, DEFAULT_ATTEMPTS_PER_MODEL),
            failure_ratio=_env_ratio(RATIO_ENV, DEFAULT_FAILURE_RATIO),
            clock=clock,
        )

    def policy(self) -> dict[str, Any]:
        """What this row should do: which models to skip, and the retry budget.

        Only what is *cooling* is excluded. A model whose cooldown has expired
        is back in its configured position without anyone deciding to trust it
        again -- the recovery is the passage of time, not an operator action.
        """
        with self._lock:
            now = self._clock()
            skip = sorted(
                model for model, until in self._cooling_until.items() if until > now
            )
            return {"skip": skip, "attempts_per_model": self._attempts_per_model}

    def record(self, events: Iterable[tuple[str, int, int, str | None]]) -> None:
        """Fold one row's report into the health.

        An event is `(model, asked, failed, last_error_class)`: how many requests
        the row sent to that model, how many of them failed, and the class of the
        last one. A model the row never asked has no event.

        Three outcomes (ADR 0056):

        - nothing failed -- the model is healthy for this row, and the escalation
          resets, so the next failure starts at the floor rather than where the
          last outage left off;
        - a share below the ratio gate failed -- a blip; the chain order is left
          alone, and the escalation resets;
        - the rest -- a degraded row; the cooldown escalates.
        """
        with self._lock:
            for model, asked, failed, error_class in events:
                if asked <= 0:
                    # A model the row never asked is not a zero-failure sample.
                    continue
                if model not in self._order:
                    self._order.append(model)
                self._window.setdefault(
                    model, deque(maxlen=HEALTH_WINDOW_SAMPLES)
                ).append((asked, failed))
                if failed > 0 and error_class is not None:
                    self._last_failure[model] = (error_class, self._clock())
                if failed == 0 or failed / asked < self._failure_ratio:
                    self._failures.pop(model, None)
                    self._cooling_until.pop(model, None)
                    self._cooldown_span.pop(model, None)
                    continue
                failures = self._failures.get(model, 0) + 1
                self._failures[model] = failures
                cooldown = min(
                    self._cooldown_seconds * (2 ** (failures - 1)),
                    self._cooldown_max_seconds,
                )
                self._cooling_until[model] = self._clock() + cooldown
                self._cooldown_span[model] = cooldown
                LOGGER.warning(
                    "model %s failed %d/%d asks (%s); skipping it for %.0fs",
                    model,
                    failed,
                    asked,
                    error_class or "?",
                    cooldown,
                )

    def snapshot(self) -> dict[str, Any]:
        """A read-only projection for the diagnostic view (ADR 0051, ADR 0057).

        Two model dimensions, answering two questions:

        - `models` -- what is cooling *right now*, and why (ADR 0051). A model
          that answered is healthy and its episode is over, and reporting a
          cleared episode would send an operator after a failure that no longer
          exists.
        - `window` -- the last `HEALTH_WINDOW_SAMPLES` rows each model was asked
          in, folded to attempts / failures / fallbacks / rate (#269). This is
          the half-broken model the first cell cannot show: one that fails part
          of every row is *below* the demotion gate, so it never cools and never
          appears in `models`, while the journal fills with fallbacks. Only
          models that failed at least once in the window appear -- a model that
          never failed has nothing to report.
        """
        with self._lock:
            now = self._clock()
            cooling: dict[str, Any] = {}
            for model, until in self._cooling_until.items():
                if until <= now:
                    continue
                failed_class, failed_at = self._last_failure.get(model, ("", now))
                cooling[model] = {
                    "cooling_seconds": round(max(0.0, until - now), 1),
                    "cooldown_seconds": self._cooldown_span.get(model, 0.0),
                    "failures": self._failures.get(model, 0),
                    "last_failure_class": failed_class,
                    "since_last_failure_seconds": round(max(0.0, now - failed_at), 1),
                }
            window: dict[str, Any] = {}
            for model, samples in self._window.items():
                attempts = sum(asked for asked, _ in samples)
                failures = sum(failed for _, failed in samples)
                if failures == 0:
                    continue
                window_class: str | None = None
                window_at: float | None = None
                remembered = self._last_failure.get(model)
                if remembered is not None:
                    window_class, window_at = remembered
                window[model] = {
                    "attempts": attempts,
                    "failures": failures,
                    # One per row the model failed in: how often the chain had to
                    # move past it, which is what the journal counts as
                    # "falling back to".
                    "fallbacks": sum(1 for _, failed in samples if failed),
                    "failure_rate": round(failures / attempts, 3) if attempts else 0.0,
                    "last_failure_class": window_class,
                    "since_last_failure_seconds": (
                        None
                        if window_at is None
                        else round(max(0.0, now - window_at), 1)
                    ),
                }
            return {
                "preferred": next(
                    (model for model in self._order if model not in cooling), None
                ),
                "skipped": sorted(cooling),
                "attempts_per_model": self._attempts_per_model,
                "models": cooling,
                "window": window,
            }


_HEALTH: ModelHealth | None = None
_HEALTH_LOCK = threading.Lock()


def health() -> ModelHealth:
    """The process-wide registry, built from the environment on first use."""
    global _HEALTH
    with _HEALTH_LOCK:
        if _HEALTH is None:
            _HEALTH = ModelHealth.from_env()
        return _HEALTH


def reset_health() -> None:
    """Forget the process-wide registry (tests; the environment never changes)."""
    global _HEALTH
    with _HEALTH_LOCK:
        _HEALTH = None


__all__ = [
    "ATTEMPTS_ENV",
    "COOLDOWN_ENV",
    "COOLDOWN_MAX_ENV",
    "DEFAULT_ATTEMPTS_PER_MODEL",
    "DEFAULT_COOLDOWN_MAX_SECONDS",
    "DEFAULT_COOLDOWN_SECONDS",
    "DEFAULT_FAILURE_RATIO",
    "HEALTH_WINDOW_SAMPLES",
    "RATIO_ENV",
    "ModelHealth",
    "health",
    "reset_health",
]
