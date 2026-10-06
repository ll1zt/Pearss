"""Scenario register (normative source: SCENARIOS.md).

AC-280..282: DEBUG shows which dedupe rule matched and what happened to each
draft; the default level stays quiet. The fetch preflight's refusal reason and
the work ledger's per-unit outcome are DEBUG too, for the same reason: they are
the "why did this branch happen" facts a diagnosis opens with.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.pipeline import fetch_one, normalise_and_store
from core.models import ItemDraft
from tests.migrated_schema import migrated_database
from tests.source_seed import seed_source

pytestmark = pytest.mark.integration

SOURCE = "src-1"


@pytest.fixture
def session(tmp_path: Path) -> Iterator[Session]:
    """A migrated database containing one source row."""
    migrated_database(tmp_path, "decision-debug.db")
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'decision-debug.db'}")
    try:
        with Session(engine) as opened:
            seed_source(
                opened,
                source_id=SOURCE,
                url="https://example.com/src-1/feed",
            )
            opened.commit()
            yield opened
    finally:
        engine.dispose()


def _draft(*, external_id: str, body: str, title: str = "T") -> ItemDraft:
    return ItemDraft(
        origin_id=SOURCE,
        external_id=external_id,
        title=title,
        canonical_url=f"https://example.com/{external_id}",
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
        content=body,
    )


def _debug_records(caplog: pytest.LogCaptureFixture, logger: str) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == logger and record.levelno == logging.DEBUG
    ]


@pytest.mark.integration
def test_debug_names_the_dedupe_match_source(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    """AC-280: an identity hit says so, and names the row it matched."""
    stored = normalise_and_store(session, [_draft(external_id="e1", body="<p>one</p>")])
    first_id = stored[0].result.item.id

    # Same identity (external_id -> guid), different content: identity matched.
    with caplog.at_level(logging.DEBUG, logger="adapters.repository.items"):
        normalise_and_store(session, [_draft(external_id="e1", body="<p>changed</p>")])

    lines = _debug_records(caplog, "adapters.repository.items")
    assert lines, "the dedupe decision left no debug line"
    assert any("via identity" in line and first_id in line for line in lines), lines


@pytest.mark.integration
def test_debug_names_the_content_hash_match_source(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    """AC-280: the other comparison names itself too, so the two are tellable."""
    stored = normalise_and_store(
        session, [_draft(external_id="e1", body="<p>same</p>")]
    )
    first_id = stored[0].result.item.id

    # A different entry (different external_id) with the same body: content hit.
    with caplog.at_level(logging.DEBUG, logger="adapters.repository.items"):
        normalise_and_store(session, [_draft(external_id="e2", body="<p>same</p>")])

    lines = _debug_records(caplog, "adapters.repository.items")
    assert any("via content_hash" in line and first_id in line for line in lines), lines


@pytest.mark.integration
def test_debug_reports_each_draft_outcome(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    """AC-281: three drafts, three lines, each naming its action and item id."""
    normalise_and_store(session, [_draft(external_id="e1", body="<p>old</p>")])

    batch = [
        _draft(external_id="e1", body="<p>old</p>"),  # same content -> skip
        _draft(external_id="e1", body="<p>new</p>"),  # new content -> update_pending
        _draft(external_id="e2", body="<p>fresh</p>"),  # new entry -> insert
    ]
    with caplog.at_level(logging.DEBUG, logger="app.pipeline"):
        results = normalise_and_store(session, batch)

    lines = _debug_records(caplog, "app.pipeline")
    assert len(lines) == 3, lines
    for line, ingested in zip(lines, results, strict=True):
        assert f"action={ingested.outcome.action.value}" in line
        assert f"item={ingested.result.item.id}" in line


@pytest.mark.integration
def test_the_default_level_produces_none_of_the_debug_lines(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    """AC-282: INFO output is unchanged by the debug additions.

    Asserted on the *messages*, not on `levelno == DEBUG`: a regression that
    promoted one of these lines to INFO would slip through a filter that only
    looks at DEBUG records -- the test would pass on the very change it exists
    to catch (AC-309).
    """
    normalise_and_store(session, [_draft(external_id="e1", body="<p>old</p>")])

    with caplog.at_level(logging.INFO, logger="app.pipeline"):
        normalise_and_store(
            session,
            [
                _draft(external_id="e1", body="<p>old</p>"),
                _draft(external_id="e2", body="<p>fresh</p>"),
            ],
        )

    spoken = [record.getMessage() for record in caplog.records]
    assert not any("draft outcome:" in message for message in spoken), spoken


@pytest.mark.integration
def test_a_preflight_refusal_says_why_at_debug(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    """AC-280 (the fetch preflight branch): a refusal names its reason."""
    from adapters.repository import get_source

    source = get_source(session, SOURCE)
    assert source is not None

    with caplog.at_level(logging.DEBUG, logger="app.pipeline"):
        unit = fetch_one(
            session,
            source=source,
            registration=None,
            client=None,  # type: ignore[arg-type]  # refused before any request
            url="https://example.com/post?token=secret",
        )

    assert unit.action.value == "rejected"
    lines = _debug_records(caplog, "app.pipeline")
    assert any("no collector serves this source" in line for line in lines), lines
    assert all("secret" not in line for line in lines), lines
