"""User recovery after temporary SQLite outages, with a controlled poll timer."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from adapters.audit import record
from adapters.db_models import AuditLog
from app.enrichment_worker import EnrichmentWorker
from app.scheduler import Scheduler
from core.enums import AuditOutcome
from tests.migrated_schema import migrated_database

pytestmark = pytest.mark.integration


async def _explode() -> None:
    """A background task body that dies of a programming error."""
    raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_user_scheduler_recovers_after_storage_unlocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Given locked storage, when a later poll succeeds, expired audits disappear.

    Integration-level timing controls isolate enumeration failure from startup
    reaping. The rollback journal lets an exclusive lock block actual reads;
    this does not simulate production WAL read concurrency or API readiness.
    """
    url = migrated_database(tmp_path, "recovery.db")
    engine = sa.create_engine(url, connect_args={"timeout": 0})
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        record(
            session,
            audit_id="expired",
            actor="user",
            operation="GET /v1/items",
            outcome=AuditOutcome.SUCCEEDED,
            correlation_id="recovery",
            occurred_at=datetime(2020, 1, 1, tzinfo=UTC),
        )
        session.commit()
    lock = sqlite3.connect(tmp_path / "recovery.db", timeout=0)
    waits = iter(("lock", "unlock", "finish"))

    async def poll(seconds: float) -> None:
        assert seconds == 60
        phase = next(waits)
        if phase == "lock":
            lock.execute("BEGIN EXCLUSIVE")
        elif phase == "unlock":
            lock.rollback()
        else:
            raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", poll)
    monkeypatch.setenv("PEARSS_AUDIT_RETENTION_DAYS", "30")
    try:
        with httpx.Client() as client:
            worker = Scheduler(factory, client=client, monotonic=lambda: 1000)
            with pytest.raises(asyncio.CancelledError):
                await worker._loop()
        with factory() as session:
            assert list(session.scalars(sa.select(AuditLog.id))) == []
    finally:
        lock.rollback()
        lock.close()
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("maintenance", ["reap", "prune"])
async def test_user_failed_maintenance_retries_without_waiting_another_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maintenance: str
) -> None:
    """Given a failed write, when storage unlocks, due maintenance still runs.

    Drive maintenance directly to isolate its timer from the scheduler poll.
    Both attempts use the same monotonic instant and real migrated SQLite.
    """
    from sqlalchemy.exc import OperationalError

    from adapters.db_models import JobRun
    from adapters.repository import (
        claim_job_run,
        create_job_run,
        create_source,
        ensure_collect_job,
    )

    url = migrated_database(tmp_path, "maintenance-retry.db")
    engine = sa.create_engine(url, connect_args={"timeout": 0})
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setenv("PEARSS_AUDIT_RETENTION_DAYS", "30")
    claimed = "2020-01-01T00:00:00.000000Z"
    try:
        with factory() as session:
            source = create_source(
                session,
                source_type="rss",
                url="https://maintenance.example/feed",
                canonical_url="https://maintenance.example/feed",
                schedule_policy='{"type":"fixed","interval":"30m"}',
                tenant_id="default",
                at=claimed,
            )
            job_id = ensure_collect_job(
                session, source_id=source.id, tenant_id="default", at=claimed
            )
            create_job_run(
                session,
                run_id="abandoned",
                job_id=job_id,
                correlation_id="maintenance",
                at=claimed,
            )
            claim_job_run(session, run_id="abandoned", at=claimed)
            for audit_id, year in (("expired", 2020), ("retained", 2099)):
                record(
                    session,
                    audit_id=audit_id,
                    actor="user",
                    operation="GET /v1/items",
                    outcome=AuditOutcome.SUCCEEDED,
                    correlation_id="maintenance",
                    occurred_at=datetime(year, 1, 1, tzinfo=UTC),
                )
            session.commit()
        with httpx.Client() as client:
            worker = Scheduler(factory, client=client, monotonic=lambda: 1000)
            maintain = (
                worker._reap_orphaned_runs_if_due
                if maintenance == "reap"
                else worker._prune_audit_if_due
            )
            lock = sqlite3.connect(tmp_path / "maintenance-retry.db", timeout=0)
            try:
                lock.execute("BEGIN IMMEDIATE")
                with pytest.raises(OperationalError, match="database is locked"):
                    await maintain()
                lock.rollback()
                with factory() as session:
                    assert session.scalar(sa.select(JobRun.status)) == "running"
                    assert sorted(session.scalars(sa.select(AuditLog.id))) == [
                        "expired",
                        "retained",
                    ]
                await maintain()
            finally:
                lock.rollback()
                lock.close()
        with factory() as session:
            assert session.scalar(sa.select(JobRun.status)) == (
                "timed_out" if maintenance == "reap" else "running"
            )
            assert sorted(session.scalars(sa.select(AuditLog.id))) == (
                ["expired", "retained"] if maintenance == "reap" else ["retained"]
            )
    finally:
        engine.dispose()


