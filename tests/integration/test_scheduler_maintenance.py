"""The scheduler's maintenance passes: reaping orphans and pruning audit rows.

These are the two things the tick loop does that are not collection, and both
exist to fix a specific failure that had already happened:

  - `_reap_orphaned_runs_if_due` closes runs a *dead* process left `running`.
    Startup-only reaping (the old behaviour) meant a run abandoned inside this
    process held its source's only active-run slot until the next restart --
    the source silently stopped collecting. Eight such rows had accumulated in
    practice before the per-tick pass existed.
  - `_prune_audit_if_due` enforces the audit retention window. ADR 0016 added it
    because public exposure turns every unauthorized probe into a row.

Neither had a test. The repository functions underneath them did, which is the
gap that makes this file necessary: `reap_orphaned_runs` and `prune_audit_log`
can be correct while the loop never calls them, and deleting either call from
`_loop` would have broken nothing that was watching.

Both are gated on elapsed monotonic time, so the clock is injected rather than
waited on. Sleeping an hour to observe a scheduling decision is not a test --
it is a slow, flaky way of asserting nothing. `Scheduler(monotonic=...)` is the
seam; production passes the default.

The assertions are on what a client of this system would see: whether a run is
closed, whether an audit row survived. Neither reaches into `_next_reap_at` or
any other internal field.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from adapters.db_models import AuditLog
from adapters.repository import (
    claim_job_run,
    create_job_run,
    ensure_collect_job,
)
from core.enums import JobRunStatus

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def idle_client() -> Iterator[httpx.Client]:
    """A client nothing in this file fetches through.

    The Scheduler requires a client because collection needs one; these tests
    exercise the maintenance passes, which never fetch. A transport that raises
    makes that explicit -- if a future change made reaping fetch something, it
    would fail here rather than quietly succeeding against a stub response.
    """

    def _refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"maintenance must not fetch: {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(_refuse))
    try:
        yield client
    finally:
        client.close()


def _claimed_ago(**delta: float) -> str:
    """A fixed-width timestamp `delta` before now.

    Fixed width because that is the stored format (`adapters.timestamps`); a
    default `strftime` would drop the microseconds and compare wrongly against
    the grace cutoff.
    """
    moment = datetime.now(UTC).replace(microsecond=0) - timedelta(**delta)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


class _Clock:
    """A monotonic clock the test advances by hand.

    Only `time.monotonic` is replaced; the scheduler's other time reads
    (`datetime.now`) are used for the grace-window comparison, and those are
    kept deterministic by choosing run timestamps relative to a fixed instant
    rather than by freezing the wall clock.
    """

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _scheduler(
    session_factory: sessionmaker[Session], client: httpx.Client, clock: _Clock
) -> Any:
    from app.scheduler import Scheduler

    # tick_seconds large enough that the loop's own sleep never fires during a
    # test; the maintenance passes are driven by calling them directly.
    return Scheduler(session_factory, client=client, tick_seconds=3600, monotonic=clock)


def _seed_running_run(session: Session, *, run_id: str, claimed_at: str) -> None:
    """A source with one run left in `running` since `claimed_at`."""
    session.execute(
        sa.text(
            "insert into source (id, type, url, canonical_url, schedule_policy,"
            " status, trust_level, tenant_id, created_at, updated_at)"
            " values ('s1', 'rss', 'https://s1.example/feed',"
            ' \'https://s1.example/feed\', \'{"type":"fixed","interval":"30m"}\','
            " 'active', 'standard', 'default', :now, :now)"
        ),
        {"now": "2026-06-01T00:00:00.000000Z"},
    )
    job_id = ensure_collect_job(
        session,
        source_id="s1",
        tenant_id="default",
        at="2026-06-01T00:00:00.000000Z",
    )
    create_job_run(
        session, run_id=run_id, job_id=job_id, correlation_id="c1", at=claimed_at
    )
    claim_job_run(session, run_id=run_id, at=claimed_at)
    session.commit()


def _run_status(session_factory: sessionmaker[Session], run_id: str) -> str:
    with session_factory() as session:
        return str(
            session.execute(
                sa.text("select status from job_run where id = :id"), {"id": run_id}
            ).scalar_one()
        )


# --------------------------------------------------------------------------
# Scenario: a tick closes a run its process abandoned
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_a_tick_reaps_a_run_abandoned_past_the_grace_window(
    session_factory: sessionmaker[Session], idle_client: httpx.Client
) -> None:
    """The per-tick pass, not just the startup one.

    A run claimed well before the grace window is genuinely orphaned: no live
    process can still be working on it, so closing it is what frees the
    source's active-run slot. The run is claimed one day ago against a
    one-hour grace, which is unambiguous rather than boundary-shaped.
    """
    claimed = _claimed_ago(days=1)

    with session_factory() as session:
        _seed_running_run(session, run_id="abandoned", claimed_at=claimed)

    clock = _Clock()
    scheduler = _scheduler(session_factory, idle_client, clock)
    # `_next_reap_at` starts at 0, so the first call is always due.
    asyncio.run(scheduler._reap_orphaned_runs_if_due())

    assert _run_status(session_factory, "abandoned") == JobRunStatus.TIMED_OUT.value


@pytest.mark.integration
def test_a_tick_does_not_reap_a_run_inside_the_grace_window(
    session_factory: sessionmaker[Session], idle_client: httpx.Client
) -> None:
    """The failure this window exists to prevent.

    A run that is genuinely in flight must never be timed out: doing so loses
    its result *and* frees the active-run slot, so the source would start a
    second collection of the same thing. One minute against a one-hour grace
    is a live run by construction.
    """
    claimed = _claimed_ago(minutes=1)

    with session_factory() as session:
        _seed_running_run(session, run_id="live", claimed_at=claimed)

    clock = _Clock()
    scheduler = _scheduler(session_factory, idle_client, clock)
    asyncio.run(scheduler._reap_orphaned_runs_if_due())

    assert _run_status(session_factory, "live") == JobRunStatus.RUNNING.value


# --------------------------------------------------------------------------
# Scenario: maintenance is throttled, not run on every pass
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_the_reap_pass_is_throttled_between_calls(
    session_factory: sessionmaker[Session], idle_client: httpx.Client
) -> None:
    """Reaping scans the run table, so it runs periodically rather than always.

    Asserted through the clock: an orphan seeded *after* the first pass is
    untouched by an immediate second call, and closed once the clock has moved
    past the interval. That is the throttle's observable effect, and it is the
    reason a burst of ticks does not turn into a burst of table scans.
    """
    claimed = _claimed_ago(days=1)

    clock = _Clock()
    scheduler = _scheduler(session_factory, idle_client, clock)

    # First pass: due immediately (`_next_reap_at` starts at 0), nothing to do.
    asyncio.run(scheduler._reap_orphaned_runs_if_due())

    with session_factory() as session:
        _seed_running_run(session, run_id="after-first-pass", claimed_at=claimed)

    # Second pass, same instant: throttled, so the orphan is still running.
    asyncio.run(scheduler._reap_orphaned_runs_if_due())
    assert _run_status(session_factory, "after-first-pass") == (
        JobRunStatus.RUNNING.value
    )

    # Once the interval has elapsed the pass runs again and closes it. Four
    # ticks is the documented interval, so half an hour of tick time is past it.
    clock.advance(3600 * 5)
    asyncio.run(scheduler._reap_orphaned_runs_if_due())
    assert _run_status(session_factory, "after-first-pass") == (
        JobRunStatus.TIMED_OUT.value
    )


# --------------------------------------------------------------------------
# Scenario: the audit retention window is enforced by the loop
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_a_tick_prunes_audit_rows_past_the_retention_window(
    session_factory: sessionmaker[Session],
    idle_client: httpx.Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retention has to be enforced somewhere, and this pass is the only place.

    Without it the table grows without bound, which ADR 0016 made a real
    concern rather than a tidiness one: once the service is publicly reachable,
    every unauthorized probe is a row.
    """
    monkeypatch.setenv("PEARSS_AUDIT_RETENTION_DAYS", "30")

    with session_factory() as session:
        for audit_id, occurred in (
            ("audit-stale", "2020-01-01T00:00:00.000000Z"),
            ("audit-recent", "2099-01-01T00:00:00.000000Z"),
        ):
            session.add(
                AuditLog(
                    id=audit_id,
                    occurred_at=occurred,
                    actor="probe",
                    operation="GET /v1/items",
                    args_redacted="{}",
                    outcome="rejected",
                    correlation_id=audit_id,
                )
            )
        session.commit()

    clock = _Clock()
    scheduler = _scheduler(session_factory, idle_client, clock)
    asyncio.run(scheduler._prune_audit_if_due())

    with session_factory() as session:
        remaining = {
            row[0]
            for row in session.execute(sa.text("select id from audit_log")).fetchall()
        }
    assert remaining == {"audit-recent"}, remaining


