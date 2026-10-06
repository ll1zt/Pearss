"""Scenario register (normative source: SCENARIOS.md).

AC-306..307: a background job's log lines group under one correlation id, and
two jobs get different ones. `correlation.current()` generates an id when none
is set and does not store it, so before this every background line carried an
id of its own -- which is the same as carrying none at all (issue #318).

The id is read the way production reads it: through the handler's
`CorrelationIdFilter`, so what is asserted is the rendered contract rather than
"some field was set".
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from adapters.timestamps import fixed_width_utc as timestamp
from app import log_setup
from app.main import create_engine_for
from app.scheduler import run_tick
from tests.migrated_schema import migrated_database
from tests.source_seed import mirror_sources

pytestmark = pytest.mark.integration

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
SCHEDULER = "app.scheduler"


class _IdCollector(logging.Handler):
    """Keeps records with the production filter applied, so ids are stamped."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []
        self.addFilter(log_setup.CorrelationIdFilter())

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def scheduler_ids(self) -> set[str | None]:
        return {
            getattr(record, "correlation_id", None)
            for record in self.records
            if record.name == SCHEDULER
        }

    def scheduler_messages(self) -> list[str]:
        return [
            record.getMessage() for record in self.records if record.name == SCHEDULER
        ]


@pytest.fixture
def collector() -> Iterator[_IdCollector]:
    """A root handler with the production filter, at DEBUG.

    The root level is part of the fixture because DEBUG is what the idle tick's
    heartbeat is (AC-293), and this test needs a second line from a second
    thread to prove grouping rather than a single line that trivially matches
    itself.
    """
    handler = _IdCollector()
    root = logging.getLogger()
    previous = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)


@pytest.fixture
def session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine_for(migrated_database(tmp_path, "job-correlation.db"))
    try:
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        engine.dispose()


def _add_source(session: Session, *, source_id: str) -> None:
    session.execute(
        sa.text(
            "insert into source (id, type, url, canonical_url, schedule_policy,"
            " status, trust_level, tenant_id, created_at, updated_at)"
            ' values (:id, \'rss\', :url, :url, \'{"type":"fixed","interval":"60s"}\','
            " 'active', 'standard', 'default', :now, :now)"
        ),
        {
            "id": source_id,
            "url": f"https://{source_id}.example.com/feed",
            "now": timestamp(NOW),
        },
    )
    mirror_sources(session)
    session.commit()


def _failing_client() -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(403)))


def test_one_tick_groups_every_line_under_a_single_id(
    session_factory: sessionmaker[Session], collector: _IdCollector
) -> None:
    """AC-306: a tick is one job, so its lines carry one id, not one each."""
    with session_factory() as session:
        _add_source(session, source_id="src-correlation-a")

    client = _failing_client()
    try:
        # First tick collects and fails; the second finds the source in backoff,
        # so it writes the failing line (pool thread) *and* the idle heartbeat
        # (calling thread) -- two lines from two threads, one job.
        run_tick(session_factory, client=client, now=NOW)
        collector.records.clear()
        run_tick(session_factory, client=client, now=NOW + timedelta(seconds=30))
    finally:
        client.close()

    messages = collector.scheduler_messages()
    assert len(messages) >= 2, messages
    ids = collector.scheduler_ids()
    assert len(ids) == 1, ids
    assert ids != {None}


def test_two_ticks_get_different_ids(
    session_factory: sessionmaker[Session], collector: _IdCollector
) -> None:
    """AC-307: the id identifies the job, so two jobs must not share one."""
    with session_factory() as session:
        _add_source(session, source_id="src-correlation-b")

    client = _failing_client()
    try:
        run_tick(session_factory, client=client, now=NOW)
        first = collector.scheduler_ids()
        collector.records.clear()
        run_tick(session_factory, client=client, now=NOW + timedelta(seconds=30))
        second = collector.scheduler_ids()
    finally:
        client.close()

    assert first and second, (first, second)
    assert first.isdisjoint(second), (first, second)
