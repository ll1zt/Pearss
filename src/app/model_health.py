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
again resets to the floor, so a recovered provider is trusted straight away
while a provider that keeps failing is asked less and less.
"""

from __future__ import annotations

import logging
import os
import threading
import time
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

COOLDOWN_ENV: Final = "PEARSS_MODEL_COOLDOWN_SECONDS"
COOLDOWN_MAX_ENV: Final = "PEARSS_MODEL_COOLDOWN_MAX_SECONDS"
ATTEMPTS_ENV: Final = "PEARSS_MODEL_ATTEMPTS_PER_MODEL"


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
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cooldown_seconds = cooldown_seconds
        self._cooldown_max_seconds = max(cooldown_max_seconds, cooldown_seconds)
        self._attempts_per_model = max(1, attempts_per_model)
        self._clock = clock
        self._lock = threading.Lock()
        # Consecutive failed episodes per model, and until when it is skipped.
        self._failures: dict[str, int] = {}
        self._cooling_until: dict[str, float] = {}

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

    def record(self, events: Iterable[tuple[str, str | None]]) -> None:
        """Fold one row's report into the health.

        An event is `(model, None)` when that model answered something in the
        row, or `(model, error_class)` when it did not. Answering is what makes
        a model healthy: its escalation resets, so the next failure starts at
        the floor rather than where the last outage left off. A model that
        failed twice in one row is one episode, because the report has one entry
        per model.
        """
        with self._lock:
            for model, error_class in events:
                if error_class is None:
                    self._failures.pop(model, None)
                    self._cooling_until.pop(model, None)
                    continue
                failures = self._failures.get(model, 0) + 1
                self._failures[model] = failures
                cooldown = min(
                    self._cooldown_seconds * (2 ** (failures - 1)),
                    self._cooldown_max_seconds,
                )
                self._cooling_until[model] = self._clock() + cooldown
                LOGGER.warning(
                    "model %s failed (%s); skipping it for %.0fs",
                    model,
                    error_class,
                    cooldown,
                )


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
    "ModelHealth",
    "health",
    "reset_health",
]
