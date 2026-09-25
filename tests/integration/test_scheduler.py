"""Integration tests for the scheduler.

The properties under test are the ones the derived design promises: a due
source is collected, a non-due one is not, a policy change takes effect without
anyone rescheduling anything, and two schedulers ticking together collect a
source only once.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from adapters.db_models import AuditLog
from adapters.repository import (
    claim_job_run,
    collectable_sources,
    consecutive_failures,
    create_job_run,
    ensure_collect_job,
    finish_job_run,
    last_collect_at,
    prune_audit_log,
    update_gaps_for_source,
)
from adapters.timestamps import fixed_width_utc
from app.main import create_engine_for
from app.scheduler import TickReport, collect_now, evaluate_source, run_tick
from core.enums import ErrorClass, JobRunStatus
from core.job_state import InvalidJobRunTransitionError, JobRunEvent
from core.models import schedule_policy_from_json, schedule_policy_to_json
from tests.migrated_schema import migrated_database

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def add_source(session: Session, *, source_id: str, policy: dict[str, object]) -> None:
    session.execute(
        sa.text(
            "insert into source (id, type, url, canonical_url, schedule_policy,"
            " status, trust_level, tenant_id, created_at, updated_at)"
            " values (:id, 'rss', :url, :url, :policy, 'active', 'standard',"
            " 'default', :now, :now)"
        ),
        {
            "id": source_id,
            "url": f"https://{source_id}.example.com/feed",
            "policy": schedule_policy_to_json(schedule_policy_from_json(policy)),
            "now": NOW.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z",
        },
    )
    session.commit()


def add_items(
    session: Session, *, source_id: str, count: int, spacing: timedelta
) -> None:
    for index in range(count):
        moment = NOW - spacing * (count - index)
        session.execute(
            sa.text(
                "insert into item (id, source_id, guid, content_hash, title, url,"
                " published_at, publish_status, tenant_id, created_at, updated_at)"
                " values (:id, :s, :g, :h, :t, :u, :p, 'ready', 'default', :p, :p)"
            ),
            {
                "id": f"{source_id}-i{index}",
                "s": source_id,
                "g": f"{source_id}-g{index}",
                "h": f"{source_id}-h{index}",
                "t": f"Post {index}",
                "u": f"https://{source_id}.example.com/{index}",
                "p": moment.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z",
            },
        )
    session.commit()


@pytest.fixture
def session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    url = migrated_database(tmp_path, "sched.db")
    engine = create_engine_for(url)
    try:
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        engine.dispose()


@pytest.fixture
def fake_feed() -> httpx.Client:
    """A client whose every response is a small valid RSS feed."""
    body = (
        b'<?xml version="1.0"?><rss version="2.0"><channel><title>F</title>'
        b"<item><title>Live post</title>"
        b"<link>https://live.example.com/post-1</link>"
        b"<guid>live-1</guid>"
        b"<pubDate>Mon, 01 Jun 2026 11:00:00 +0000</pubDate>"
        b"<description>Body.</description></item></channel></rss>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


# --------------------------------------------------------------------------
# Job lifecycle
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_collect_job_is_created_once_per_source(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        add_source(session, source_id="s1", policy={"type": "fixed", "interval": "30m"})
        first = ensure_collect_job(
            session,
            source_id="s1",
            tenant_id="default",
            at="2026-06-01T12:00:00.000000Z",
        )
        second = ensure_collect_job(
            session,
            source_id="s1",
            tenant_id="default",
            at="2026-06-01T12:00:00.000000Z",
        )
        session.commit()
        assert first == second
        count = session.execute(sa.text("select count(*) from job")).scalar()
        assert count == 1


@pytest.mark.integration
def test_job_run_walks_its_state_machine(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        add_source(session, source_id="s1", policy={"type": "fixed", "interval": "30m"})
        job_id = ensure_collect_job(
            session,
            source_id="s1",
            tenant_id="default",
            at="2026-06-01T12:00:00.000000Z",
        )
        create_job_run(
            session,
            run_id="r1",
            job_id=job_id,
            correlation_id="c1",
            at="2026-06-01T12:00:00.000000Z",
        )
        session.commit()
        assert claim_job_run(session, run_id="r1", at="2026-06-01T12:00:01.000000Z")
        status = finish_job_run(
            session,
            run_id="r1",
            event=JobRunEvent.SUCCEED,
            correlation_id="c1",
            metrics={"inserted": 3},
            at="2026-06-01T12:00:02.000000Z",
        )
        session.commit()
        assert status is JobRunStatus.SUCCEEDED
        row = session.execute(
            sa.text(
                "select status, metrics, finished_at, started_at"
                " from job_run where id='r1'"
            )
        ).one()
        assert row[0] == "succeeded"
        assert '"inserted": 3' in row[1]
        # Pinned to the injected clock, not merely non-null: a wrong timestamp
        # (wrong event's time, wrong width) passed the old presence check.
        # Changing these requires a data migration.
        assert row[2] == "2026-06-01T12:00:02.000000Z", row[2]
        assert row[3] == "2026-06-01T12:00:01.000000Z", row[3]


@pytest.mark.integration
def test_reopening_a_terminal_run_is_refused(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        add_source(session, source_id="s1", policy={"type": "fixed", "interval": "30m"})
        job_id = ensure_collect_job(
            session,
            source_id="s1",
            tenant_id="default",
            at="2026-01-01T00:00:00.000000Z",
        )
        create_job_run(session, run_id="r1", job_id=job_id, correlation_id="c1", at="x")
        claim_job_run(session, run_id="r1", at="x")
        finish_job_run(
            session,
            run_id="r1",
            event=JobRunEvent.SUCCEED,
            correlation_id="c1",
            at="x",
        )
        with pytest.raises(InvalidJobRunTransitionError, match="terminal"):
            finish_job_run(
                session,
                run_id="r1",
                event=JobRunEvent.FAIL,
                correlation_id="c1",
                error_class=ErrorClass.NETWORK,
                at="y",
            )


@pytest.mark.integration
def test_claim_is_exclusive_under_concurrency(
    session_factory: sessionmaker[Session],
) -> None:
    """The conditional update stops one run row being claimed twice.

    Job-level exclusion between two *different* runs is the unique partial
    index's job (see test_second_collection_while_a_run_is_active_stands_down);
    this pins the row lifecycle guard underneath it.
    """
    with session_factory() as session:
        add_source(session, source_id="s1", policy={"type": "fixed", "interval": "30m"})
        job_id = ensure_collect_job(
            session,
            source_id="s1",
            tenant_id="default",
            at="2026-01-01T00:00:00.000000Z",
        )
        create_job_run(session, run_id="r1", job_id=job_id, correlation_id="c1", at="x")
        session.commit()

    winners = 0
    for _ in range(2):
        with session_factory() as session:
            if claim_job_run(session, run_id="r1", at="2026-01-01T00:00:01.000000Z"):
                winners += 1
            session.commit()
    assert winners == 1


# --------------------------------------------------------------------------
# Derived schedule inputs
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_update_gaps_come_from_publication_times(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        add_source(
            session,
            source_id="s1",
            policy={"type": "adaptive", "min": "15m", "max": "24h"},
        )
        add_items(session, source_id="s1", count=4, spacing=timedelta(hours=2))
        gaps = update_gaps_for_source(session, source_id="s1")
    assert gaps
    assert all(gap == pytest.approx(7200.0) for gap in gaps)


@pytest.mark.integration
def test_consecutive_failures_stop_at_a_success(
    session_factory: sessionmaker[Session],
) -> None:
    """A recovered source must not keep its backoff.

    Counting every failure ever would leave a source that has since succeeded
    throttled forever.
    """
    with session_factory() as session:
        add_source(session, source_id="s1", policy={"type": "fixed", "interval": "30m"})
        job_id = ensure_collect_job(
            session,
            source_id="s1",
            tenant_id="default",
            at="2026-01-01T00:00:00.000000Z",
        )
        # Two failures, then a success, then two more failures.
        for index, event in enumerate(
            [
                JobRunEvent.FAIL,
                JobRunEvent.FAIL,
                JobRunEvent.SUCCEED,
                JobRunEvent.FAIL,
                JobRunEvent.FAIL,
            ]
        ):
            run_id = f"r{index}"
            create_job_run(
                session,
                run_id=run_id,
                job_id=job_id,
                correlation_id=f"c{index}",
                at=f"2026-01-0{index + 1}T00:00:00.000000Z",
            )
            claim_job_run(
                session, run_id=run_id, at=f"2026-01-0{index + 1}T00:00:01.000000Z"
            )
            finish_job_run(
                session,
                run_id=run_id,
                event=event,
                correlation_id=f"c{index}",
                error_class=ErrorClass.NETWORK if event is JobRunEvent.FAIL else None,
                at=f"2026-01-0{index + 1}T00:00:02.000000Z",
            )
        session.commit()
        assert consecutive_failures(session, job_id=job_id) == 2


@pytest.mark.integration
def test_last_collect_at_ignores_unfinished_runs(
    session_factory: sessionmaker[Session],
) -> None:
    """An unfinished run is not evidence that a collection happened.

    Seeding a real queued/running row is the whole test: asserting None on an
    empty table cannot distinguish "filters on finished_at" from "returns
    None unconditionally".
    """
    with session_factory() as session:
        add_source(session, source_id="s1", policy={"type": "fixed", "interval": "30m"})
        job_id = ensure_collect_job(
            session,
            source_id="s1",
            tenant_id="default",
            at="2026-01-01T00:00:00.000000Z",
        )
        # A run that was claimed but never finished: finished_at stays null.
        create_job_run(
            session, run_id="r-open", job_id=job_id, correlation_id="c1", at="x"
        )
        claim_job_run(session, run_id="r-open", at="2026-01-01T00:00:01.000000Z")
        session.commit()

        assert last_collect_at(session, job_id=job_id) is None

        # Finishing it makes the same job visible, proving the query looks at
        # finished runs rather than filtering the job out some other way.
        finish_job_run(
            session,
            run_id="r-open",
            event=JobRunEvent.SUCCEED,
            correlation_id="c1",
            at="2026-01-01T00:00:02.000000Z",
        )
        session.commit()
        assert last_collect_at(session, job_id=job_id) is not None


# --------------------------------------------------------------------------
# run_tick
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_tick_collects_a_never_fetched_source(
    session_factory: sessionmaker[Session], fake_feed: httpx.Client
) -> None:
    with session_factory() as session:
        add_source(session, source_id="s1", policy={"type": "fixed", "interval": "30m"})

    report = run_tick(session_factory, client=fake_feed, now=NOW)
    assert report.considered == 1
    assert report.due == 1
    assert report.collected == ["s1"]

    with session_factory() as session:
        inserted = session.execute(sa.text("select count(*) from item")).scalar()
        runs = session.execute(sa.text("select status from job_run")).fetchall()
    assert inserted == 1
    assert [row[0] for row in runs] == ["succeeded"]


@pytest.mark.integration
def test_second_tick_skips_a_source_that_is_not_due(
    session_factory: sessionmaker[Session], fake_feed: httpx.Client
) -> None:
    with session_factory() as session:
        add_source(session, source_id="s1", policy={"type": "fixed", "interval": "30m"})

    run_tick(session_factory, client=fake_feed, now=NOW)
    later = run_tick(session_factory, client=fake_feed, now=NOW + timedelta(minutes=5))
    assert later.due == 0
    assert later.skipped == ["s1"]


@pytest.mark.integration
def test_a_policy_change_takes_effect_without_rescheduling(
    session_factory: sessionmaker[Session], fake_feed: httpx.Client
) -> None:
    """The headline property of the derived design.

    With a stored next-run time, shortening an interval does nothing until
    someone calls reschedule. Here the next tick simply recomputes and sees a
    source that is already overdue.
    """
    with session_factory() as session:
        add_source(session, source_id="s1", policy={"type": "fixed", "interval": "1h"})
    run_tick(session_factory, client=fake_feed, now=NOW)

    # Shorten the interval immediately after a successful run.
    with session_factory() as session:
        session.execute(
            sa.text("update source set schedule_policy = :p"),
            {
                "p": schedule_policy_to_json(
                    schedule_policy_from_json({"type": "fixed", "interval": "1m"})
                )
            },
        )
        session.commit()

    report = run_tick(session_factory, client=fake_feed, now=NOW + timedelta(minutes=2))
    assert report.due == 1, "a shortened interval must take effect on the next tick"


@pytest.mark.integration
def test_failures_back_off_without_any_reschedule(
    session_factory: sessionmaker[Session],
) -> None:
    """A failing source must slow down on its own."""
    failing = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(403))
    )
    with session_factory() as session:
        add_source(session, source_id="s1", policy={"type": "fixed", "interval": "60s"})

    first = run_tick(session_factory, client=failing, now=NOW)
    assert first.failed == ["s1"]

    # 30s later the un-backed-off interval would have elapsed; the doubled one
    # has not, so the source is correctly held back.
    second = run_tick(session_factory, client=failing, now=NOW + timedelta(seconds=30))
    assert second.due == 0

    # Well past the backoff, it is tried again.
    third = run_tick(session_factory, client=failing, now=NOW + timedelta(seconds=200))
    assert third.due == 1

    with session_factory() as session:
        statuses = [
            row[0]
            for row in session.execute(
                sa.text("select status from job_run order by id")
            )
        ]
        classes = [
            row[0]
            for row in session.execute(
                sa.text("select error_class from job_run where error_class is not null")
            )
        ]
    assert statuses == ["failed", "failed"]
    assert set(classes) == {ErrorClass.BLOCKED.value}


@pytest.mark.integration
def test_paused_sources_are_not_collected(
    session_factory: sessionmaker[Session], fake_feed: httpx.Client
) -> None:
    with session_factory() as session:
        add_source(session, source_id="s1", policy={"type": "fixed", "interval": "30m"})
        session.execute(sa.text("update source set status = 'paused'"))
        session.commit()

    report = run_tick(session_factory, client=fake_feed, now=NOW)
    assert report.considered == 0
    assert report.due == 0


@pytest.mark.integration
def test_one_broken_source_does_not_end_the_tick(
    session_factory: sessionmaker[Session], fake_feed: httpx.Client
) -> None:
    """A malformed policy must not stop every other source being collected.

    Without the per-source guard this presents as a quietly idle scheduler
    rather than a failing one, which is the worst possible failure mode.
    """
    with session_factory() as session:
        add_source(
            session, source_id="good", policy={"type": "fixed", "interval": "30m"}
        )
        session.execute(
            sa.text(
                "insert into source (id, type, url, canonical_url, schedule_policy,"
                " status, trust_level, tenant_id, created_at, updated_at)"
                " values ('broken', 'rss', 'https://b.example/f', 'https://b.example/f',"
                " 'not json', 'active', 'standard', 'default',"
                " '2026-06-01T12:00:00.000000Z', '2026-06-01T12:00:00.000000Z')"
            )
        )
        session.commit()

    report = run_tick(session_factory, client=fake_feed, now=NOW)
    assert "good" in report.collected
    assert "broken" in report.failed


@pytest.mark.integration
def test_adaptive_source_tracks_observed_cadence(
    session_factory: sessionmaker[Session], fake_feed: httpx.Client
) -> None:
    """A slow-publishing source should be fetched less often than a fast one."""
    with session_factory() as session:
        add_source(
            session,
            source_id="slow",
            policy={"type": "adaptive", "min": "15m", "max": "24h"},
        )
        add_items(session, source_id="slow", count=6, spacing=timedelta(hours=6))
        gaps = update_gaps_for_source(session, source_id="slow")
    assert gaps
    decision = schedule_policy_from_json  # noqa: F841  (readability anchor)
    # 6-hourly publication should place the next attempt hours out, not at min.
    report = run_tick(session_factory, client=fake_feed, now=NOW)
    assert report.collected == ["slow"]
    soon = run_tick(session_factory, client=fake_feed, now=NOW + timedelta(minutes=20))
    assert soon.due == 0, (
        "an adaptive source measured as slow must not be re-fetched soon"
    )


@pytest.mark.integration
def test_tick_report_counts_add_up(
    session_factory: sessionmaker[Session], fake_feed: httpx.Client
) -> None:
    with session_factory() as session:
        for name in ("a", "b", "c"):
            add_source(
                session, source_id=name, policy={"type": "fixed", "interval": "30m"}
            )

    report = run_tick(session_factory, client=fake_feed, now=NOW)
    assert isinstance(report, TickReport)
    assert report.considered == 3
    assert len(report.collected) + len(report.failed) + len(report.skipped) == 3


@pytest.mark.integration
def test_collect_now_returns_a_cancelled_run_when_claim_loses(
    session_factory: sessionmaker[Session], fake_feed: httpx.Client
) -> None:
    """A scheduler that loses the race must do no work, not duplicate it.

    The competing run has to exist before `collect_now` is called, or the
    claim never loses and the CANCELLED branch is never entered -- which is
    exactly how this test used to pass while asserting the happy path.
    """
    with session_factory() as session:
        add_source(session, source_id="s1", policy={"type": "fixed", "interval": "30m"})
        job_id = ensure_collect_job(
            session,
            source_id="s1",
            tenant_id="default",
            at="2026-01-01T00:00:00.000000Z",
        )
        # A live run for the same job: the unique partial index refuses a
        # second active run, so the caller's claim stands down.
        create_job_run(
            session, run_id="r-holder", job_id=job_id, correlation_id="c-holder", at="x"
        )
        claim_job_run(session, run_id="r-holder", at="2026-01-01T00:00:01.000000Z")
        session.commit()

    with session_factory() as session:
        status, result = collect_now(
            session,
            source_id="s1",
            policy_json='{"type": "fixed", "interval": "30m"}',
            client=fake_feed,
            now=NOW,
        )
        session.commit()

    assert status is JobRunStatus.CANCELLED
    assert result is None, "a cancelled run must not have collected anything"

    with session_factory() as session:
        inserted = session.execute(sa.text("select count(*) from item")).scalar()
    assert inserted == 0, "the losing caller must not have fetched or stored"


@pytest.mark.integration
def test_second_collection_while_a_run_is_active_stands_down(
    session_factory: sessionmaker[Session], fake_feed: httpx.Client
) -> None:
    """A job with an active run refuses a second collection.

    Regression: each caller creates its own run id, so a conditional update on
    that row can never arbitrate between them. The unique partial index over
    active runs per job is what stops a manual trigger and a scheduler tick
    from both fetching the same source.
    """
    with session_factory() as session:
        add_source(session, source_id="s1", policy={"type": "fixed", "interval": "30m"})
        job_id = ensure_collect_job(
            session,
            source_id="s1",
            tenant_id="default",
            at="2026-01-01T00:00:00.000000Z",
        )
        create_job_run(
            session, run_id="r-live", job_id=job_id, correlation_id="c1", at="x"
        )
        claim_job_run(session, run_id="r-live", at="2026-01-01T00:00:01.000000Z")
        session.commit()

    with session_factory() as session:
        status, result = collect_now(
            session,
            source_id="s1",
            policy_json='{"type": "fixed", "interval": "30m"}',
            client=fake_feed,
            now=NOW,
        )
        session.commit()
    assert status is JobRunStatus.CANCELLED
    assert result is None


@pytest.mark.integration
def test_collectable_sources_lists_only_active(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        add_source(
            session, source_id="active-one", policy={"type": "fixed", "interval": "30m"}
        )
        add_source(
            session, source_id="gone", policy={"type": "fixed", "interval": "30m"}
        )
        session.execute(sa.text("update source set status='disabled' where id='gone'"))
        session.commit()
        rows = collectable_sources(session, tenant_id="default")
    assert [row[0] for row in rows] == ["active-one"]


@pytest.mark.integration
def test_agent_sources_are_not_collectable(
    session_factory: sessionmaker[Session],
) -> None:
    """An agent source is pushed to, not polled (ADR 0022).

    `select_for("agent", ...)` falls through to `select(url)`, which matches no
    collector, so scheduling one produces a failed JobRun every tick -- a
    permanent fake failure that also corrupts the success-rate signal. The
    exclusion belongs in this query rather than in a per-source skip inside the
    tick: an agent source should never enter the pass at all.
    """
    with session_factory() as session:
        add_source(
            session, source_id="pushed", policy={"type": "fixed", "interval": "30m"}
        )
        session.execute(
            sa.text(
                "insert into source (id, type, url, canonical_url, schedule_policy,"
                " status, trust_level, tenant_id, created_at, updated_at)"
                " values ('intake', 'agent', 'agent://intake', 'agent://intake',"
                " :policy, 'active', 'standard', 'default', :now, :now)"
            ),
            {
                "policy": schedule_policy_to_json(
                    schedule_policy_from_json({"type": "fixed", "interval": "30m"})
                ),
                "now": NOW.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z",
            },
        )
        session.commit()
        rows = collectable_sources(session, tenant_id="default")
    assert [row[0] for row in rows] == ["pushed"]


# --------------------------------------------------------------------------
# The loop wrapper
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_overlapping_ticks_are_serialised(
    session_factory: sessionmaker[Session], fake_feed: httpx.Client
) -> None:
    """A tick slower than the interval must not stack up.

    Without the lock, a pass taking longer than tick_seconds would start
    another while the first ran, and both would scan the same sources.

    The replacement is synchronous on purpose: tick_once hands run_tick to
    asyncio.to_thread, which invokes a plain callable in a worker thread. An
    async stand-in would only produce a coroutine that is never awaited, and
    the test would pass without ever entering the lock.

    Serialisation is proven with an event gate rather than a sleep window:
    the first tick blocks until released, so a second tick has every
    opportunity to overlap if the lock were absent. A timing-based version
    ("sleep 50ms, hope four coroutines overlap") is flaky on a loaded runner
    and can pass with no contention at all.
    """
    import asyncio
    import threading

    import app.scheduler as scheduler_module
    from app.scheduler import Scheduler

    entered = threading.Event()
    release = threading.Event()
    concurrent = threading.Semaphore(0)
    in_flight = 0
    peak = 0
    counter_lock = threading.Lock()
    calls = 0

    def gated_run_tick(*args: object, **kwargs: object) -> TickReport:
        nonlocal in_flight, peak, calls
        with counter_lock:
            in_flight += 1
            peak = max(peak, in_flight)
            calls += 1
            first = calls == 1
        if first:
            entered.set()
            # Hold the tick open until the test has launched its rivals.
            release.wait(timeout=10)
        else:
            concurrent.release()
        with counter_lock:
            in_flight -= 1
        return TickReport(considered=0, due=0, collected=[], failed=[], skipped=[])

    scheduler = Scheduler(session_factory, client=fake_feed, tick_seconds=0.01)
    original = scheduler_module.run_tick
    scheduler_module.run_tick = gated_run_tick
    try:
        first = asyncio.create_task(scheduler.tick_once())
        # Wait until the first tick genuinely holds the lock.
        assert await asyncio.to_thread(entered.wait, 10), "the first tick never ran"
        rivals = [asyncio.create_task(scheduler.tick_once()) for _ in range(3)]
        # Give the rivals every chance to enter while the first is blocked.
        overlapped = await asyncio.to_thread(concurrent.acquire, True, 0.5)
        assert not overlapped, "a second tick entered while the first held the lock"
        release.set()
        await asyncio.gather(first, *rivals)
    finally:
        scheduler_module.run_tick = original

    assert peak == 1, f"ticks overlapped: {peak} ran at once"
    assert calls == 4, f"every queued tick must still run, got {calls}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_scheduler_starts_and_stops_cleanly(
    session_factory: sessionmaker[Session], fake_feed: httpx.Client
) -> None:
    from app.scheduler import Scheduler

    scheduler = Scheduler(session_factory, client=fake_feed, tick_seconds=3600)
    with session_factory() as session:
        add_source(session, source_id="s1", policy={"type": "fixed", "interval": "30m"})

    await scheduler.start()
    # Starting twice must not leave a second loop running.
    await scheduler.start()
    await scheduler.stop()
    # Stopping an already-stopped scheduler is a no-op rather than an error.
    await scheduler.stop()

    # A long interval means the loop never fired on its own during the test, so
    # the source is still uncollected; driving one tick proves the object works.
    report = await scheduler.tick_once()
    assert report.considered == 1


@pytest.mark.integration
def test_scheduler_can_be_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.scheduler import scheduler_enabled

    monkeypatch.delenv("PEARSS_SCHEDULER_ENABLED", raising=False)
    assert scheduler_enabled()
    for value in ("0", "false", "no", "off", "OFF"):
        monkeypatch.setenv("PEARSS_SCHEDULER_ENABLED", value)
        assert not scheduler_enabled(), value
    monkeypatch.setenv("PEARSS_SCHEDULER_ENABLED", "1")
    assert scheduler_enabled()


@pytest.mark.integration
def test_orphaned_running_runs_are_reaped_on_start(
    session_factory: sessionmaker[Session], fake_feed: httpx.Client
) -> None:
    """A run left `running` by a dead process is never closed by anyone.

    Without reaping, the history keeps showing work in progress that stopped
    existing hours ago — and eight such rows accumulated in practice before
    this existed.
    """
    import asyncio

    from app.scheduler import Scheduler

    with session_factory() as session:
        add_source(session, source_id="s1", policy={"type": "fixed", "interval": "30m"})
        job_id = ensure_collect_job(
            session,
            source_id="s1",
            tenant_id="default",
            at="2026-01-01T00:00:00.000000Z",
        )
        create_job_run(
            session, run_id="orphan", job_id=job_id, correlation_id="c1", at="x"
        )
        claim_job_run(session, run_id="orphan", at="2020-01-01T00:00:00.000000Z")
        session.commit()

    scheduler = Scheduler(session_factory, client=fake_feed, tick_seconds=3600)
    asyncio.run(scheduler.start())

    with session_factory() as session:
        row = session.execute(
            sa.text(
                "select status, error_class, finished_at from job_run where id='orphan'"
            )
        ).one()
    assert row[0] == "timed_out"
    assert row[1] == "network"
    # The reap decision stamps the wall clock (scheduler.start() takes no
    # injectable one), so the exact instant cannot be pinned here. The
    # format contract can: fixed-width UTC with a Z suffix — a naive
    # timestamp, a wrong column, or isoformat()'s variable width all fail.
    import re as _re

    assert _re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z", row[2]), row[2]


@pytest.mark.integration
def test_a_live_run_is_not_reaped(
    session_factory: sessionmaker[Session],
) -> None:
    """Only runs older than the reap window are touched.

    Reaping a run that is genuinely in flight would mark live work failed.
    The cutoff is a fixed instant and the run is claimed at a fixed instant
    after it, so the boundary is deterministic -- a wall-clock version would
    flip if the machine stalled between the claim and the reap.
    """
    from datetime import timedelta

    from adapters.repository import reap_orphaned_runs

    cutoff = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    with session_factory() as session:
        add_source(session, source_id="s1", policy={"type": "fixed", "interval": "30m"})
        job_id = ensure_collect_job(
            session,
            source_id="s1",
            tenant_id="default",
            at="2026-01-01T00:00:00.000000Z",
        )
        create_job_run(
            session, run_id="live", job_id=job_id, correlation_id="c1", at="x"
        )
        # Claimed one second after the cutoff: inside the reap window.
        claim_job_run(
            session, run_id="live", at=fixed_width_utc(cutoff + timedelta(seconds=1))
        )
        session.commit()

        reaped = reap_orphaned_runs(session, older_than=cutoff)
        session.commit()
        assert reaped == 0
        status = session.execute(
            sa.text("select status from job_run where id='live'")
        ).scalar()
    assert status == "running"

    # The complement, at the same fixed instant, proves the window is enforced
    # and not merely "never reaps".
    with session_factory() as session:
        add_source(session, source_id="s2", policy={"type": "fixed", "interval": "30m"})
        job_id = ensure_collect_job(
            session,
            source_id="s2",
            tenant_id="default",
            at="2026-01-01T00:00:00.000000Z",
        )
        create_job_run(
            session, run_id="stale", job_id=job_id, correlation_id="c2", at="x"
        )
        # Claimed one second *before* the cutoff: outside the window.
        claim_job_run(
            session, run_id="stale", at=fixed_width_utc(cutoff - timedelta(seconds=1))
        )
        session.commit()
        reaped = reap_orphaned_runs(session, older_than=cutoff)
        session.commit()
        assert reaped == 1
        status = session.execute(
            sa.text("select status from job_run where id='stale'")
        ).scalar()
    assert status == "timed_out"


@pytest.mark.integration
def test_audit_retention_prunes_only_old_rows(
    session_factory: sessionmaker[Session],
) -> None:
    """ADR 0016: audit rows older than the retention window are deleted.

    A public deployment turns every unauthorized probe into an audit row, so
    without a retention window the forensic table itself becomes the disk
    exhaustion channel. The cutoff is the fixed-width text format, so the
    comparison stays exact.
    """
    from datetime import UTC
    from datetime import datetime as dt

    with session_factory() as session:
        for audit_id, occurred in (
            ("audit-old", "2020-01-01T00:00:00.000000Z"),
            ("audit-new", "2099-01-01T00:00:00.000000Z"),
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
        pruned = prune_audit_log(session, older_than=dt.now(UTC).replace(year=2026))
        session.commit()

    assert pruned == 1
    with session_factory() as session:
        remaining = session.execute(sa.text("select id from audit_log")).fetchall()
    assert [row[0] for row in remaining] == ["audit-new"]


@pytest.mark.integration
def test_an_escaped_exception_closes_the_run_and_does_not_wedge_the_source(
    session_factory: sessionmaker[Session],
    fake_feed: httpx.Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raise inside the collection path must leave no open run behind.

    The run is claimed in its own transaction before collection starts, so an
    exception that escapes `collect_source` used to leave the row in
    `running` forever - and the schema's unique partial index over active
    runs then refused every future collection of this source. The run has to
    reach a terminal state here, not wait for a reaper.
    """
    import app.scheduler as scheduler_module
    from app.pipeline import collect_source as real_collect_source

    with session_factory() as session:
        add_source(session, source_id="s1", policy={"type": "fixed", "interval": "30m"})

    def raising_collect_source(*args: object, **kwargs: object) -> object:
        raise RuntimeError("the collection path exploded")

    monkeypatch.setattr(scheduler_module, "collect_source", raising_collect_source)
    status, result = collect_now(
        session_factory(),
        source_id="s1",
        policy_json='{"type":"fixed","interval":"30m"}',
        client=fake_feed,
        now=NOW,
    )
    assert status is JobRunStatus.FAILED
    # A failed result, not None: callers read None as "stood down / another
    # run in progress", and trigger turned the finished run into a
    # misleading 409 already_running (issue #2).
    assert result is not None and result.error_class == "parse"
    assert result.detail == (
        "collection raised: RuntimeError: the collection path exploded"
    )

    # The source must be collectable again immediately: no open run holding
    # the only active-run slot. (Not monkeypatch.undo(): that would also tear
    # down the autouse hermetic-DNS fixture and turn the second fetch into a
    # DNS failure unrelated to what this test exercises.)
    monkeypatch.setattr(scheduler_module, "collect_source", real_collect_source)
    status, _ = collect_now(
        session_factory(),
        source_id="s1",
        policy_json='{"type":"fixed","interval":"30m"}',
        client=fake_feed,
        now=NOW,
    )
    assert status is JobRunStatus.SUCCEEDED

    with session_factory() as session:
        rows = session.execute(
            sa.text("select status, error_class from job_run order by started_at")
        ).fetchall()
    assert ("failed", "parse") in [tuple(row) for row in rows]
    assert rows[-1][0] == "succeeded"


