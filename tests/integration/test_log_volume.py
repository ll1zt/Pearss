"""Scenario register (normative source: SCENARIOS.md).

AC-290..292: the INFO volume does not grow with idle polling or with batch size
(issue #297's rule: INFO/WARNING is event-driven and O(1) per job or failure).
AC-299: the same rule on the chain path -- an item the chain itself drops, because
its text is already in the target language, is not a per-item event either.
AC-300: the same rule for a source that keeps failing -- "still in backoff" is one
state, not one event per tick.
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
from core.enrichment import EnrichmentKind, EnrichmentSpec
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


_TRANSLATE_TO_ZH = EnrichmentSpec(
    plugin_id="translate", kind=EnrichmentKind.TRANSLATION, lang="zh"
)


def _chinese_draft(index: int) -> ItemDraft:
    return ItemDraft(
        source_id=SOURCE,
        external_id=f"zh{index}",
        title=f"第 {index} 篇",
        canonical_url=f"https://example.com/zh/{index}",
        published_at=NOW,
        content="<p>这是一篇已经是中文的文章不需要再翻译</p>",
    )


def test_a_dropped_translation_spec_is_not_an_info_line_per_item(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """AC-299: a Chinese timeline through translate->zh drops every spec.

    That is the steady state of such a source, not a per-item event, so the
    INFO volume must not grow with the batch. AC-292's test passes no chain, so
    it never reaches this line -- the rule held only on the path the test drove.
    """
    counts: list[int] = []
    dropped_per_run: list[list[str]] = []
    for label, size in (("one", 1), ("fifty", 50)):
        engine = create_engine_for(migrated_database(tmp_path, f"chain-{label}.db"))
        try:
            with sessionmaker(engine, expire_on_commit=False)() as session:
                _add_source(
                    session,
                    source_id=SOURCE,
                    policy={"type": "fixed", "interval": "1h"},
                )
                caplog.clear()
                with caplog.at_level(logging.DEBUG, logger="app.pipeline"):
                    normalise_and_store(
                        session,
                        [_chinese_draft(i) for i in range(size)],
                        chain=(_TRANSLATE_TO_ZH,),
                    )
                counts.append(len(_store_path_info_records(caplog)))
                dropped_per_run.append(
                    [
                        record.getMessage()
                        for record in caplog.records
                        if record.name == "app.pipeline"
                        and record.levelno == logging.DEBUG
                        and "already in" in record.getMessage()
                    ]
                )
        finally:
            engine.dispose()

    assert counts[0] == counts[1], counts
    # Moved down a level, not deleted: the reason is still in the journal for
    # anyone diagnosing "why was this not translated?".
    assert all(dropped_per_run), "the reason a spec was dropped must still be logged"


def _failing_client() -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(403)))


def test_a_source_stuck_in_backoff_is_not_a_line_per_tick(
    session_factory: sessionmaker[Session], caplog: pytest.LogCaptureFixture
) -> None:
    """AC-300: "still failing" is one state, not one event per tick.

    A feed answering 403 is held in backoff and re-evaluated on every tick. The
    decision does not change between those ticks, so a line per tick is 1440
    identical rows a day for one broken source -- the flood issue #297's rule
    exists to stop, on the one path that still had it.
    """
    with session_factory() as session:
        _add_source(
            session,
            source_id="src-failing-log",
            policy={"type": "fixed", "interval": "60s"},
        )

    client = _failing_client()
    try:
        with caplog.at_level(logging.INFO, logger="app.scheduler"):
            for index in range(10):
                run_tick(
                    session_factory,
                    client=client,
                    now=NOW + timedelta(seconds=30 * index),
                )

        failing = [
            line for line in _info_records(caplog, "app.scheduler") if "failing" in line
        ]
        assert len(failing) == 1, failing
    finally:
        client.close()
