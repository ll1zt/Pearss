"""Resource ownership when real application startup is rejected by storage."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.main import create_app
from tests.integration.conftest import migrated_database

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.mark.parametrize(
    "deny_jobs", [True, False], ids=["startup-fails", "normal-stop"]
)
async def test_user_service_exit_closes_its_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, deny_jobs: bool
) -> None:
    """Given storage permissions, when startup fails or service stops, resources close.

    Drive the real ASGI lifespan directly to observe resources before test
    teardown can hide leaks. SQLite's authorizer rejects job_run access;
    application services and plugin children are not mocked.
    """
    monkeypatch.setenv("PEARSS_SCHEDULER_ENABLED", "1")
    monkeypatch.setenv("PEARSS_TICK_SECONDS", "3600")
    monkeypatch.setenv("PEARSS_PLUGIN_DIR", str(tmp_path / "plugins"))
    engine = sa.create_engine(migrated_database(tmp_path, "lifespan.db"))

    def authorize(
        action: int,
        table: str | None,
        column: str | None,
        database: str | None,
        trigger: str | None,
    ) -> int:
        if deny_jobs and table == "job_run":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    @sa.event.listens_for(engine, "connect")
    def restrict_jobs(connection: sqlite3.Connection, record: object) -> None:
        connection.set_authorizer(authorize)

    app = create_app(sessionmaker(engine, expire_on_commit=False))
    try:
        if deny_jobs:
            with pytest.RaisesGroup(sa.exc.DatabaseError, flatten_subgroups=True):
                async with app.router.lifespan_context(app):
                    pytest.fail("startup must reject denied job storage")
        else:
            async with app.router.lifespan_context(app):
                assert app.state.http_client.is_closed is False
        assert app.state.http_client.is_closed is True
        assert [
            task.get_name()
            for task in asyncio.all_tasks()
            if task.get_name() in {"pearss-scheduler", "pearss-enrichment"}
            and not task.done()
        ] == []
    finally:
        # Preserve test isolation even when the resource assertion fails.
        if hasattr(app.state, "scheduler"):
            await app.state.scheduler.stop()
        if hasattr(app.state, "enrichment_worker"):
            await app.state.enrichment_worker.stop()
        if hasattr(app.state, "http_client"):
            app.state.http_client.close()
        engine.dispose()
