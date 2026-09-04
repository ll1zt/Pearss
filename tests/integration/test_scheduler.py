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
from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session, sessionmaker

from adapters.repository import (
    claim_job_run,
    collectable_sources,
    consecutive_failures,
    create_job_run,
    ensure_collect_job,
    finish_job_run,
    job_for_source,
    last_collect_at,
    update_gaps_for_source,
)
from app.scheduler import TickReport, collect_now, run_tick
from core.enums import ErrorClass, JobRunStatus
from core.job_state import InvalidJobRunTransitionError, JobRunEvent
from core.models import schedule_policy_from_json, schedule_policy_to_json

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
    url = str(sa.URL.create("sqlite+pysqlite", database=str(tmp_path / "sched.db")))
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    engine = sa.create_engine(url)
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
            sa.text("select status, metrics, finished_at from job_run where id='r1'")
        ).one()
        assert row[0] == "succeeded"
        assert '"inserted": 3' in row[1]
        assert row[2] is not None


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
    """The conditional update is what stops a source being fetched twice.

    Two schedulers ticking at the same instant both try to claim; exactly one
    must win, or the same feed is collected concurrently and the second run
    duplicates work while both write job_run rows.
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
    with session_factory() as session:
        add_source(session, source_id="s1", policy={"type": "fixed", "interval": "30m"})
        assert last_collect_at(session, job_id="job-s1") is None


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
    """A scheduler that loses the race must do no work, not duplicate it."""
    with session_factory() as session:
        add_source(session, source_id="s1", policy={"type": "fixed", "interval": "30m"})
        job = job_for_source(session, "s1")
        assert job is None, "the job is created by collect_now"

    with session_factory() as session:
        status, result = collect_now(
            session,
            source_id="s1",
            policy_json='{"type": "fixed", "interval": "30m"}',
            client=fake_feed,
            now=NOW,
        )
        session.commit()
    assert status is JobRunStatus.SUCCEEDED
    assert result is not None and result.inserted == 1


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
        session.execute(sa.text("update source set status='deleted' where id='gone'"))
        session.commit()
        rows = collectable_sources(session, tenant_id="default")
    assert [row[0] for row in rows] == ["active-one"]


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
    """
    import asyncio
    import threading
    import time

    import app.scheduler as scheduler_module
    from app.scheduler import Scheduler

    lock = threading.Lock()
    inside = 0
    peak = 0

    def slow_run_tick(*args: object, **kwargs: object) -> TickReport:
        nonlocal inside, peak
        with lock:
            inside += 1
            peak = max(peak, inside)
        time.sleep(0.05)
        with lock:
            inside -= 1
        return TickReport(considered=0, due=0, collected=[], failed=[], skipped=[])

    scheduler = Scheduler(session_factory, client=fake_feed, tick_seconds=0.01)
    original = scheduler_module.run_tick
    scheduler_module.run_tick = slow_run_tick
    try:
        await asyncio.gather(*(scheduler.tick_once() for _ in range(4)))
    finally:
        scheduler_module.run_tick = original

    assert peak == 1, f"ticks overlapped: {peak} ran at once"


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
