"""Host-level pacing for processor fetches (issue #35).

A processor that fetches the *item's own URL* talks to a third-party server the
operator does not control. Nothing in the chain model bounds that: subscribing a
blog with 122 stored posts queued 122 fetches, and the worker fired them as fast
as its concurrency slots allowed — three in flight at once, no spacing, no cap.
That is a burst at someone's blog, which is what this module prevents.

Why the pacing lives here and not in the processor: processors run in a
sandboxed child process spawned per row (`app/processor_runtime`), so a counter
inside one cannot pace anything — every execution starts from zero. The host
worker is the only process that sees the whole queue, so the state belongs to
it. The state is in memory on purpose: a restart forgetting the pacing
bookkeeping is a far smaller problem than an un-paced burst, and it costs no
schema.

Three bounds, all per host:

- **interval** — minimum wall-clock gap between two dispatches to the same
  host. Enforced by waiting, not by dropping: the work still happens, just
  spaced out.
- **per-sweep cap** — at most N dispatches to one host in one sweep. Rows
  beyond the cap stay queued for the next sweep, so a 122-post backlog drains
  at N per sweep instead of all at once.
- **cooldown** — after a host answers with a `blocked` classification (403/429:
  "stop, you are asking too much"), that host is skipped entirely for the
  cooldown window. A rate-limited origin is a request to slow down; trying the
  next item on it is the opposite of listening.

robots.txt is deliberately **not** consulted (decision recorded in ADR 0029):
the sites this feature serves publish permissive robots files, fetching robots
per host adds an uncached third-party request that the pacing itself would have
to police, and the harm actually observed was request rate, not path permission.
An operator who needs path-level permission can filter at the source level.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from typing import Final
from urllib.parse import urlsplit

LOGGER = logging.getLogger(__name__)

# Conservative defaults, deliberately slow: a blog is not an API. Two fetches
# per sweep with 2s between them drains a 100-post backlog over ~50 minutes,
# which is invisible to the reader next to a 1h feed interval.
DEFAULT_MIN_INTERVAL_SECONDS: Final = 2.0
DEFAULT_MAX_PER_HOST_PER_SWEEP: Final = 2
DEFAULT_COOLDOWN_SECONDS: Final = 900.0
DEFAULT_BACKFILL_LIMIT: Final = 10

MIN_INTERVAL_ENV: Final = "PEARSS_FETCH_MIN_INTERVAL"
MAX_PER_SWEEP_ENV: Final = "PEARSS_FETCH_MAX_PER_HOST_PER_SWEEP"
COOLDOWN_ENV: Final = "PEARSS_FETCH_COOLDOWN_SECONDS"
BACKFILL_LIMIT_ENV: Final = "PEARSS_BACKFILL_DEFAULT_LIMIT"

# Processors whose work is a fetch of the item's own URL on a third-party host.
# A translation calls a provider the operator chose and pays for; pacing it by
# the *item's* host would throttle the provider for no reason, which is why
# this is a set of ids rather than "every processor".
PACED_PROCESSORS: Final[frozenset[str]] = frozenset({"fulltext"})


def host_of(url: str | None) -> str:
    """The pacing key for a URL: its host, lowercased; "" when unparseable."""
    if not url:
        return ""
    try:
        return (urlsplit(url).netloc or "").lower()
    except ValueError:
        return ""


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        LOGGER.warning("%s is not a number (%r); using %s", name, raw, default)
        return default
    return value if value >= 0 else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        LOGGER.warning("%s is not an integer (%r); using %s", name, raw, default)
        return default
    return value if value >= 1 else default


def backfill_default_limit() -> int:
    """How many items a backfill queues when the caller names no limit.

    Unbounded by default was the accident in #35: one call queued 122 fetches
    of a third-party site. The default is a batch the operator can watch,
    repeated until the backlog is done.
    """
    return _env_int(BACKFILL_LIMIT_ENV, DEFAULT_BACKFILL_LIMIT)


class HostPacer:
    """Per-host dispatch pacing, safe to call from the worker's threads.

    One entry point, `take_turn`, because the three bounds interact: it waits
    out the interval, then re-checks the cooldown and the per-sweep cap before
    admitting. Waiting anywhere else is wrong — claiming in the sweep loop and
    sleeping there would let the (already admitted) rows dispatch together in
    the thread pool, which is the burst this exists to prevent.
    """

    def __init__(
        self,
        *,
        min_interval: float = DEFAULT_MIN_INTERVAL_SECONDS,
        max_per_sweep: int = DEFAULT_MAX_PER_HOST_PER_SWEEP,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._min_interval = min_interval
        self._max_per_sweep = max_per_sweep
        self._cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._sleep = sleep
        # A sweep must not block indefinitely on one host: past this budget
        # the row stays queued for the next sweep instead.
        self._max_wait = max(min_interval * 4.0, 5.0)
        self._lock = threading.Lock()
        self._last_dispatch: dict[str, float] = {}
        self._dispatched_this_sweep: dict[str, int] = {}
        self._cooldown_until: dict[str, float] = {}
        # Hosts with a fetch in flight right now (issue #57). A refusal can
        # only be recorded once the fetch that met it has returned, so without
        # this a sibling row -- already admitted by the per-sweep cap -- can
        # dispatch inside that window and hammer the host the cooldown exists
        # to protect.
        self._in_flight: dict[str, threading.Event] = {}

    @classmethod
    def from_env(cls) -> HostPacer:
        return cls(
            min_interval=_env_float(MIN_INTERVAL_ENV, DEFAULT_MIN_INTERVAL_SECONDS),
            max_per_sweep=_env_int(MAX_PER_SWEEP_ENV, DEFAULT_MAX_PER_HOST_PER_SWEEP),
            cooldown_seconds=_env_float(COOLDOWN_ENV, DEFAULT_COOLDOWN_SECONDS),
        )

    @property
    def min_interval(self) -> float:
        """The configured spacing between two fetches to one host."""
        return self._min_interval

    @property
    def max_per_sweep(self) -> int:
        """The configured per-host budget for one sweep."""
        return self._max_per_sweep

    def begin_sweep(self) -> None:
        """Start a new sweep: the per-sweep caps refill, cooldowns persist."""
        with self._lock:
            self._dispatched_this_sweep = {}

    def reserve(self, host: str) -> bool:
        """Claim this sweep's slot for `host` — cap and cooldown, no waiting.

        Called at claim time, which is single-threaded: a host that is
        cooling down down after a refusal, or whose sweep budget is spent,
        simply is not claimed and waits for the next sweep.
        """
        if not host:
            return True
        with self._lock:
            if self._cooldown_until.get(host, 0.0) > self._clock():
                return False
            if self._dispatched_this_sweep.get(host, 0) >= self._max_per_sweep:
                return False
            self._dispatched_this_sweep[host] = (
                self._dispatched_this_sweep.get(host, 0) + 1
            )
            return True

    def pace(self, host: str) -> bool:
        """Wait out the interval before fetching `host`; False = don't fetch.

        Admission is two waits in a fixed order, and the order is the point:

        1. **Wait for a sibling fetch on this host to finish**, then take the
           slot. The one fact that decides whether this fetch should happen at
           all -- was the previous one refused? -- is not known until that
           fetch returns, so waiting only for the interval let a second row
           dispatch into the window between the refusal arriving and the
           cooldown being recorded. How wide that window was depended on how
           slow the origin was to answer (issue #57: a 3s refusal produced two
           requests instead of one).
        2. **Wait out `min_interval`** while holding the slot, re-checking the
           cooldown each time round.

        Holding the slot across the *fetch* is the caller's job (`release` in a
        `finally`), not this method's: this returns as soon as admission is
        granted, and the fetch starts immediately afterwards. The caller must
        also record any refusal *before* releasing -- see the worker -- so the
        sibling this gate wakes up sees the cooldown rather than racing it.

        False means the host went on cooldown while waiting, or the budget was
        spent -- the caller leaves its row queued for the next sweep rather
        than burning an attempt.
        """
        if not host:
            return True
        deadline = self._clock() + self._max_wait
        if not self._acquire(host, deadline):
            return False
        if self._await_interval(host, deadline):
            return True
        # Declined after acquiring: hand the slot on, so the next row for this
        # host does not wait for a turn that was never taken.
        self.release(host)
        return False

    def _acquire(self, host: str, deadline: float) -> bool:
        """Take this host's fetch slot, waiting for a sibling to finish first."""
        while True:
            with self._lock:
                current = self._in_flight.get(host)
                if current is None or current.is_set():
                    # Free, or the previous holder has finished (and may have
                    # installed a cooldown this row will see on the next step).
                    self._in_flight[host] = threading.Event()
                    return True
                wait_on = current
            remaining = deadline - self._clock()
            if remaining <= 0:
                return False
            # Bounded, so the loop re-checks the deadline rather than blocking
            # indefinitely on a holder that somehow never releases.
            wait_on.wait(min(0.25, remaining))

    def _await_interval(self, host: str, deadline: float) -> bool:
        """Wait out the configured spacing, re-checking the cooldown each pass."""
        while True:
            with self._lock:
                now = self._clock()
                if self._cooldown_until.get(host, 0.0) > now:
                    return False
                last = self._last_dispatch.get(host)
                if last is None or (now - last) >= self._min_interval:
                    self._last_dispatch[host] = now
                    return True
                wait = self._min_interval - (now - last)
            remaining = deadline - self._clock()
            if remaining <= 0:
                return False
            self._sleep(max(0.0, min(wait, remaining)))

    def release(self, host: str) -> None:
        """The fetch for `host` has returned (success or failure).

        Called by the fetcher in a `finally`, so a crash inside the processor
        cannot leave a host permanently marked busy -- that would turn a
        transient error into a stalled queue.
        """
        if not host:
            return
        with self._lock:
            holder = self._in_flight.pop(host, None)
        if holder is not None:
            holder.set()

    def record_blocked(self, host: str) -> None:
        """A host refused us (403/429): stop asking for the cooldown window."""
        if not host:
            return
        with self._lock:
            self._cooldown_until[host] = self._clock() + self._cooldown_seconds
        LOGGER.warning(
            "host %s returned blocked; pausing fetches to it for %.0fs",
            host,
            self._cooldown_seconds,
        )


_PACER: HostPacer | None = None
_PACER_LOCK = threading.Lock()


def pacer() -> HostPacer:
    """The process-wide pacer, built from the environment on first use."""
    global _PACER
    with _PACER_LOCK:
        if _PACER is None:
            _PACER = HostPacer.from_env()
        return _PACER


def reset_pacer() -> None:
    """Forget the process-wide pacer (tests; the environment never changes)."""
    global _PACER
    with _PACER_LOCK:
        _PACER = None


__all__ = [
    "PACED_PROCESSORS",
    "HostPacer",
    "backfill_default_limit",
    "host_of",
    "pacer",
    "reset_pacer",
]
