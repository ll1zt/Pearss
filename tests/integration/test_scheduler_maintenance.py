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

There is a third pass now: `_prune_work_ledger_if_due` enforces the ledger's
retention window (ADR 0046 D-F, issue #203). It sits here for the same reason as
the other two -- the repository function underneath can be correct while the
loop never calls it -- and its scenario also pins what the window must *not*
reach: a work with no end, and every table that is not the ledger.

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

from adapters.audit import record
from adapters.db_models import AuditLog, Work
from adapters.repository import (
    append_unit,
    claim_job_run,
    create_job_run,
    create_source,
    create_work,
    enqueue_chain,
    ensure_collect_job,
    finish_work,
    timestamp,
)
from app.pipeline import normalise_and_store
from app.scheduler import PRUNE_INTERVAL_SECONDS
from core.enrichment import EnrichmentKind, EnrichmentSpec
from core.enums import (
    AuditOutcome,
    JobRunStatus,
    SourceStatus,
    UnitOutcome,
    WorkStatus,
)
from core.models import ItemDraft

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
# Scenario: the loop itself closes orphans and prunes expired audit rows
# --------------------------------------------------------------------------
#
# The tests above drive the passes directly, which proves they work but not
# that anything invokes them. That distinction is the whole reason this file
# exists -- the repository functions were already tested while the loop's calls
# to them were not -- so the wiring gets its own check: the real loop, real
# repositories, and only the timer replaced.


@pytest.mark.integration
@pytest.mark.asyncio
async def test_user_abandoned_runs_close_and_expired_audits_disappear_in_the_loop(
    session_factory: sessionmaker[Session],
    idle_client: httpx.Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Given stale and live facts, when the loop runs, only stale facts change.

    Drive the real loop, not startup reaping or substituted maintenance methods.
    Paused sources keep collection idle without replacing tick_once. Only the
    timer is virtual: each wait advances time and bounds the loop's lifetime.
    """
    monkeypatch.setenv("PEARSS_AUDIT_RETENTION_DAYS", "30")
    with session_factory() as session:
        for run_id, claimed in (
            ("abandoned", _claimed_ago(days=1)),
            ("live", _claimed_ago(minutes=1)),
        ):
            url = f"https://{run_id}.example/feed"
            source = create_source(
                session,
                source_type="rss",
                url=url,
                canonical_url=url,
                schedule_policy='{"type":"fixed","interval":"30m"}',
                tenant_id="default",
                at=claimed,
            )
            source.status = SourceStatus.PAUSED.value
            job_id = ensure_collect_job(
                session,
                source_id=source.id,
                tenant_id="default",
                at=claimed,
            )
            create_job_run(
                session,
                run_id=run_id,
                job_id=job_id,
                correlation_id=run_id,
                at=claimed,
            )
            claim_job_run(session, run_id=run_id, at=claimed)
        for audit_id, occurred in (
            ("expired", datetime(2020, 1, 1, tzinfo=UTC)),
            ("retained", datetime(2099, 1, 1, tzinfo=UTC)),
        ):
            record(
                session,
                audit_id=audit_id,
                actor="user",
                operation="POST /v1/sources",
                outcome=AuditOutcome.SUCCEEDED,
                correlation_id=audit_id,
                occurred_at=occurred,
            )
        # The ledger's own window, in the same pass: one finished work past it
        # and one *unfinished* work of the same age. The second is the point --
        # the loop must not delete a row a running executor is writing to.
        expired_work = create_work(
            session,
            kind="delete",
            targets=["it-loop-expired"],
            params={},
            fingerprint="fp-loop-expired",
            tenant_id="default",
        )
        append_unit(
            session,
            work_id=expired_work.id,
            target="it-loop-expired",
            outcome=UnitOutcome.OK,
        )
        finish_work(
            session,
            expired_work.id,
            status=WorkStatus.SUCCEEDED,
            now=datetime.now(UTC) - timedelta(days=40),
        )
        live_work = create_work(
            session,
            kind="fetch",
            targets=["https://live-work.example/post"],
            params={},
            fingerprint="fp-loop-live",
            tenant_id="default",
        )
        append_unit(
            session,
            work_id=live_work.id,
            target="https://live-work.example/post",
            outcome=UnitOutcome.SKIPPED,
        )
        session.execute(
            sa.update(Work)
            .where(Work.id == live_work.id)
            .values(created_at=timestamp(datetime.now(UTC) - timedelta(days=40)))
        )
        session.commit()

    clock = _Clock()
    scheduler = _scheduler(session_factory, idle_client, clock)
    waits = 0

    async def advance_timer(seconds: float) -> None:
        nonlocal waits
        waits += 1
        if waits > 2:
            raise asyncio.CancelledError
        clock.advance(seconds)

    monkeypatch.setattr(asyncio, "sleep", advance_timer)
    with pytest.raises(asyncio.CancelledError):
        await scheduler._loop()

    assert _run_status(session_factory, "abandoned") == "timed_out"
    assert _run_status(session_factory, "live") == "running"
    with session_factory() as session:
        remaining = session.scalars(sa.select(AuditLog.id)).all()
    assert remaining == ["retained"]

    works_after, units_after = _ledger_rows(session_factory)
    assert expired_work.id not in works_after, (
        "the loop must enforce the ledger window, not only the pass when called"
    )
    assert f"{expired_work.id}:it-loop-expired" not in units_after
    assert live_work.id in works_after, "an unfinished work is live, not stale"
    assert f"{live_work.id}:https://live-work.example/post" in units_after


# --------------------------------------------------------------------------
# Scenario: the ledger's retention window, and what it must not reach
# --------------------------------------------------------------------------


def _ledger_rows(session_factory: sessionmaker[Session]) -> tuple[set[str], set[str]]:
    """Every work id and every `work:target` unit currently in the ledger."""
    with session_factory() as session:
        works = {str(row[0]) for row in session.execute(sa.text("select id from work"))}
        units = {
            f"{row[0]}:{row[1]}"
            for row in session.execute(sa.text("select work_id, target from work_unit"))
        }
    return works, units


def _fact_counts(session_factory: sessionmaker[Session]) -> tuple[int, int]:
    """Rows in the two fact-source tables retention must not reach."""
    with session_factory() as session:
        items = session.execute(sa.text("select count(*) from item")).scalar_one()
        enrichments = session.execute(
            sa.text("select count(*) from item_enrichment")
        ).scalar_one()
    return int(items), int(enrichments)


def _item_row(session_factory: sessionmaker[Session], item_id: str) -> tuple[Any, ...]:
    """One item's own columns, as AC-204 pins them."""
    with session_factory() as session:
        return tuple(
            session.execute(
                sa.text(
                    "select guid, content_hash, publish_status, content_ref"
                    " from item where id = :id"
                ),
                {"id": item_id},
            ).one()
        )


@pytest.mark.integration
def test_the_ledger_window_prunes_finished_work_and_leaves_live_rows_alone(
    session_factory: sessionmaker[Session],
    idle_client: httpx.Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-224: 30 days of ledger, and the three things age must not delete.

    The ledger answers "what happened to this target" and is written per target,
    so it grows with every command; ADR 0046 D-F gives it a 30-day window and
    says what it is not -- a fact source. Two halves of that are asserted here,
    and the second is the one with teeth:

    - a *finished* work older than the window goes, taking its units with it
      (the units are that work's record, not separate evidence);
    - a work with **no end** stays, however old, because a queued or running work
      is live: its row is what a running executor writes its units against, and
      deleting it would make a job that is still running look like it vanished;
    - nothing outside the ledger moves. The item and enrichment counts and the
      item's own row are compared before and after -- retention that reached
      into the corpus would be deleting facts to save space.

    The rows come from the repository (`create_work` / `finish_work` /
    `append_unit`), so this reproduces the production write path; the timestamps
    are the scenario, and they are set on the model rather than inserted.
    """
    monkeypatch.setenv("PEARSS_WORK_RETENTION_DAYS", "30")

    now = datetime.now(UTC)
    stale = now - timedelta(days=40)
    fresh = now - timedelta(hours=1)

    with session_factory() as session:
        # A fact-source row and a derived row, so "unchanged" is not vacuous.
        source = create_source(
            session,
            source_type="rss",
            url="https://retention.example/feed",
            canonical_url="https://retention.example/feed",
            schedule_policy="{}",
            tenant_id="default",
            at=timestamp(now),
        )
        item = normalise_and_store(
            session,
            [
                ItemDraft(
                    origin_id=source.id,
                    external_id="retention-1",
                    title="Kept across the prune",
                    canonical_url="https://retention.example/post-1",
                    published_at=None,
                    content="<p>Body.</p>",
                )
            ],
            tenant_id="default",
        )[0].result.item
        enqueue_chain(
            session,
            item_id=item.id,
            content_hash="hash-retention-1",
            chain=(EnrichmentSpec(plugin_id="fulltext", kind=EnrichmentKind.FULLTEXT),),
        )

        # Three works: old and finished, fresh and finished, old and *unfinished*.
        old_work = create_work(
            session,
            kind="delete",
            targets=["it-retention-old"],
            params={},
            fingerprint="fp-retention-old",
            tenant_id="default",
        )
        append_unit(
            session,
            work_id=old_work.id,
            target="it-retention-old",
            outcome=UnitOutcome.OK,
        )
        finish_work(session, old_work.id, status=WorkStatus.SUCCEEDED, now=stale)

        fresh_work = create_work(
            session,
            kind="delete",
            targets=["it-retention-fresh"],
            params={},
            fingerprint="fp-retention-fresh",
            tenant_id="default",
        )
        append_unit(
            session,
            work_id=fresh_work.id,
            target="it-retention-fresh",
            outcome=UnitOutcome.OK,
        )
        finish_work(session, fresh_work.id, status=WorkStatus.SUCCEEDED, now=fresh)

        live_work = create_work(
            session,
            kind="fetch",
            targets=["https://retention.example/post-1"],
            params={},
            fingerprint="fp-retention-live",
            tenant_id="default",
        )
        append_unit(
            session,
            work_id=live_work.id,
            target="https://retention.example/post-1",
            outcome=UnitOutcome.SKIPPED,
            detail="still going",
        )
        # An old *unfinished* work: the case where age alone would be wrong.
        session.execute(
            sa.update(Work)
            .where(Work.id == live_work.id)
            .values(created_at=timestamp(stale))
        )
        session.commit()

    facts_before = _fact_counts(session_factory)
    item_before = _item_row(session_factory, item.id)
    works_before, units_before = _ledger_rows(session_factory)
    assert {old_work.id, fresh_work.id, live_work.id} <= works_before

    clock = _Clock()
    scheduler = _scheduler(session_factory, idle_client, clock)
    asyncio.run(scheduler._prune_work_ledger_if_due())

    works_after, units_after = _ledger_rows(session_factory)

    assert old_work.id not in works_after, "a finished work past 30 days goes"
    assert not {row for row in units_after if row.startswith(f"{old_work.id}:")}, (
        "its units are that work's record and go with it"
    )

    assert fresh_work.id in works_after, "a finished work inside the window stays"
    assert f"{fresh_work.id}:it-retention-fresh" in units_after, units_after

    assert live_work.id in works_after, (
        "a work with no end is live, not stale: age alone must never delete it, "
        "because a running executor writes its units against that row"
    )
    assert f"{live_work.id}:https://retention.example/post-1" in units_after

    assert works_after == works_before - {old_work.id}, works_after ^ works_before
    assert units_after == units_before - (units_before - units_after), (
        units_after,
        units_before,
    )
    assert _fact_counts(session_factory) == facts_before, (
        "retention may not reach outside the ledger"
    )
    assert _item_row(session_factory, item.id) == item_before, (
        "the item's own row is untouched"
    )


def _seed_finished_work(
    session: Session, *, fingerprint: str, target: str, finished_days_ago: float
) -> str:
    """A finished work with one unit, through the repository's own write path."""
    work = create_work(
        session,
        kind="delete",
        targets=[target],
        params={},
        fingerprint=fingerprint,
        tenant_id="default",
    )
    append_unit(session, work_id=work.id, target=target, outcome=UnitOutcome.OK)
    finish_work(
        session,
        work.id,
        status=WorkStatus.SUCCEEDED,
        now=datetime.now(UTC) - timedelta(days=finished_days_ago),
    )
    session.flush()
    return work.id


@pytest.mark.integration
def test_ledger_retention_of_zero_keeps_every_row(
    session_factory: sessionmaker[Session],
    idle_client: httpx.Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`PEARSS_WORK_RETENTION_DAYS=0` means keep the ledger forever.

    The same escape hatch the audit window has, for the same reason: an operator
    who wants the whole history must be able to say so. The setting has to reach
    the *pass* rather than only the parser -- a window read as zero that still
    deleted rows would be the worst possible reading of "keep everything".
    """
    monkeypatch.setenv("PEARSS_WORK_RETENTION_DAYS", "0")
    with session_factory() as session:
        work_id = _seed_finished_work(
            session,
            fingerprint="fp-retention-forever",
            target="it-retention-forever",
            finished_days_ago=400,
        )
        session.commit()

    clock = _Clock()
    scheduler = _scheduler(session_factory, idle_client, clock)
    asyncio.run(scheduler._prune_work_ledger_if_due())

    works_after, units_after = _ledger_rows(session_factory)
    assert work_id in works_after, "0 days means keep everything"
    assert f"{work_id}:it-retention-forever" in units_after


@pytest.mark.integration
def test_the_ledger_pass_is_throttled_between_calls(
    session_factory: sessionmaker[Session],
    idle_client: httpx.Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The window is enforced once per maintenance interval, not every tick.

    Asserted on the ledger rather than on the timer field: an expired work that
    appears *after* a pass survives the next call, because that call is inside
    the interval and must not run the delete. A pass per tick would be a delete
    against the ledger every few seconds, which is the cost the interval exists
    to avoid.
    """
    monkeypatch.setenv("PEARSS_WORK_RETENTION_DAYS", "30")
    clock = _Clock()
    scheduler = _scheduler(session_factory, idle_client, clock)
    asyncio.run(scheduler._prune_work_ledger_if_due())

    with session_factory() as session:
        work_id = _seed_finished_work(
            session,
            fingerprint="fp-retention-throttled",
            target="it-retention-throttled",
            finished_days_ago=40,
        )
        session.commit()

    # The clock has not moved, so this call is inside the interval.
    asyncio.run(scheduler._prune_work_ledger_if_due())

    works_after, _ = _ledger_rows(session_factory)
    assert work_id in works_after, (
        "a second pass inside the interval must not run the delete"
    )

    # Past the interval it does run: the assertion above is about the throttle,
    # not about the row being unreachable.
    clock.advance(PRUNE_INTERVAL_SECONDS + 1)
    asyncio.run(scheduler._prune_work_ledger_if_due())
    works_final, _ = _ledger_rows(session_factory)
    assert work_id not in works_final
