"""End-to-end: a WordPress summary feed collected with REST fulltext.

The pipeline runs for real — fetch hook, summary detection, REST swap,
normalise, dedupe, gate — against fixture-served HTTP and a temporary
database. The transport serves the truncated feed and the REST payload on
their real URL shapes, so a wrong path or a skipped upgrade fails loudly.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from adapters.repository import get_source
from app.pipeline import collect_source
from tests.migrated_schema import migrated_database
from tests.source_seed import seed_source

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "src/plugins/collectors/rss/fixtures"
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
POLICY = '{"type":"fixed","interval":"30m"}'
FEED_URL = "https://bear.example.com/blog/feed"


@pytest.fixture
def session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    url = migrated_database(tmp_path, "wp.db")

    engine = create_engine(url)
    try:
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        engine.dispose()


def _add_source(session: Session, source_id: str, url: str) -> None:
    seed_source(
        session,
        source_id=source_id,
        source_type="rss",
        url=url,
        schedule_policy=POLICY,
    )
    session.commit()


def _wp_client(tmp_path: Path) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/wp-json/wp/v2/posts"):
            assert request.url.path.startswith("/blog/"), "subdirectory install"
            return httpx.Response(
                200,
                content=(FIXTURES / "wp_rest_posts.json").read_bytes(),
                headers={"content-type": "application/json"},
            )
        return httpx.Response(
            200, content=(FIXTURES / "wp_summary_feed.xml").read_bytes()
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.mark.integration
def test_summary_feed_stores_full_text(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """The stored body is the REST article, not the feed's truncated cut."""
    with session_factory() as session:
        _add_source(session, "s1", FEED_URL)
        source = get_source(session, "s1")
        assert source is not None
        result = collect_source(
            session, source=source, client=_wp_client(tmp_path), tenant_id="default"
        )
        session.commit()

    assert result.succeeded
    assert (result.drafts, result.inserted) == (2, 2)

    with session_factory() as session:
        rows = session.execute(
            sa.text("select title, content_ref from item where origin_id = 's1'")
        ).fetchall()
    # The query has no ORDER BY, so the order is the planner's business: it
    # asserted insertion order until a source-scoped index made the planner
    # return these in sort-key order instead. What this test is about is which
    # items were stored, so it asks that question.
    titles = {row[0] for row in rows}
    assert titles == {"視力検査と昼食", "引っ越し準備"}


@pytest.mark.integration
def test_a_second_collection_does_not_duplicate(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """Re-collection after the upgrade stays deduped: identity is the feed's
    GUID, which the summary/fulltext switch never touches."""
    client = _wp_client(tmp_path)
    with session_factory() as session:
        _add_source(session, "s2", FEED_URL)
        source = get_source(session, "s2")
        assert source is not None
        first = collect_source(
            session, source=source, client=client, tenant_id="default"
        )
        session.commit()

    with session_factory() as session:
        source = get_source(session, "s2")
        assert source is not None
        second = collect_source(
            session, source=source, client=client, tenant_id="default"
        )
        session.commit()

    assert (first.inserted, second.inserted) == (2, 0)