@pytest.mark.integration
def test_user_an_escaped_collection_error_reaches_the_run_with_its_type(
    session_factory: sessionmaker[Session],
    fake_feed: httpx.Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The last-resort handler must return a failed result, not a bare
    status: trigger reads a None result as "already running" (409) even
    though the run is finished, and the old detail was a 200-character
    traceback head whose cut hid which exception fired.

    Controlled fault injection at the collect seam: after the classification
    fixes no reachable production path escapes collection as an exception,
    so the handler can only be driven by injecting one.
    """
    from app import scheduler as scheduler_module

    with session_factory() as session:
        add_source(
            session, source_id="esc", policy={"type": "fixed", "interval": "30m"}
        )
        session.commit()

    def explode(*args: object, **kwargs: object) -> object:
        raise RuntimeError("boom: the pipeline itself broke")

    monkeypatch.setattr(scheduler_module, "collect_source", explode)
    with session_factory() as session:
        status, result = scheduler_module.collect_now(
            session,
            source_id="esc",
            policy_json='{"type": "fixed", "interval": "30m"}',
            client=fake_feed,
        )

    assert status is JobRunStatus.FAILED
    assert result is not None, "a finished run must not read as already_running"
    assert result.error_class == "parse"
    assert result.detail == (
        "collection raised: RuntimeError: boom: the pipeline itself broke"
    )
    with session_factory() as session:
        row = session.execute(sa.text("select error_class, metrics from job_run")).one()
    assert row[0] == "parse"
    assert "RuntimeError: boom" in str(row[1]), str(row[1])[:200]


@pytest.mark.integration
def test_a_run_records_its_real_duration(
    session_factory: sessionmaker[Session],
    fake_feed: httpx.Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run's stored timestamps must measure the work, not the tick.

    Given a source whose collection takes measurable time (the origin
    handler advances the injected clock mid-collection, as a slow origin
    would elapse wall time)
    When the run completes successfully
    Then `finished_at` reflects when the work ended -- not the moment the
    tick started, which currently leaves every scheduler-produced run with
    `finished_at == started_at` and a stored duration of zero (issue #17).
    """
    import app.scheduler as scheduler_module

    clock = {"now": NOW}

    def advancing_handler(request: httpx.Request) -> httpx.Response:
        # The collection itself consumes time: the fetch happens between
        # claim and finish, so the clock moves while the run is open.
        clock["now"] = clock["now"] + timedelta(seconds=90)
        return httpx.Response(
            200,
            content=(
                b'<?xml version="1.0"?><rss version="2.0"><channel><title>F</title>'
                b"<item><title>Live post</title>"
                b"<link>https://live.example.com/post-1</link>"
                b"<guid>live-1</guid>"
                b"<pubDate>Mon, 01 Jun 2026 11:00:00 +0000</pubDate>"
                b"<description>Body.</description></item></channel></rss>"
            ),
        )

    advancing_feed = httpx.Client(transport=httpx.MockTransport(advancing_handler))

    # The completion clock is a module-level seam so the test stays
    # deterministic (no sleeps). Patched with raising=False: before the
    # fix the seam does not exist and collect_now never reads a completion
    # clock at all -- which is the defect, so the final assertion fails on
    # the recorded timestamp, not on test setup.
    def fake_utcnow() -> datetime:
        return clock["now"]

    monkeypatch.setattr(scheduler_module, "_utcnow", fake_utcnow, raising=False)

    with session_factory() as session:
        add_source(session, source_id="s1", policy={"type": "fixed", "interval": "30m"})
        session.commit()

    with session_factory() as session:
        status, _ = collect_now(
            session,
            source_id="s1",
            policy_json='{"type": "fixed", "interval": "30m"}',
            client=advancing_feed,
            now=NOW,
        )
        session.commit()

    assert status is JobRunStatus.SUCCEEDED
    with session_factory() as session:
        row = session.execute(
            sa.text("select started_at, finished_at from job_run")
        ).one()
    assert row[0] == "2026-06-01T12:00:00.000000Z", row[0]
    # The collection took 90s of injected time: the finish stamp must say
    # 12:01:30, not the claim moment 12:00:00.
    assert row[1] == "2026-06-01T12:01:30.000000Z", (
        f"finished_at must record when the work ended, got {row[1]}"
    )


def _seed_failure_streak(
    session: Session,
    *,
    source_id: str,
    error_class: ErrorClass,
    count: int = 3,
) -> None:
    """Seed `count` resolved failed runs carrying the given classification.

    Started an hour before NOW so the source is due on cadence whatever the
    interval math concludes; the streak is what the retry table keys on.
    """
    job_id = ensure_collect_job(
        session,
        source_id=source_id,
        tenant_id="default",
        at="2026-06-01T11:00:00.000000Z",
    )
    for i in range(count):
        run_id = f"r-{error_class.value}-{i}"
        create_job_run(
            session, run_id=run_id, job_id=job_id, correlation_id=f"c-{i}", at="x"
        )
        claim_job_run(session, run_id=run_id, at="2026-06-01T11:00:00.000000Z")
        finish_job_run(
            session,
            run_id=run_id,
            event=JobRunEvent.FAIL,
            correlation_id=f"c-{i}",
            error_class=error_class,
            at="2026-06-01T11:00:05.000000Z",
        )
    session.commit()


@pytest.mark.integration
def test_a_blocked_streak_probes_at_the_normal_pace(
    session_factory: sessionmaker[Session],
) -> None:
    """The retry table must reach the interval through the real session.

    Given a source with three resolved failures classified `blocked` -- a
    non-retryable class per the error taxonomy
    When the scheduler evaluates the source (evaluate_source, the real
    composition of the streak query and the policy math)
    Then the next interval is exactly the configured one: the service
    already refused us, and backoff would only delay the probe that
    notices the fix (issue #5's wiring, pinned end to end). The multiplier
    is 1.0 here with no jitter, so the assertion is exact.
    """
    with session_factory() as session:
        add_source(session, source_id="s1", policy={"type": "fixed", "interval": "30m"})
        _seed_failure_streak(session, source_id="s1", error_class=ErrorClass.BLOCKED)

        decision = evaluate_source(
            session,
            source_id="s1",
            policy_json='{"type": "fixed", "interval": "30m"}',
            now=NOW,
        )
        session.commit()

    assert decision.due is True  # elapsed (1h) exceeds the 30m cadence
    assert decision.interval_seconds == 1800.0, (
        "a blocked streak must not back off -- the wiring must reach the "
        "interval, not die in a unit test"
    )


@pytest.mark.integration
def test_a_network_streak_backs_off_within_the_jitter_band(
    session_factory: sessionmaker[Session],
) -> None:
    """Retryable failures keep exponential backoff even with real jitter.

    Given three resolved failures classified `network` (retryable)
    When the scheduler evaluates the source with its production rng
    Then the interval exceeds the configured one by at least the
    deterministic backoff floor (2^3 x 0.8) and stays inside the jitter
    ceiling (2^3 x 1.2): same-tick failures spread out instead of
    spiking the upstream that just failed.
    """
    with session_factory() as session:
        add_source(session, source_id="s2", policy={"type": "fixed", "interval": "30m"})
        _seed_failure_streak(session, source_id="s2", error_class=ErrorClass.NETWORK)

        decision = evaluate_source(
            session,
            source_id="s2",
            policy_json='{"type": "fixed", "interval": "30m"}',
            now=NOW,
        )
        session.commit()

    assert decision.due is False, "backoff means the source is not due yet"
    assert 1800.0 * 8 * 0.8 <= decision.interval_seconds <= 1800.0 * 8 * 1.2, (
        decision.interval_seconds
    )
    # The next attempt is anchored on the last attempt's start (the seeded
    # streak began at 11:00:00) plus the backed-off interval, not on the
    # wall clock (issue #17's anchor rule).
    assert decision.next_due_at == NOW.replace(hour=11) + timedelta(
        seconds=decision.interval_seconds
    )
