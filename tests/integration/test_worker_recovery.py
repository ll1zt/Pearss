"""User recovery after temporary SQLite outages, with a controlled poll timer."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from adapters.audit import record
from adapters.db_models import AuditLog
from app.scheduler import Scheduler
from core.enums import AuditOutcome
from tests.integration.conftest import migrated_database

pytestmark = pytest.mark.integration


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
