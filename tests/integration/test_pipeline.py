"""End-to-end tests for the collection pipeline.

These run the real path — fixture -> collector -> normalize -> repository ->
RSS output — against a temporary SQLite database. No network, and never the
shared development database.

The gate assertions matter most: a pending item reaching a feed is the failure
the entire design exists to prevent, and it stays invisible until a client has
already cached the wrong content.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from lxml import etree
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from adapters.raw_payload import RawPayload
from adapters.repository import (
    count_items,
    items_ready_for_feed,
    mark_published,
    record_raw_fetch,
    store_item,
)
from adapters.rss_out import FeedMeta, render_feed, visible_items
from core.enums import PublishStatus
from core.models import Item, ItemDraft
from core.normalize import normalize
from plugins.collectors import rss

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "src/plugins/collectors/rss/fixtures"
TENANT = "default"
SOURCE = "src-1"


@pytest.fixture
def session(tmp_path: Path) -> Iterator[Session]:
    """A migrated database containing one source row."""
    db_path = tmp_path / "pipeline.db"
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{db_path}")
    command.upgrade(config, "head")

    engine = create_engine(f"sqlite+pysqlite:///{db_path}")
    try:
        with Session(engine) as opened:
            _seed_source(opened, SOURCE)
            yield opened
    finally:
        # Disposed explicitly: filterwarnings=error turns an unclosed
        # connection's ResourceWarning into a failure, which is the correct
        # strictness — a leaked connection per test would slow CI and, on a
        # real database, exhaust the pool.
        engine.dispose()


def _seed_source(session: Session, source_id: str) -> None:
    session.execute(
        text(
            "insert into source (id, type, schedule_policy, status, tenant_id,"
            " created_at, updated_at) values (:id, 'rss', '{}', 'active',"
            " 'default', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
        ),
        {"id": source_id},
    )
    session.commit()


def collect(source_id: str, fixture: str) -> list[ItemDraft]:
    """Run the collector over a recorded snapshot."""
    payload = RawPayload(
        url="https://blog.example.com/feed",
        status_code=200,
        body=(FIXTURES / fixture).read_bytes(),
        source_id=source_id,
        raw_fetch_id="raw-1",
    )
    return rss.parse(payload)


def normalise_all(drafts: list[ItemDraft]) -> list[Item]:
    return [normalize(draft, tenant_id=TENANT).item for draft in drafts]


def store_all(session: Session, items: list[Item]) -> None:
    for item in items:
        store_item(session, item)
    session.commit()


def promote_all_to_ready(session: Session) -> None:
    """Advance every item through the gate, as a completed chain would."""
    session.execute(text("update item set publish_status = 'ready'"))
    session.commit()


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_fixture_to_stored_items(session: Session) -> None:
    items = normalise_all(collect(SOURCE, "valid.xml"))
    assert len(items) == 3

    store_all(session, items)
    assert count_items(session) == 3


@pytest.mark.integration
def test_re_fetching_produces_no_duplicates(session: Session) -> None:
    first = normalise_all(collect(SOURCE, "valid.xml"))
    store_all(session, first)

    # Same fixture again: deterministic ids mean this is a no-op.
    second = normalise_all(collect(SOURCE, "valid.xml"))
    assert [i.id for i in first] == [i.id for i in second]

    for item in second:
        outcome = store_item(session, item)
        assert outcome.action.value == "skip_same_source"
    session.commit()

    assert count_items(session) == 3


@pytest.mark.integration
def test_same_article_from_two_sources_is_related(session: Session) -> None:
    _seed_source(session, "src-2")

    one = ItemDraft(
        source_id="src-1",
        external_id="e1",
        title="Shared Article",
        canonical_url="https://a.example/1",
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
        content="<p>Same body.</p>",
    )
    two = ItemDraft(
        source_id="src-2",
        external_id="e2",
        title="Shared Article",
        canonical_url="https://b.example/2",
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
        content="<p>Same   body.</p>",
    )

    first = normalize(one, tenant_id=TENANT).item
    second = normalize(two, tenant_id=TENANT).item

    # Cross-source dedupe: same content_hash, distinct guids.
    assert first.content_hash == second.content_hash
    assert first.guid != second.guid

    assert store_item(session, first).action.value == "insert"
    session.commit()
    assert store_item(session, second).action.value == "insert_related"
    session.commit()

    assert count_items(session) == 2


@pytest.mark.integration
def test_raw_fetch_is_recorded(session: Session) -> None:
    record_raw_fetch(
        session,
        fetch_id="fetch-1",
        source_id=SOURCE,
        status="succeeded",
        correlation_id="corr-1",
        payload_ref="blob://abc",
        http_status=200,
    )
    session.commit()

    count = session.execute(text("select count(*) from raw_fetch")).scalar()
    assert count == 1


# --------------------------------------------------------------------------
# The publish gate
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_pending_items_never_reach_the_feed(session: Session) -> None:
    items = normalise_all(collect(SOURCE, "valid.xml"))
    store_all(session, items)

    assert items_ready_for_feed(session) == []

    rendered = render_feed(
        FeedMeta(title="T", link="https://e.com"),
        items_ready_for_feed(session),
        {},
    ).decode()
    for item in items:
        assert item.guid not in rendered


@pytest.mark.integration
def test_ready_items_appear_in_the_feed(session: Session) -> None:
    items = normalise_all(collect(SOURCE, "valid.xml"))
    store_all(session, items)
    promote_all_to_ready(session)

    ready = items_ready_for_feed(session)
    assert len(ready) == 3

    rendered = render_feed(
        FeedMeta(title="T", link="https://e.com"), ready, {}
    ).decode()
    for item in ready:
        assert item.guid in rendered


@pytest.mark.integration
def test_feed_has_stable_guids_and_canonical_links(session: Session) -> None:
    items = normalise_all(collect(SOURCE, "valid.xml"))
    store_all(session, items)
    promote_all_to_ready(session)

    ready = items_ready_for_feed(session)
    rendered = render_feed(
        FeedMeta(title="T", link="https://e.com"),
        ready,
        {item.id: "Body text." for item in ready},
    )

    root = etree.fromstring(rendered)
    guids = root.findall("./channel/item/guid")
    links = root.findall("./channel/item/link")

    assert len(guids) == 3
    assert all(guid.text for guid in guids)
    assert all((link.text or "").startswith("https://") for link in links)
    # Tracking parameters were stripped during normalisation.
    assert not any("utm_source" in (link.text or "") for link in links)


@pytest.mark.integration
def test_feed_leaks_no_internal_references(session: Session) -> None:
    items = normalise_all(collect(SOURCE, "valid.xml"))
    store_all(session, items)
    promote_all_to_ready(session)

    ready = items_ready_for_feed(session)
    rendered = render_feed(
        FeedMeta(title="T", link="https://e.com"),
        ready,
        {item.id: "Body." for item in ready},
    ).decode()

    assert "content_ref" not in rendered
    assert "payload_ref" not in rendered
    # Internal row ids must not be exposed; only the public guid.
    for item in ready:
        assert item.id not in rendered


@pytest.mark.integration
def test_publishing_happens_exactly_once(session: Session) -> None:
    items = normalise_all(collect(SOURCE, "valid.xml"))
    store_all(session, items)
    promote_all_to_ready(session)

    ready = items_ready_for_feed(session)
    at = datetime(2026, 6, 1, tzinfo=UTC)

    assert mark_published(session, ready, at=at) == 3
    session.commit()

    # A second render must not re-publish: the transition is one-way.
    assert mark_published(session, ready, at=at + timedelta(days=1)) == 0
    session.commit()


@pytest.mark.integration
def test_visible_items_filters_pending_in_memory() -> None:
    # SQL filtering is the primary defence; this guards the in-memory helper
    # used when a caller already holds a list.
    pending = Item(
        id="p",
        source_id="s",
        guid="gp",
        content_hash="h1",
        title="Pending",
        url="https://e.com/p",
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
        publish_status=PublishStatus.PENDING,
    )
    ready = Item(
        id="r",
        source_id="s",
        guid="gr",
        content_hash="h2",
        title="Ready",
        url="https://e.com/r",
        published_at=datetime(2026, 1, 2, tzinfo=UTC),
        publish_status=PublishStatus.READY,
    )
    assert [i.id for i in visible_items([pending, ready])] == ["r"]
