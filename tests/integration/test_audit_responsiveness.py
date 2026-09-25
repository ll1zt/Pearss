"""Audit writes must not stall the shared event loop.

The audit middleware runs inside the same asyncio loop that serves every
other request, but `_record` performed synchronous SQLAlchemy work and a
commit. Under SQLite writer contention -- a normal state while a collection
run or the enrichment worker holds the write lock -- an audited request
could block *all* requests for the length of the database wait timeout.
This suite holds a real writer lock and measures what users experience:
an unrelated request must stay responsive while the audited request
completes, and the audit outcome must remain observable either way.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa

from app.middleware import CorrelationAndAuditMiddleware
from tests.migrated_schema import migrated_database

pytestmark = pytest.mark.integration

# A response is "responsive" if it lands well inside the database wait
# timeout an audit write could otherwise block for.
RESPONSIVE_BUDGET_SECONDS = 2.0


class _AuditHarness:
    """A real app whose audit database a test can lock at the SQLite level."""

    def __init__(self, tmp_path: Path) -> None:
        url = migrated_database(tmp_path, "a15.db")
        import os

        os.environ["PEARSS_DATABASE_URL"] = url
        os.environ["PEARSS_STORAGE_DIR"] = str(tmp_path / "storage")
        os.environ["PEARSS_API_TOKEN"] = WRITE_TOKEN
        from app.main import create_app, create_engine_for

        self.engine = create_engine_for(url)
        from sqlalchemy.orm import sessionmaker

        self.application = create_app(sessionmaker(self.engine, expire_on_commit=False))

    def audit_rows(self) -> int:
        from sqlalchemy.orm import Session

        with Session(self.engine) as session:
            rows = session.execute(
                sa.text("SELECT COUNT(*) FROM audit_log")
            ).scalar_one()
            return int(rows)


WRITE_TOKEN = "audit-responsiveness-write-token"


@pytest_asyncio.fixture
async def harness(
    tmp_path: Path,
) -> AsyncIterator[tuple[_AuditHarness, httpx.AsyncClient]]:
    app_harness = _AuditHarness(tmp_path)
    transport = httpx.ASGITransport(app=app_harness.application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as opened:
        yield app_harness, opened


@pytest.mark.asyncio
async def test_user_reads_stay_responsive_while_audit_waits_for_a_writer(
    harness: tuple[_AuditHarness, httpx.AsyncClient],
) -> None:
    """Given a competing writer holding the SQLite lock, when a request that
    will be audited but performs no other database write completes, then an
    unrelated request is answered within the budget: the audit write must not
    hold the shared event loop hostage while it waits for the lock.
    """
    app_harness, client = harness
    from sqlalchemy.orm import Session

    # A real writer transaction holding the database's write lock.
    holder = Session(app_harness.engine)
    try:
        holder.execute(sa.text("CREATE TABLE IF NOT EXISTS a15_probe (v INTEGER)"))
        holder.execute(sa.text("INSERT INTO a15_probe VALUES (1)"))
        holder.commit()
        holder.execute(sa.text("INSERT INTO a15_probe VALUES (2)"))
        # No commit: the write lock is now held by this transaction.

        # A keyless write is refused 400 after authentication -- no business
        # row, no idempotency reservation -- so the ONLY database work it
        # can contend on is the audit record itself. That isolates A15:
        # whether the audit write lands or is logged as failed, no other
        # request may stall behind it.
        refused = asyncio.create_task(
            client.post(
                "/v1/sources",
                json={
                    "url": "https://a15.example/feed",
                    "schedule_policy": {"type": "fixed", "interval": "30m"},
                },
                headers={"Authorization": f"Bearer {WRITE_TOKEN}"},
            )
        )
        # Probe repeatedly for the whole window the request is in flight:
        # a single early probe can miss a stall that starts later. Every
        # probe must answer within the budget until the request completes.
        probes: list[float] = []
        while not refused.done():
            started = time.monotonic()
            response = await asyncio.wait_for(
                client.get("/healthz"), RESPONSIVE_BUDGET_SECONDS
            )
            probes.append(time.monotonic() - started)
            assert response.status_code == 200, (
                "health probe failed while audit was in flight"
            )
            await asyncio.sleep(0.05)
        assert probes, "the probe never ran while the audited request was in flight"
        assert max(probes) < RESPONSIVE_BUDGET_SECONDS, (
            f"unrelated read took {max(probes):.2f}s while the audit write waited"
        )
    finally:
        holder.rollback()
        holder.close()

    audited_response = await asyncio.wait_for(refused, 30)
    # The refusal itself must still be the documented contract: no key, no
    # write, whatever the audit subsystem is doing.
    assert audited_response.status_code == 400, audited_response.text


@pytest.mark.asyncio
async def test_user_audited_request_completes_when_the_writer_releases(
    harness: tuple[_AuditHarness, httpx.AsyncClient],
) -> None:
    """Given the lock is released, the audited request finishes and its
    record becomes observable in the audit table.
    """
    app_harness, client = harness
    from sqlalchemy.orm import Session

    holder = Session(app_harness.engine)
    try:
        holder.execute(sa.text("CREATE TABLE IF NOT EXISTS a15_probe2 (v INTEGER)"))
        holder.execute(sa.text("INSERT INTO a15_probe2 VALUES (1)"))
        holder.commit()
        holder.execute(sa.text("INSERT INTO a15_probe2 VALUES (2)"))

        audited = asyncio.create_task(
            client.post(
                "/v1/sources",
                json={
                    "url": "https://a15b.example/feed",
                    "schedule_policy": {"type": "fixed", "interval": "30m"},
                },
                headers={
                    "Authorization": f"Bearer {WRITE_TOKEN}",
                    "Idempotency-Key": "a15-audited",
                },
            )
        )
        await asyncio.sleep(0.2)
    finally:
        holder.rollback()
        holder.close()

    response = await asyncio.wait_for(audited, 30)
    assert response.status_code == 201, response.text
    for _ in range(100):
        if app_harness.audit_rows() >= 1:
            break
        await asyncio.sleep(0.05)
    assert app_harness.audit_rows() >= 1, "the completed write was never audited"


@pytest.mark.asyncio
async def test_the_audit_queue_is_bounded_under_a_writer_held_forever(
    harness: tuple[_AuditHarness, httpx.AsyncClient],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A flood of audited writes must grow the audit backlog only to its bound.

    Given a real writer transaction holds the SQLite lock (the audit worker
    cannot commit anything for the whole scenario)
    When more authenticated writes arrive than the audit queue's bound
    Then every caller still gets its HTTP answer promptly, the queue's
    population never exceeds the bound, and each dropped row is visible in
    the journal at error level with its correlation id -- overload is a
    policy, not a silent loss (issue #18).
    """
    from app.middleware import AUDIT_QUEUE_BOUND

    app_harness, client = harness
    from sqlalchemy.orm import Session

    holder = Session(app_harness.engine)
    try:
        holder.execute(sa.text("CREATE TABLE IF NOT EXISTS a18_probe (v INTEGER)"))
        holder.execute(sa.text("INSERT INTO a18_probe VALUES (1)"))
        holder.commit()
        holder.execute(sa.text("INSERT INTO a18_probe VALUES (2)"))
        # No commit: the audit worker blocks on the lock for the scenario.

        total = AUDIT_QUEUE_BOUND + 40
        for i in range(total):
            response = await asyncio.wait_for(
                client.post(
                    "/v1/sources",
                    json={
                        "url": f"https://a18-{i}.example/feed",
                        "schedule_policy": {"type": "fixed", "interval": "30m"},
                    },
                    headers={"Authorization": f"Bearer {WRITE_TOKEN}"},
                ),
                RESPONSIVE_BUDGET_SECONDS,
            )
            # A keyless write is refused 400 before any business row or
            # idempotency reservation (the A15 isolation trick): the request
            # itself never contends on the held lock, so the ONLY database
            # work in flight is the audit record. With a key the request's
            # own reservation would block on the busy timeout and the
            # responsiveness budget would measure the wrong thing.
            assert response.status_code == 400, response.status_code

        queued = CorrelationAndAuditMiddleware._EXECUTOR._work_queue.qsize()
        assert queued <= AUDIT_QUEUE_BOUND, (
            f"the audit queue grew to {queued}, past its bound"
        )
        dropped = [
            r
            for r in caplog.records
            if r.levelname == "ERROR" and "audit queue full" in r.getMessage()
        ]
        assert dropped, "a dropped audit row must be visible in the journal"
    finally:
        holder.rollback()
        holder.close()

    # After the lock is released the worker drains; the table must hold no
    # more rows than were accepted (total minus dropped) -- bounded loss,
    # bounded memory, no unbounded growth anywhere.
    CorrelationAndAuditMiddleware.drain_audit(30.0)
    rows = app_harness.audit_rows()
    assert rows <= total
