"""The per-host pacer's admission rules (ADR 0029, ADR 0033).

`HostPacer` decides two things that are cheap to get subtly wrong and
expensive to notice: how many requests one host sees per sweep, and whether a
refusal actually stops the next one. Both are pure logic once the clock and the
sleep are injected, so they are pinned here; the end-to-end behaviour (a real
origin, a real child process, a real SQLite queue) is
`tests/integration/test_fetch_politeness.py`.

Issue #57 is the reason the in-flight gate exists: admission used to wait only
for the interval, so a *slow* refusal left a window in which a sibling row
dispatched before the cooldown was recorded.
"""

from __future__ import annotations

import threading

import pytest

from app.fetch_pacing import HostPacer

pytestmark = pytest.mark.unit


class _Clock:
    """A manually advanced monotonic clock."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _pacer(**kwargs: object) -> tuple[HostPacer, _Clock, list[float]]:
    clock = _Clock()
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock.advance(seconds)

    pacer = HostPacer(clock=clock, sleep=sleep, **kwargs)  # type: ignore[arg-type]
    return pacer, clock, slept


# --------------------------------------------------------------------------
# The bounds from ADR 0029
# --------------------------------------------------------------------------


def test_the_per_sweep_cap_admits_exactly_that_many() -> None:
    pacer, _clock, _slept = _pacer(min_interval=0.0, max_per_sweep=2)

    assert pacer.reserve("blog.example") is True
    assert pacer.reserve("blog.example") is True
    assert pacer.reserve("blog.example") is False, "the third is over budget"

    # A new sweep refills the budget; cooldowns would persist.
    pacer.begin_sweep()
    assert pacer.reserve("blog.example") is True


def test_hosts_are_budgeted_independently() -> None:
    pacer, _clock, _slept = _pacer(min_interval=0.0, max_per_sweep=1)

    assert pacer.reserve("a.example") is True
    assert pacer.reserve("a.example") is False
    assert pacer.reserve("b.example") is True, "one host's budget is not another's"


def test_a_refused_host_is_declined_for_the_cooldown() -> None:
    pacer, clock, _slept = _pacer(
        min_interval=0.0, max_per_sweep=5, cooldown_seconds=300
    )

    pacer.record_blocked("blog.example")
    assert pacer.reserve("blog.example") is False

    clock.advance(299)
    assert pacer.reserve("blog.example") is False, "still inside the cooldown"

    clock.advance(2)
    assert pacer.reserve("blog.example") is True, "the cooldown has expired"


def test_the_interval_is_waited_out_before_dispatching() -> None:
    pacer, _clock, slept = _pacer(min_interval=5.0, max_per_sweep=5)

    assert pacer.pace("blog.example") is True
    pacer.release("blog.example")
    assert pacer.pace("blog.example") is True, "the interval is waited, not refused"

    assert slept, "the pacer must actually wait"
    assert sum(slept) >= 5.0, f"waited {sum(slept)}s for a 5s interval"


def test_an_empty_host_is_never_paced() -> None:
    """Rows that are not host-paced (a local file, an unparsable URL) pass."""
    pacer, _clock, _slept = _pacer(min_interval=1.0, max_per_sweep=1)

    assert pacer.pace("") is True
    assert pacer.reserve("") is True
    pacer.release("")


# --------------------------------------------------------------------------
# The in-flight gate (issue #57)
# --------------------------------------------------------------------------


def test_a_sibling_waits_for_an_in_flight_fetch() -> None:
    """The second row must not dispatch while the first is still out.

    This is issue #57 in isolation: the cooldown can only be installed once the
    first fetch returns, so a second row that dispatches before that has already
    made the request the cooldown was supposed to prevent.
    """
    pacer, _clock, _slept = _pacer(min_interval=0.0, max_per_sweep=5)

    assert pacer.pace("blog.example") is True, "the first row takes the slot"

    seen: list[bool] = []

    def sibling() -> None:
        seen.append(pacer.pace("blog.example"))
        pacer.release("blog.example")

    worker = threading.Thread(target=sibling, daemon=True)
    worker.start()
    # The sibling must still be waiting: nothing has released the slot.
    worker.join(timeout=0.5)
    assert worker.is_alive(), (
        "a sibling dispatch while a fetch is in flight is the race that lets a "
        "refused host be hit twice"
    )

    # The first fetch returns and was refused.
    pacer.record_blocked("blog.example")
    pacer.release("blog.example")
    worker.join(timeout=2.0)

    assert seen == [False], (
        "the sibling must see the cooldown the refused fetch installed, not "
        "dispatch into the window before it was recorded"
    )


def test_a_sibling_proceeds_when_the_in_flight_fetch_succeeded() -> None:
    """Waiting must not mean refusing: a healthy host keeps its throughput.

    The gate serialises same-host dispatches; it must not turn them into one
    per sweep. This is the half that a naive "one row per host per sweep" fix
    would break, so it is asserted explicitly.
    """
    pacer, _clock, _slept = _pacer(min_interval=0.0, max_per_sweep=5)

    assert pacer.pace("blog.example") is True
    outcome: list[bool] = []

    def sibling() -> None:
        outcome.append(pacer.pace("blog.example"))
        pacer.release("blog.example")

    worker = threading.Thread(target=sibling, daemon=True)
    worker.start()
    # A successful fetch releases without recording any refusal.
    pacer.release("blog.example")
    worker.join(timeout=2.0)

    assert outcome == [True], "a healthy host must keep admitting rows"


def test_release_frees_the_slot_for_the_next_row() -> None:
    """`release` is what makes the gate a handoff rather than a lock."""
    pacer, _clock, _slept = _pacer(min_interval=0.0, max_per_sweep=5)

    assert pacer.pace("blog.example") is True
    pacer.release("blog.example")
    assert pacer.pace("blog.example") is True, "the slot was handed on"
    pacer.release("blog.example")


def test_a_declined_admission_does_not_hold_the_slot() -> None:
    """A row that is refused must not leave the host waiting on it.

    Declining can happen *after* the slot is taken -- the cooldown is noticed
    while waiting out the interval -- and holding it then would stall the host
    until the wait budget expired, turning a refusal into a slowdown.
    """
    pacer, clock, _slept = _pacer(
        min_interval=0.0, max_per_sweep=5, cooldown_seconds=300
    )

    assert pacer.pace("blog.example") is True, "the first row takes the slot"
    # A sibling is refused while the host is cooling down, and must hand the
    # slot back rather than keep it for a fetch it never made.
    pacer.record_blocked("blog.example")
    pacer.release("blog.example")
    assert pacer.pace("blog.example") is False, "the cooldown declines it"

    clock.advance(4000)
    assert pacer.pace("blog.example") is True, (
        "after the cooldown expires the slot must still be free: the declined "
        "row handed it on rather than holding it"
    )
    pacer.release("blog.example")


def test_a_crashed_fetch_does_not_stall_the_host() -> None:
    """Releasing happens in a `finally`, so an exception frees the slot.

    Pinned here as the contract the worker relies on: a processor that raises
    must not leave the host marked busy, or one transient error would stop that
    host for the rest of the process's life.
    """
    pacer, _clock, _slept = _pacer(min_interval=0.0, max_per_sweep=5)

    assert pacer.pace("blog.example") is True
    try:
        raise RuntimeError("the fetch blew up")
    except RuntimeError:
        pacer.release("blog.example")

    assert pacer.pace("blog.example") is True, "the slot survived the failure"
    pacer.release("blog.example")
