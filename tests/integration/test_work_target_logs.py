"""Scenario register (normative source: SCENARIOS.md).

AC-298: a work unit's target is caller-supplied text, so the DEBUG line that
names it must not carry a token, must not break into a second journal line, and
must not print an ``ingest`` payload's body. The primitives themselves are the
shared URL redactor (``adapters.redaction``) and the control-character escaper
(``adapters.log_safety``); what this file pins is that the work worker uses
them -- that is the line that reaches the journal.
"""

from __future__ import annotations

import itertools
import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from adapters.repository import create_source, create_work
from adapters.timestamps import fixed_width_utc as timestamp
from app.main import create_engine_for
from app.work_worker import WorkWorker
from tests.migrated_schema import migrated_database

pytestmark = pytest.mark.integration

SITE = "https://targets.example"
FEED_URL = f"{SITE}/feed"

_FINGERPRINTS = itertools.count()


@pytest.fixture
def session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    url = migrated_database(tmp_path, "work_target_logs.db")
    engine = create_engine_for(url)
    try:
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        engine.dispose()


def _submit(
    session_factory: sessionmaker[Session],
    *,
    kind: str,
    targets: list[str],
    params: dict[str, object] | None = None,
) -> str:
    with session_factory() as session:
        work = create_work(
            session,
            kind=kind,
            targets=targets,
            params=params or {},
            fingerprint=f"fp-target-logs-{next(_FINGERPRINTS)}",
            tenant_id="default",
        )
        session.commit()
        return work.id


async def _sweep(session_factory: sessionmaker[Session]) -> None:
    """One pass of the real worker, with a client these works never use."""
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(599, text="no request belongs to this test")
        )
    )
    worker = WorkWorker(session_factory, client=client)
    try:
        await worker.sweep_once()
    finally:
        client.close()


def _worker_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "app.work_worker" and record.levelno == logging.DEBUG
    ]


@pytest.mark.asyncio
async def test_a_unit_target_with_a_token_and_a_newline_cannot_leak_or_forge(
    session_factory: sessionmaker[Session], caplog: pytest.LogCaptureFixture
) -> None:
    """AC-298: the per-unit line names a string the caller chose."""
    work_id = _submit(
        session_factory,
        kind="fetch",
        targets=[
            f"{SITE}/post?token=SUPERSECRET",
            f"{SITE}/post\n1970-01-01 ERROR forged-line",
        ],
        # A source this deployment cannot resolve: every target is written off
        # in one sentence, which is the branch that logs them.
        params={"source_id": "no-such-source"},
    )

    with caplog.at_level(logging.DEBUG, logger="app.work_worker"):
        await _sweep(session_factory)

    messages = _worker_messages(caplog)
    assert messages, "the unit outcome was not logged at all"
    joined = "\n".join(messages)
    assert work_id in joined
    assert "SUPERSECRET" not in joined, joined
    for message in messages:
        assert "\n" not in message and "\r" not in message, message
    assert not any(
        line.startswith("1970-01-01 ERROR") for line in joined.splitlines()
    ), joined
    # The line still says what was asked for, minus the credential.
    assert f"{SITE}/post" in joined


@pytest.mark.asyncio
async def test_an_ingest_target_is_summarised_so_its_body_stays_out_of_the_journal(
    session_factory: sessionmaker[Session], caplog: pytest.LogCaptureFixture
) -> None:
    """AC-298: an ingest target *is* the item, body included."""
    with session_factory() as session:
        source = create_source(
            session,
            source_type="rss",
            url=FEED_URL,
            canonical_url=FEED_URL,
            schedule_policy='{"type": "fixed", "interval": "30m"}',
            pipeline_chain="[]",
            target_lang="en",
            tenant_id="default",
            at=timestamp(datetime(2026, 10, 2, 3, 0, tzinfo=UTC)),
        )
        session.commit()
        source_id = str(source.id)

    target = json.dumps(
        {"external_id": "x", "title": "T", "content": "<p>MARKER-BODY-TEXT</p>"},
        separators=(",", ":"),
        sort_keys=True,
    )
    _submit(
        session_factory,
        kind="ingest",
        targets=[target],
        params={"source_id": source_id},
    )

    with caplog.at_level(logging.DEBUG, logger="app.work_worker"):
        await _sweep(session_factory)

    messages = _worker_messages(caplog)
    assert messages, "the unit outcome was not logged at all"
    joined = "\n".join(messages)
    assert "MARKER-BODY-TEXT" not in joined, joined
    # What replaces the payload: enough to tell two units apart without
    # carrying either one.
    assert f"{len(target)} bytes" in joined, joined
