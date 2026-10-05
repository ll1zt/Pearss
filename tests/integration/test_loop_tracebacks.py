"""Scenario register (normative source: SCENARIOS.md).

AC-278..279: when a background loop's task dies of an escaped exception, the
shutdown path that reports it keeps the message and the traceback; a normal
cancellation stays quiet.

The three loops have different sweep boundaries (the scheduler and enrichment
worker let a non-storage error escape, the work worker catches it and keeps
sweeping), so the "it already died" state is constructed directly: a real
``asyncio.Task`` that raised. What is under test is the ``stop()`` failure
path -- the code that reports a dead loop -- not the loop bodies.
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from collections.abc import Iterator
from typing import Protocol

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.integration

# A message nothing else in the suite emits, so a match proves this exception
# travelled from the dead task into the journal.
_DEATH_MESSAGE = "loop died with a unique message 6b1d4f"


class _Stoppable(Protocol):
    _task: asyncio.Task[None] | None

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


@pytest.fixture
def client() -> Iterator[httpx.Client]:
    """A client the loops are constructed with; the failure path never fetches."""
    with httpx.Client() as opened:
        yield opened


async def _kill_the_task(loop: _Stoppable) -> None:
    """Put `loop` in the state of a task that died of an escaped error."""

    async def die() -> None:
        raise KeyError(_DEATH_MESSAGE)

    loop._task = asyncio.create_task(die())
    # Let the scheduled task run to completion (with its exception) before the
    # caller drives stop().
    await asyncio.sleep(0)


def _assert_the_traceback_survived(caplog: pytest.LogCaptureFixture) -> None:
    errors = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert errors, "the loop's death was not reported at error level"
    assert any(record.exc_info for record in errors), (
        "the loop's death was reported without exc_info, so message and call "
        "site are lost"
    )
    rendered = "".join(
        "".join(traceback.format_exception(*record.exc_info))
        for record in errors
        if record.exc_info
    )
    assert _DEATH_MESSAGE in rendered
    assert "Traceback" in rendered


@pytest.mark.asyncio
async def test_a_scheduler_that_died_keeps_its_traceback(
    caplog: pytest.LogCaptureFixture,
    session_factory: sessionmaker[Session],
    client: httpx.Client,
) -> None:
    """AC-278: /healthz says degraded; the journal must say why."""
    from app.scheduler import Scheduler

    scheduler = Scheduler(session_factory, client=client)
    await _kill_the_task(scheduler)

    with caplog.at_level(logging.DEBUG, logger="app.scheduler"):
        await scheduler.stop()

    assert "scheduler task failed" in caplog.text
    _assert_the_traceback_survived(caplog)


@pytest.mark.asyncio
async def test_a_work_loop_that_died_keeps_its_traceback(
    caplog: pytest.LogCaptureFixture,
    session_factory: sessionmaker[Session],
    client: httpx.Client,
) -> None:
    """AC-278: the work loop's shutdown path reports the same way."""
    from app.work_worker import WorkWorker

    worker = WorkWorker(session_factory, client=client)
    await _kill_the_task(worker)

    with caplog.at_level(logging.DEBUG, logger="app.work_worker"):
        await worker.stop()

    assert "work task failed" in caplog.text
    _assert_the_traceback_survived(caplog)


@pytest.mark.asyncio
async def test_an_enrichment_loop_that_died_keeps_its_traceback(
    caplog: pytest.LogCaptureFixture,
    session_factory: sessionmaker[Session],
) -> None:
    """AC-278: the enrichment worker's shutdown path reports the same way."""
    from app.enrichment_worker import EnrichmentWorker

    worker = EnrichmentWorker(session_factory, processors={})
    await _kill_the_task(worker)

    with caplog.at_level(logging.DEBUG, logger="app.enrichment_worker"):
        await worker.stop()

    assert "enrichment task failed" in caplog.text
    _assert_the_traceback_survived(caplog)


@pytest.mark.asyncio
async def test_a_normally_stopped_loop_logs_no_traceback(
    caplog: pytest.LogCaptureFixture,
    session_factory: sessionmaker[Session],
    client: httpx.Client,
) -> None:
    """AC-279: cancelling a running loop is a normal path, not an incident."""
    from app.enrichment_worker import EnrichmentWorker
    from app.scheduler import Scheduler
    from app.work_worker import WorkWorker

    loops: list[_Stoppable] = [
        Scheduler(session_factory, client=client, tick_seconds=3600),
        WorkWorker(session_factory, client=client, sweep_seconds=3600),
        EnrichmentWorker(session_factory, processors={}, sweep_seconds=3600),
    ]
    with caplog.at_level(logging.DEBUG):
        for loop in loops:
            await loop.start()
        for loop in loops:
            await loop.stop()

    noisy = [
        record
        for record in caplog.records
        if record.levelno >= logging.ERROR or record.exc_info
    ]
    assert not noisy, (
        "a normal stop must not look like a crash; got: "
        f"{[record.getMessage() for record in noisy]}"
    )
