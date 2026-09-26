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
import time

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


def test_waiting_for_a_sibling_gives_up_when_the_budget_runs_out() -> None:
    """A holder that never releases must not block a row forever.

    `_acquire` waits in bounded slices and re-checks the deadline, so an
    abandoned slot (a fetcher that died without releasing, or one still inside
    a very slow processor) costs this row its turn rather than a worker thread
    forever. Returning False hands the row back to the queue, which is the same
    outcome as any other declined admission.

    Driven directly rather than with threads: the state under test is "the
    deadline is already spent while a slot is held", which is what a real
    timeout produces but is not deterministic to arrange through the fetch path.
    """
    pacer, clock, _slept = _pacer(min_interval=0.0, max_per_sweep=5)
    assert pacer.pace("blog.example") is True, "the slot is now held"

    # A second row's budget is already exhausted: it must decline rather than
    # wait on a holder that never releases.
    assert pacer._acquire("blog.example", clock() - 1.0) is False, (
        "a spent budget must decline instead of blocking on the holder"
    )
    pacer.release("blog.example")


def test_the_interval_wait_gives_up_when_the_budget_runs_out() -> None:
    """The same bound applies to waiting out `min_interval`.

    Reachable when a host's spacing plus a slow sibling exceeds the wait
    budget: the row goes back to the queue instead of occupying a worker.
    """
    pacer, _clock, _slept = _pacer(min_interval=60.0, max_per_sweep=5)
    assert pacer.pace("blog.example") is True
    pacer.release("blog.example")

    # A second row with no budget left cannot wait the 60s interval out.
    assert pacer._await_interval("blog.example", -1.0) is False, (
        "a spent budget must decline rather than sleep through the interval"
    )


def test_a_slot_left_held_blocks_the_next_row_until_the_budget_expires() -> None:
    """What a missing release costs, stated as behaviour.

    This is the *failure mode* the release contract prevents, pinned so the
    contract has a visible consequence rather than only a comment: a slot held
    by a row that never releases makes the next row for that host wait out its
    whole budget and then decline, rather than fetching.

    Uses the real clock deliberately. A manually-advanced clock cannot serve
    two threads -- the sibling never observes the advance, so it never returns
    -- and the property under test is precisely "the wait terminates", which a
    fake clock would assert by construction. The budget here is small (a 10ms
    interval gives a 5s minimum budget), so the test stays fast.

    The end-to-end suite cannot assert this: by the time a test looks, some
    other row has released the slot through its own normal path. An earlier
    attempt to cover it there survived its injection check, which is why the
    consequence is stated here instead.
    """
    pacer = HostPacer(min_interval=0.01, max_per_sweep=5, cooldown_seconds=300.0)

    assert pacer.pace("blog.example") is True, "the slot is taken and not released"

    outcome: list[bool] = []
    started = time.monotonic()

    def sibling() -> None:
        outcome.append(pacer.pace("blog.example"))

    worker = threading.Thread(target=sibling, daemon=True)
    worker.start()
    worker.join(timeout=20)

    assert outcome == [False], (
        "a host whose slot is never released must decline later rows after "
        "their budget expires rather than serve them"
    )
    assert time.monotonic() - started >= 1.0, (
        "the sibling should have waited out its budget, not declined at once"
    )


def test_a_woken_sibling_sees_a_cooldown_recorded_before_the_release() -> None:
    """A sibling waiting at the gate must be refused by a cooldown already set.

    This is the ordering the worker is expected to respect: record the refusal,
    then release the slot. What it pins is the pacer's side of that contract --
    that a cooldown installed before a release is visible to the row the release
    wakes -- which is the half the pacer is responsible for.

    Stated honestly: it does **not** prove the worker calls them in that order.
    Reversing the worker's two statements still passes this test and the whole
    suite, because the woken sibling has to re-acquire the pacer lock while the
    recorder already holds it, so the sibling loses by a few hundred
    microseconds (measured in a worker trace). The ordering is therefore defence
    in depth; what actually fixed the load-dependent failures was making the
    politeness tests order-independent, which is done in the integration suite.
    """
    pacer, _clock, _slept = _pacer(min_interval=60.0, max_per_sweep=5)

    # A sibling is waiting at the gate: the slot is taken by the fetch that is
    # about to be refused.
    assert pacer.pace("blog.example") is True
    outcome: list[bool] = []

    def sibling() -> None:
        outcome.append(pacer.pace("blog.example"))

    worker = threading.Thread(target=sibling, daemon=True)
    worker.start()

    # The correct order: install the cooldown, *then* release.
    pacer.record_blocked("blog.example")
    pacer.release("blog.example")
    worker.join(timeout=10)

    assert outcome == [False], (
        "the waiting sibling must be refused by the cooldown that was installed "
        "before the release; if it is admitted, the worker released the slot "
        "before recording the refusal"
    )


def test_a_sibling_is_admitted_when_no_cooldown_was_recorded() -> None:
    """The other half: a successful fetch releases and the sibling proceeds.

    Without this, the test above could be satisfied by refusing everything.
    """
    pacer, _clock, _slept = _pacer(min_interval=0.0, max_per_sweep=5)

    assert pacer.pace("blog.example") is True
    outcome: list[bool] = []

    def sibling() -> None:
        outcome.append(pacer.pace("blog.example"))
        pacer.release("blog.example")

    worker = threading.Thread(target=sibling, daemon=True)
    worker.start()
    pacer.release("blog.example")
    worker.join(timeout=10)

    assert outcome == [True], "a healthy fetch must let the sibling through"