async def _replace_with_failed_task(worker: Scheduler | EnrichmentWorker) -> None:
    live = worker._task
    assert live is not None
    live.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await live
    worker._task = asyncio.create_task(_explode())
    with pytest.raises(RuntimeError, match="boom"):
        await worker._task


@pytest.mark.asyncio
@pytest.mark.parametrize("worker_kind", ["scheduler", "enrichment"])
async def test_user_stop_drains_a_task_that_died_of_an_error(
    tmp_path: Path,
    worker_kind: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Given a background task ended by a programming error, stop completes.

    Shutdown must survive a dead worker: the sequential lifespan stops the
    scheduler, the enrichment worker and plugin children in order, and one
    stored exception breaking that chain would leave later resources running.
    After stop, the task reference is cleared so a restart is possible.

    The log assertions read `caplog.records`, not captured stderr. They used to
    read stderr and passed only because `migrated_database` ran `alembic`
    in-process, and `migrations/env.py` calls `logging.config.fileConfig`,
    which installs a root handler pointing at whatever `sys.stderr` is at that
    moment -- here, this test's capture stream. Once the schema is built once
    per session instead of once per test (issue #44), that handler belongs to
    an earlier test and the record goes to a closed stream. Asserting on the
    records is what the repo does elsewhere and does not depend on which test
    happened to reconfigure logging first.
    """
    from app.enrichment_worker import EnrichmentWorker
    from app.scheduler import Scheduler

    engine = sa.create_engine(migrated_database(tmp_path, "stop-drain.db"))
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        with caplog.at_level(logging.ERROR), httpx.Client() as client:
            worker: Scheduler | EnrichmentWorker
            if worker_kind == "scheduler":
                worker = Scheduler(factory, client=client, tick_seconds=3600)
            else:
                worker = EnrichmentWorker(factory, processors={}, sweep_seconds=3600)
            try:
                await worker.start()
                await _replace_with_failed_task(worker)
                await worker.stop()
                assert worker._task is None
                assert worker.liveness() == (True, ""), (
                    "a stopped worker must not report itself degraded"
                )

                # A cleared task reference is what makes a restart possible.
                # Checked through `liveness()` rather than `_task is not None`:
                # the latter is the private reference, and mypy narrows it to
                # None from the assert above, so it would read as unreachable.
                # The public signal is also the one operators see.
                await worker.start()
                assert worker.liveness() == (True, worker_kind), (
                    "the worker did not come back up after a task died"
                )
            finally:
                await worker.stop()

    finally:
        engine.dispose()

    failures = [
        record for record in caplog.records if "task failed" in record.getMessage()
    ]
    assert len(failures) == 1, (
        f"expected exactly one failure log for {worker_kind}, got "
        f"{[r.getMessage() for r in failures]}"
    )
    reported = failures[0].getMessage()
    assert reported == f"{worker_kind} task failed (RuntimeError)", reported
    # The exception's own message and traceback must not reach the log: this
    # line exists to report that shutdown survived, not to dump the failure.
    assert "boom" not in reported, reported
    assert failures[0].exc_info is None, (
        "the failure log carries exc_info, so the traceback is written out; "
        "only the exception's type name belongs here"
    )
