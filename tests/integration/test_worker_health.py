"""Operational health must name background-worker liveness, not just process liveness.

`/healthz` returned `{"status": "ok"}` unconditionally: the HTTP process was
alive even when the scheduler or enrichment loop had died of a programming
error, and an operator watching the probe saw green while collection silently
stopped (problem review A07: "expose worker liveness/failure in operational
health"). These scenarios drive the real app and real worker objects.

Given/When/Then, user voice: the user is the operator whose monitoring hits
`/healthz`. The lifespan is entered *inside each test* (the same task), not
in a fixture: anyio forbids exiting a cancel scope from another task.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.orm import sessionmaker

from app.main import create_app, create_engine_for
from tests.migrated_schema import migrated_database

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    url = migrated_database(tmp_path, "a07.db")
    monkeypatch.setenv("PEARSS_DATABASE_URL", url)
    monkeypatch.setenv("PEARSS_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("PEARSS_SCHEDULER_ENABLED", "1")
    monkeypatch.setenv("PEARSS_TICK_SECONDS", "3600")
    return create_app(sessionmaker(create_engine_for(url), expire_on_commit=False))


@pytest_asyncio.fixture
async def served(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient]]:
    application = _app(tmp_path, monkeypatch)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as opened:
        yield application, opened
    application.state.http_client.close()


async def test_healthz_reports_scheduler_death_not_green(
    served: tuple[FastAPI, httpx.AsyncClient],
) -> None:
    """Given a running service, when the scheduler task dies of an escaped
    error, then /healthz reports the degradation instead of a blanket ok.
    """
    app, client = served

    # ASGITransport does not run the lifespan, and the cancel scope must be
    # entered and exited by this test's task -- so it opens here, inside the
    # same task that will answer the requests.
    async with app.router.lifespan_context(app):
        before = await client.get("/healthz")
        assert before.status_code == 200
        assert before.json() == {"status": "ok"}, before.json()

        # The escaped-error shape from A07: the task object still exists but
        # the loop is dead. Health must distinguish this from a live loop.
        task = app.state.scheduler._task
        assert task is not None and not task.done()
        task.cancel()
        with contextlib.suppress(BaseException):
            await task

        after = await client.get("/healthz")
        assert after.status_code == 503, after.status_code
        assert after.json()["status"] == "degraded"
        assert "scheduler" in after.json()["details"]


async def test_healthz_returns_to_ok_after_worker_restart(
    served: tuple[FastAPI, httpx.AsyncClient],
) -> None:
    """Given a degraded worker, when it starts again, then health is ok."""
    app, client = served

    async with app.router.lifespan_context(app):
        task = app.state.scheduler._task
        assert task is not None
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        assert (await client.get("/healthz")).status_code == 503

        await app.state.scheduler.start()
        assert (await client.get("/healthz")).status_code == 200
