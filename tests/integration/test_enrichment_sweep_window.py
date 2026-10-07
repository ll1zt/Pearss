"""The sweep's candidate window, and the period it actually runs at (issue #376).

Scenario register (the issue's 验收判据, which carries no AC number):

  - With N > concurrency rows queued, one sweep claims exactly `concurrency`.
  - The head of the queue being paced must not consume the round: a row behind
    it, on a fresh host, is claimed in the *same* sweep.
  - The period between sweeps is observable (readable and logged), and a loop
    that was starved by something else in the process is reported rather than
    silently running at twice the configured interval.

Why this matters: the enumeration window was exactly `concurrency` wide, and a
paced row is skipped without being replaced. So a host in cooldown with a block
of rows at the head of the queue (ordered by `created_at`) could consume an
entire round's opportunity -- the observed case was 40 retrying translate rows
holding up 762 fresh ones for nine minutes while every sweep reported "2 ran".
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from adapters.repository import create_source, parse_chain, timestamp
from app.enrichment_worker import EnrichmentWorker, sweep_queue
from app.pipeline import normalise_and_store
from core.models import ItemDraft
from tests.migrated_schema import migrated_database

pytestmark = pytest.mark.integration

CHAIN = '[{"plugin_id":"fulltext","kind":"fulltext"}]'


class TransientFailure:
    """A processor that fails the way a dead provider does: requeue, counted."""

    @staticmethod
    def process(item_ctx: Any, *, target_lang: str, store: Any) -> str:
        import httpx

        raise httpx.ConnectError("provider down")


@pytest.fixture
def session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    url = migrated_database(tmp_path, "sweep.db")
    from app.main import create_engine_for

    engine = create_engine_for(url)
    try:
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        engine.dispose()


def _seed(session: Session, urls: list[str]) -> str:
    """One source, the fulltext chain, one item per URL (its host is the item's)."""
    source = create_source(
        session,
        source_type="rss",
        url="https://sweep.example/feed",
        canonical_url="https://sweep.example/feed",
        schedule_policy="{}",
        tenant_id="default",
        pipeline_chain=CHAIN,
        at=timestamp(datetime.now(UTC)),
    )
    normalise_and_store(
        session,
        [
            ItemDraft(
                origin_id=source.id,
                # The identity is (tenant, origin, external_id): numbering
                # them per call would make a second call's items duplicates of
                # the first's, which is not what this file is testing.
                external_id=url,
                title=f"Post {index}",
                canonical_url=url,
                published_at=None,
                # Distinct bodies on purpose: identical content in one
                # origin dedupes to a single item, which would silently shrink
                # the queue this file is about.
                content=f"<p>Summary of {url}.</p>",
            )
            for index, url in enumerate(urls)
        ],
        chain=parse_chain(CHAIN, target_lang=None),
    )
    session.commit()
    return source.id


def _claimed_attempts(session: Session) -> dict[str, int]:
    """item URL -> attempt_count: a claimed row is the one that was attempted.

    Read rather than mocked: `attempt_count` is written by the claim's own
    outcome, so it answers "which rows did this sweep act on" without a stub
    that could disagree with the code under test.
    """
    rows = session.execute(
        sa.text(
            "select i.url, e.attempt_count from item_enrichment e"
            " join item i on i.id = e.item_id"
        )
    ).all()
    return {url: count for url, count in rows}


@pytest.mark.integration
def test_a_sweep_claims_exactly_the_concurrency_and_no_more(
    session_factory: sessionmaker[Session],
) -> None:
    """N > concurrency queued: the round claims `concurrency` rows, pinned."""
    with session_factory() as session:
        _seed(
            session,
            [f"https://h{index}.example/post" for index in range(5)],
        )
        report = sweep_queue(
            session,
            processors={"fulltext": TransientFailure},
            row_session_factory=session_factory,
            concurrency=3,
        )
        session.commit()
        assert report["ran"] == 3, report
        attempts = _claimed_attempts(session)

    assert len(attempts) == 5, "five rows were queued"
    assert sorted(attempts.values()) == [0, 0, 1, 1, 1], attempts


@pytest.mark.integration
def test_a_paced_head_does_not_consume_the_round(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The head is one host with a spent budget; the row behind it still lands.

    Reproduces the shape of the reported incident: the queue is ordered by
    `created_at`, so the blocked host sits in front, and a window of exactly
    `concurrency` rows would spend the whole round on it.
    """
    from app.fetch_pacing import reset_pacer

    monkeypatch.setenv("PEARSS_FETCH_MAX_PER_HOST_PER_SWEEP", "1")
    reset_pacer()
    try:
        with session_factory() as session:
            _seed(
                session,
                ["https://a.example/1", "https://a.example/2"],
            )
            _seed(
                session,
                ["https://b.example/1", "https://b.example/2"],
            )
            report = sweep_queue(
                session,
                processors={"fulltext": TransientFailure},
                row_session_factory=session_factory,
                concurrency=2,
            )
            session.commit()
            attempts = _claimed_attempts(session)
    finally:
        reset_pacer()

    assert report["ran"] == 2, report
    # a.example's budget is one per sweep, so its second row is deferred -- and
    # the round still made its second claim, on the fresh host.
    assert attempts["https://a.example/1"] == 1, attempts
    assert attempts["https://b.example/1"] == 1, (
        "a paced head must not consume the round: the row behind it is claimable "
        f"in the same sweep ({attempts})"
    )
    assert attempts["https://a.example/2"] == 0, attempts
    assert attempts["https://b.example/2"] == 0, attempts


@pytest.mark.integration
def test_a_starved_loop_is_reported_and_the_period_is_readable(
    session_factory: sessionmaker[Session],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The period has to be a number the process reports, not a journal count.

    The incident's rounds were ~130s apart against a configured 60s, and the
    sweep itself logged 0.8-6.1s: the difference was spent *outside* the sweep,
    where nothing was reporting. So the worker measures the gap between rounds
    and says so when it is not the sleep it asked for.
    """
    worker = EnrichmentWorker(session_factory, processors={}, sweep_seconds=60)

    asyncio.run(worker.sweep_once())
    assert worker.last_interval_seconds is None, "the first round has no gap"

    # The previous round ended 130s ago: 60s of sleep plus ~70s of not running.
    worker._last_sweep_ended_at = time.monotonic() - 130
    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="app.enrichment_worker"):
        asyncio.run(worker.sweep_once())
    # Annotated rather than inferred: mypy resolves the property to the
    # narrowed type of the attribute behind it (None), which would make the
    # assert below look like dead code.
    measured: float | None = worker.last_interval_seconds
    elapsed = time.monotonic() - started

    assert measured is not None, "the second round has a gap to report"
    # 130s of gap, plus however long this sweep itself took.
    assert measured >= 130, measured
    assert measured <= 130 + elapsed, (measured, elapsed)
    # And the report names it: the number in the log is the number on the
    # object, so an operator reading the journal sees the same period a test
    # would assert on.
    assert "starved" in caplog.text, caplog.text
    assert "130s" in caplog.text, caplog.text
