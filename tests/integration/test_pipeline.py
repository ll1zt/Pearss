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
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from lxml import etree
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from adapters.db_models import Source
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
from core.normalize import content_hash, normalize
from plugins.collectors.rss import collector as rss

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
            "insert into source (id, type, url, canonical_url, schedule_policy,"
            " status, tenant_id, created_at, updated_at)"
            " values (:id, 'rss', :url, :url, '{}', 'active', 'default',"
            " '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
        ),
        {"id": source_id, "url": f"https://example.com/{source_id}/feed"},
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
def test_pipeline_stores_bodies_and_serves_them(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The body must survive the whole path, not just the title.

    A regression that actually shipped: items were stored with a null
    content_ref, so NetNewsWire listed every article with no body and the RSS
    feed emitted no description at all.
    """
    from app.pipeline import bodies_for, normalise_and_store

    monkeypatch.setenv("PEARSS_STORAGE_DIR", str(tmp_path / "storage"))

    drafts = collect(SOURCE, "valid.xml")
    normalise_and_store(session, drafts)

    stored = items_ready_for_feed(session)
    assert stored, "items should be visible once the chain completes"
    assert all(item.content_ref for item in stored), (
        "every item should reference a body"
    )

    bodies = bodies_for(session, stored)
    assert len(bodies) == len(stored)
    assert any("short summary" in body for body in bodies.values())


@pytest.mark.integration
def test_rss_output_includes_descriptions(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.pipeline import bodies_for, normalise_and_store

    monkeypatch.setenv("PEARSS_STORAGE_DIR", str(tmp_path / "storage"))
    normalise_and_store(session, collect(SOURCE, "valid.xml"))

    rendered = render_feed(
        FeedMeta(title="T", link="https://e.com"),
        items_ready_for_feed(session),
        bodies_for(session, items_ready_for_feed(session)),
    ).decode()
    assert "<![CDATA[" in rendered
    assert "short summary" in rendered


@pytest.mark.integration
def test_a_declared_rss_source_is_collected_whatever_its_url(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Routing must honour source.type, not guess from the URL.

    hnrss.org/frontpage is a real RSS feed carrying none of the fragments the
    RSS matcher looks for. Routing purely by URL refused to collect it and
    reported structure_changed for a source the operator had explicitly
    created as `rss` -- a regression this collector registry introduced, since
    the pipeline previously parsed every source as a feed unconditionally.
    """
    from app import collector_registry

    registration = collector_registry.select_for("rss", "https://hnrss.org/frontpage")
    assert registration is not None
    assert registration.name == "rss"

    # And the URL alone still refuses, which is what makes the type decisive.
    assert collector_registry.select("https://hnrss.org/frontpage") is None


@pytest.mark.integration
def test_a_web_source_is_still_routed_by_url(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`web` says something must be scraped, not which scraper fits."""
    from app import collector_registry

    assert collector_registry.select_for("web", "https://t.me/s/durov") is not None
    assert collector_registry.select_for("web", "https://example.com/x") is None


@pytest.mark.integration
def test_collection_records_the_name_the_origin_gives_itself(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The feed's own <title> should end up on the source, not on each item.

    Collectors report it through ItemDraft.metadata, which normalize() drops by
    design; without this step the Reader API has nothing to show and a client
    renders the subscription by its hex id.
    """
    from adapters.repository import set_source_title_if_absent
    from app.pipeline import normalise_and_store

    monkeypatch.setenv("PEARSS_STORAGE_DIR", str(tmp_path / "storage"))
    drafts = collect(SOURCE, "valid.xml")
    assert drafts[0].metadata.get("feed_title") == "Example Blog"

    set_source_title_if_absent(
        session, source_id=SOURCE, title=drafts[0].metadata.get("feed_title")
    )
    normalise_and_store(session, drafts)
    session.commit()

    row = session.get(Source, SOURCE)
    assert row is not None
    assert row.title == "Example Blog"


@pytest.mark.integration
def test_a_recorded_title_is_not_overwritten(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator's rename, or an earlier origin name, outranks a later guess.

    The writer is conditional for the same reason the schedule is not stored
    twice: an unconditional write would let the origin silently undo a decision
    made on the operator's side.
    """
    from adapters.repository import set_source_title_if_absent

    assert set_source_title_if_absent(session, source_id=SOURCE, title="Kept") is True
    session.commit()
    assert set_source_title_if_absent(session, source_id=SOURCE, title="Later") is False
    session.commit()

    row = session.get(Source, SOURCE)
    assert row is not None and row.title == "Kept"


@pytest.mark.integration
def test_blank_title_is_not_recorded(session: Session) -> None:
    """A whitespace-only origin title must not occupy the column."""
    from adapters.repository import set_source_title_if_absent

    assert set_source_title_if_absent(session, source_id=SOURCE, title="   ") is False
    assert set_source_title_if_absent(session, source_id=SOURCE, title=None) is False


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


@pytest.mark.integration
def test_reworded_article_does_not_collide_on_the_guid(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bug that reached production.

    An entry's id and guid derive from (tenant, source, external_id) while its
    content_hash derives from the body. When a feed reworded an article the two
    disagreed, content-only dedupe missed it, and the resulting INSERT hit
    `UNIQUE constraint failed: item.tenant_id, item.guid`.
    """
    from app.pipeline import normalise_and_store

    monkeypatch.setenv("PEARSS_STORAGE_DIR", str(tmp_path / "storage"))

    def draft_with(content: str) -> ItemDraft:
        return ItemDraft(
            source_id=SOURCE,
            external_id="stable-entry-1",
            title="An article",
            canonical_url="https://blog.example.com/stable",
            published_at=datetime(2026, 1, 1, tzinfo=UTC),
            content=content,
        )

    first = normalise_and_store(session, [draft_with("<p>Original text.</p>")])
    session.commit()
    assert first[0].outcome.action.value == "insert"

    # Same entry, reworded body, still pending at the time of the second call.
    second = normalise_and_store(
        session, [draft_with("<p>Rewritten text that differs.</p>")]
    )
    session.commit()

    # It must not be treated as a new article.
    assert second[0].outcome.action.value in {
        "update_pending",
        "skip_published_content_changed",
    }
    assert count_items(session) == 1


@pytest.mark.integration
def test_reworded_published_article_keeps_the_frozen_content(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.pipeline import normalise_and_store

    monkeypatch.setenv("PEARSS_STORAGE_DIR", str(tmp_path / "storage"))
    draft = ItemDraft(
        source_id=SOURCE,
        external_id="published-entry",
        title="Headline",
        canonical_url="https://blog.example.com/p",
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
        content="<p>First version.</p>",
    )
    normalise_and_store(session, [draft])
    session.commit()
    # The chain completes and a client sees it.
    session.execute(sa.text("update item set publish_status='published'"))
    session.commit()

    changed = ItemDraft(
        source_id=SOURCE,
        external_id="published-entry",
        title="Headline",
        canonical_url="https://blog.example.com/p",
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
        content="<p>Quietly rewritten after publication.</p>",
    )
    outcome = normalise_and_store(session, [changed])[0]
    session.commit()
    assert outcome.outcome.action.value == "skip_published_content_changed"

    stored = items_ready_for_feed(session)
    assert len(stored) == 1
    # The stored hash is still the one derived from the ORIGINAL body. That is
    # the whole point: a client that cached the first version keeps being
    # served something consistent with it, rather than the row quietly
    # changing under a frozen guid.
    assert stored[0].content_hash == content_hash(
        title="Headline", content="<p>First version.</p>"
    )
    assert stored[0].content_hash != content_hash(
        title="Headline", content="<p>Quietly rewritten after publication.</p>"
    )