@pytest.mark.integration
def test_retention_of_zero_disables_pruning(
    session_factory: sessionmaker[Session],
    idle_client: httpx.Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`PEARSS_AUDIT_RETENTION_DAYS=0` means keep everything.

    An operator who wants an append-only audit trail must be able to say so;
    a "0 means delete everything" reading would be catastrophic for a fact
    source, so the boundary is pinned rather than assumed.
    """
    monkeypatch.setenv("PEARSS_AUDIT_RETENTION_DAYS", "0")

    with session_factory() as session:
        session.add(
            AuditLog(
                id="audit-ancient",
                occurred_at="1999-01-01T00:00:00.000000Z",
                actor="probe",
                operation="GET /v1/items",
                args_redacted="{}",
                outcome="rejected",
                correlation_id="audit-ancient",
            )
        )
        session.commit()

    clock = _Clock()
    scheduler = _scheduler(session_factory, idle_client, clock)
    asyncio.run(scheduler._prune_audit_if_due())

    with session_factory() as session:
        count = session.execute(sa.text("select count(*) from audit_log")).scalar_one()
    assert count == 1


# --------------------------------------------------------------------------
# Scenario: the loop actually calls the maintenance passes
# --------------------------------------------------------------------------
#
# The tests above drive the passes directly, which proves they work but not
# that anything invokes them. That distinction is the whole reason this file
# exists -- the repository functions were already tested while the loop's calls
# to them were not -- so the wiring gets its own check.


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_tick_loop_calls_both_maintenance_passes(
    session_factory: sessionmaker[Session],
    idle_client: httpx.Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_loop` must run both passes after each tick.

    Written against the loop body rather than by starting a background task
    with a real sleep: the assertion is "these three things happen in this
    order, every iteration", and `asyncio.sleep` is what makes an iteration
    long. Replacing only the sleep keeps the ordering real while removing the
    wait.
    """
    called: list[str] = []
    from app.scheduler import Scheduler

    scheduler = Scheduler(
        session_factory,
        client=idle_client,
        tick_seconds=3600,
        monotonic=_Clock(),
    )

    async def _tick() -> None:
        called.append("tick")

    async def _reap() -> None:
        called.append("reap")

    async def _prune() -> None:
        called.append("prune")

    monkeypatch.setattr(scheduler, "tick_once", _tick)
    monkeypatch.setattr(scheduler, "_reap_orphaned_runs_if_due", _reap)
    monkeypatch.setattr(scheduler, "_prune_audit_if_due", _prune)

    slept: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        # The loop sleeps *before* its body, so the body runs once per sleep.
        # Letting three sleeps through yields two complete iterations, which is
        # what shows the ordering is a loop body and not a one-shot sequence.
        slept.append(seconds)
        if len(slept) > 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await scheduler._loop()

    # The sleep is the tick interval, and the three passes run in this order
    # after every wait -- twice, so a reordering or a dropped call is visible.
    assert slept == [3600, 3600, 3600], slept
    assert called == ["tick", "reap", "prune"] * 2, called
