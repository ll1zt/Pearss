"""Scenario register (normative source: SCENARIOS.md).

AC-290..292: the INFO volume does not grow with idle polling or with batch size
(issue #297's rule: INFO/WARNING is event-driven and O(1) per job or failure).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from app.main import create_engine_for
from app.pipeline import normalise_and_store
from app.scheduler import run_tick
from core.models import ItemDraft, schedule_policy_from_json, schedule_policy_to_json
from tests.migrated_schema import migrated_database

pytestmark = pytest.mark.integration

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
SOURCE = "src-1"


def _add_source(session: Session, *, source_id: str, policy: dict[str, object]) -> None:
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


def _draft(index: int) -> ItemDraft:
    return ItemDraft(
        source_id=SOURCE,
        external_id=f"e{index}",
        title=f"Post {index}",
        canonical_url=f"https://example.com/{index}",
        published_at=NOW,
        content=f"<p>body {index}</p>",
    )


def _feed_client() -> httpx.Client:
    body = (
        b'<?xml version="1.0"?><rss version="2.0"><channel><title>F</title>'
        b"<item><title>Live</title><link>https://live.example.com/1</link>"
        b"<guid>live-1</guid>"
        b"<pubDate>Mon, 01 Jun 2026 11:00:00 +0000</pubDate>"
        b"<description>Body.</description></item></channel></rss>"
    )
    return httpx.Client(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, content=body))
    )


def _dead_client() -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(
            lambda _r: httpx.Response(599, text="no request belongs to this test")
        )
    )


def _info_records(caplog: pytest.LogCaptureFixture, logger: str) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == logger and record.levelno >= logging.INFO
    ]


# The two modules a batch of drafts goes through, so "INFO volume of storing a
# batch" is the sum rather than one module's share of it.
_STORE_PATH_LOGGERS = frozenset({"app.pipeline", "adapters.repository.items"})


def _store_path_info_records(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name in _STORE_PATH_LOGGERS and record.levelno >= logging.INFO
    ]


def test_ten_idle_ticks_add_no_info_lines(
    session_factory: sessionmaker[Session], caplog: pytest.LogCaptureFixture
) -> None:
    """AC-290: an idling scheduler is not an INFO stream."""
    with session_factory() as session:
        _add_source(
            session, source_id=SOURCE, policy={"type": "fixed", "interval": "1h"}
        )

    client = _feed_client()
    try:
        # One collection, so the ten that follow are genuinely idle.
        run_tick(session_factory, client=client, now=NOW)

        caplog.clear()
        with caplog.at_level(logging.INFO, logger="app.scheduler"):
            for index in range(10):
                run_tick(
                    session_factory,
                    client=client,
                    now=NOW + timedelta(minutes=index + 1),
                )

        assert _info_records(caplog, "app.scheduler") == []
    finally:
        client.close()


@pytest.mark.asyncio
async def test_ten_empty_sweeps_add_no_info_lines(
    session_factory: sessionmaker[Session], caplog: pytest.LogCaptureFixture
) -> None:
    """AC-290: an empty work queue is not an INFO stream either."""
    from app.work_worker import WorkWorker

    client = _dead_client()
    try:
        worker = WorkWorker(session_factory, client=client)
        with caplog.at_level(logging.INFO, logger="app.work_worker"):
            for _ in range(10):
                await worker.sweep_once()

        assert _info_records(caplog, "app.work_worker") == []
    finally:
        client.close()


def test_the_info_count_does_not_grow_with_the_batch(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """AC-292: storing 1 draft and 50 drafts produce the same INFO volume.

    The second pass over the same drafts is all dedupe, which is where a
    per-item INFO line would show up as growth.
    """
    counts: list[int] = []
    for label, size in (("one", 1), ("fifty", 50)):
        engine = create_engine_for(migrated_database(tmp_path, f"{label}.db"))
        try:
            with sessionmaker(engine, expire_on_commit=False)() as session:
                _add_source(
                    session,
                    source_id=SOURCE,
                    policy={"type": "fixed", "interval": "1h"},
                )
                for _pass in range(2):
                    caplog.clear()
                    with caplog.at_level(logging.INFO):
                        normalise_and_store(session, [_draft(i) for i in range(size)])
                    counts.append(len(_store_path_info_records(caplog)))
        finally:
            engine.dispose()

    assert counts == [1, 1, 1, 1], counts
