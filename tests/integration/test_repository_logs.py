"""Scenario register (normative source: SCENARIOS.md).

AC-288..289: a work's claim and finish are each one INFO line; an empty sweep
writes nothing at INFO (a "no work" line exists at DEBUG), so the worker's
idle polling cannot become an INFO stream.
"""

from __future__ import annotations

import logging

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from adapters.repository import create_work
from tests.integration.conftest import sweep_until_terminal

pytestmark = pytest.mark.integration


def _client() -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(599, text="no request belongs to this test")
        )
    )


@pytest.mark.asyncio
async def test_a_work_claim_and_finish_are_each_logged(
    session_factory: sessionmaker[Session], caplog: pytest.LogCaptureFixture
) -> None:
    """AC-288: the claim names the work; the finish names its counts."""
    with session_factory() as session:
        work = create_work(
            session,
            kind="fetch",
            targets=["https://a.example/1"],
            params={"subscription": "missing"},
            fingerprint="fp-repository-logs",
            tenant_id="default",
        )
        session.commit()
        work_id = work.id

    with caplog.at_level(logging.INFO):
        await sweep_until_terminal(session_factory, work_id)

    messages = [record.getMessage() for record in caplog.records]
    assert any(f"claimed work {work_id}" in message for message in messages), messages
    finished = [m for m in messages if f"work {work_id} finished" in m]
    assert len(finished) == 1, messages
    assert "ok" in finished[0] and "failed" in finished[0]


@pytest.mark.asyncio
async def test_an_empty_sweep_is_silent_at_info_and_visible_at_debug(
    session_factory: sessionmaker[Session], caplog: pytest.LogCaptureFixture
) -> None:
    """AC-289: ten empty sweeps add no INFO line; DEBUG says why."""
    from app.work_worker import WorkWorker

    client = _client()
    try:
        worker = WorkWorker(session_factory, client=client)
        with caplog.at_level(logging.INFO, logger="app.work_worker"):
            for _ in range(10):
                await worker.sweep_once()
        assert [
            record
            for record in caplog.records
            if record.name == "app.work_worker" and record.levelno >= logging.INFO
        ] == []

        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="app.work_worker"):
            await worker.sweep_once()
        assert any(
            "no work to claim" in record.getMessage() for record in caplog.records
        )
    finally:
        client.close()
