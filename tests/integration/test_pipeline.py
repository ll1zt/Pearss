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
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from lxml import etree
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

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
from app.pipeline import CollectionResult
from core.enums import PublishStatus
from core.models import Item, ItemDraft
from core.normalize import content_hash, normalize
from plugins.collectors.rss import collector as rss
from tests.migrated_schema import migrated_database

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "src/plugins/collectors/rss/fixtures"
TENANT = "default"
SOURCE = "src-1"


@pytest.fixture
def session(tmp_path: Path) -> Iterator[Session]:
    """A migrated database containing one source row."""
    db_path = tmp_path / "pipeline.db"
    migrated_database(tmp_path, "pipeline.db")

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
def test_the_feed_is_a_window_not_the_whole_archive(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unbounded render reached 5.7 MB and 4,677 items.

    Every one of those also pulled its body out of the file store, and a client
    polling on a timer paid that per poll. The window keeps the feed a feed;
    older items stay reachable through the query API and the Reader API.
    """
    from adapters.repository import FEED_WINDOW, items_ready_for_feed
    from app.pipeline import normalise_and_store

    monkeypatch.setenv("PEARSS_STORAGE_DIR", str(tmp_path / "storage"))
    drafts = collect(SOURCE, "valid.xml")
    # Enough distinct items to exceed the window, so a missing LIMIT cannot pass.
    pages = FEED_WINDOW // max(1, len(drafts)) + 2
    for page in range(pages):
        normalise_and_store(
            session,
            [
                # Content must differ too: identical bodies share a
                # content_hash and dedupe into one item, which is correct and
                # would leave this test seeding three rows.
                replace(
                    draft,
                    external_id=f"{draft.external_id}-{page}",
                    canonical_url=f"{draft.canonical_url}-{page}",
                    title=f"{draft.title} (page {page})",
                    content=f"{draft.content} body-{page}",
                )
                for draft in drafts
            ],
        )
    session.commit()

    total = int(session.execute(sa.text("select count(*) from item")).scalar() or 0)
    assert total > FEED_WINDOW, f"seeded only {total} items; test proves nothing"

    rendered = items_ready_for_feed(session)
    assert len(rendered) == FEED_WINDOW
    # Newest first, so the window is the recent window rather than an arbitrary
    # slice of the table. Items without a publication time sort last by design,
    # so compare only the dated prefix.
    dated = [item.published_at for item in rendered if item.published_at]
    assert dated == sorted(dated, reverse=True)


@pytest.mark.integration
def test_the_window_can_be_widened_by_a_caller(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The limit is a default, not a hard ceiling baked into the query."""
    from adapters.repository import FEED_WINDOW, items_ready_for_feed
    from app.pipeline import normalise_and_store

    monkeypatch.setenv("PEARSS_STORAGE_DIR", str(tmp_path / "storage"))
    normalise_and_store(session, collect(SOURCE, "valid.xml"))
    session.commit()
    assert len(items_ready_for_feed(session, limit=FEED_WINDOW * 3)) == 3


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


# --------------------------------------------------------------------------
# Conditional GET loop (record_raw_fetch wiring)
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_the_conditional_loop_closes_over_two_cycles(session: Session) -> None:
    """Cycle one stores the ETag; cycle two must send it and see a 304.

    This is the acceptance test for the record_raw_fetch wiring: without it
    the second collect_source goes out unconditional and re-downloads the
    full body every time. A MockTransport stands in for the origin and
    refuses any request that fails to present the validator.
    """
    import httpx

    from app.pipeline import collect_source

    requests_seen: list[dict[str, str | None]] = []

    def origin(request: httpx.Request) -> httpx.Response:
        requests_seen.append(
            {
                "if_none_match": request.headers.get("if-none-match"),
                "if_modified_since": request.headers.get("if-modified-since"),
            }
        )
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        assert request.headers.get("if-none-match") is None, (
            "the first cycle must be unconditional"
        )
        return httpx.Response(
            200,
            content=(FIXTURES / "valid.xml").read_bytes(),
            headers={"etag": '"v1"', "content-type": "application/rss+xml"},
        )

    client = httpx.Client(transport=httpx.MockTransport(origin))

    source = session.get(Source, SOURCE)
    assert source is not None
    # Annotated so mypy's narrowing of `first` does not leak into `second`:
    # the two results are independent outcomes from the same function.
    first: CollectionResult = collect_source(session, source=source, client=client)
    session.commit()
    assert first.succeeded
    assert requests_seen[-1]["if_none_match"] is None

    state = session.execute(
        text(
            "select etag, last_modified from raw_fetch order by fetched_at desc limit 1"
        )
    ).one()
    assert state[0] == '"v1"', "the response ETag was not written back"

    second: CollectionResult = collect_source(session, source=source, client=client)
    session.commit()
    # Read through a plain local: asserting directly on `second.not_modified`
    # makes mypy narrow the attribute to `Literal[True]` and then report the
    # rest of the test unreachable (`warn_unreachable`), hiding real errors.
    revalidated = second.succeeded and second.not_modified
    assert revalidated, "the second cycle did not revalidate: the ETag loop is open"
    assert requests_seen[-1]["if_none_match"] == '"v1"'

    # The ignore below is for mypy, not for a real defect: after the assert it
    # narrows `revalidated` to `Literal[True]` and then reports this statement
    # unreachable under `warn_unreachable`. The rows do exist -- collect_source
    # records a raw_fetch on both the 200 and the 304 path -- so the assertion
    # is live and is exactly what the ETag loop is verified by.
    rows = session.execute(  # type: ignore[unreachable]
        text("select status, http_status from raw_fetch order by fetched_at")
    ).fetchall()
    assert [tuple(r) for r in rows] == [("fetched", 200), ("not_modified", 304)]


@pytest.mark.integration
def test_a_failed_fetch_is_recorded_and_resets_validators(session: Session) -> None:
    """A 5xx lands in raw_fetch with no validators, so the next cycle is
    unconditional rather than replaying a possibly-stale ETag."""
    import httpx

    from app.pipeline import collect_source

    def flaky(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client = httpx.Client(transport=httpx.MockTransport(flaky))
    source = session.get(Source, SOURCE)
    assert source is not None
    result = collect_source(session, source=source, client=client)
    session.commit()

    assert not result.succeeded and result.error_class == "network"
    row = session.execute(
        text(
            "select status, error_class, etag from raw_fetch"
            " order by fetched_at desc limit 1"
        )
    ).one()
    assert row[0] == "failed"
    assert row[1] == "network"
    assert row[2] is None


@pytest.mark.integration
def test_a_chained_source_holds_the_gate_until_processing_completes(
    session: Session,
) -> None:
    """A source with a pipeline chain queues enrichments and stays pending.

    The gate's whole point: with a required translation configured, the item
    must not reach ready — not here (the pipeline), not via any output path.
    The enrichment worker's success is what opens it.
    """
    import json as jsonlib

    from adapters.repository import parse_chain

    chain = [{"plugin_id": "translate", "kind": "translation", "required": True}]
    raw_chain = jsonlib.dumps(chain)
    session.execute(
        text(
            "update source set pipeline_chain = :chain, target_lang = 'zh'"
            " where id = :sid"
        ),
        {"chain": raw_chain, "sid": SOURCE},
    )
    session.commit()
    specs = parse_chain(raw_chain, target_lang="zh")
    assert specs[0].lang == "zh", "the source-level target_lang fills the spec"

    drafts = collect(SOURCE, "valid.xml")
    assert drafts

    # Store through the pipeline path with the chain: items stay pending and
    # one enrichment row per spec is queued.
    from app.pipeline import normalise_and_store

    ingested = normalise_and_store(session, drafts, chain=specs)
    session.commit()
    assert ingested

    statuses = session.execute(
        text("select publish_status from item where source_id = :s"), {"s": SOURCE}
    ).fetchall()
    assert all(row[0] == "pending" for row in statuses), (
        "a chained item must not open the gate at store time"
    )

    rows = session.execute(
        text(
            "select status, kind, lang from item_enrichment"
            " where item_id = (select id from item where source_id = :s limit 1)"
        ),
        {"s": SOURCE},
    ).fetchall()
    assert rows and rows[0][0] == "queued" and rows[0][1] == "translation"
    assert rows[0][2] == "zh"


@pytest.mark.integration
def test_the_enrichment_worker_opens_the_gate_when_the_chain_completes(
    session: Session,
) -> None:
    """Full round trip: chain holds the item, worker runs, gate opens.

    The processor here is a stub whose `process` writes a canned artifact —
    what is under test is the row lifecycle (queued -> running -> succeeded),
    the gate promotion, and the chain-complete check, not the provider call.
    """
    import json as jsonlib

    from adapters.repository import parse_chain
    from app.enrichment_worker import sweep_queue
    from app.pipeline import normalise_and_store

    raw_chain = jsonlib.dumps(
        [{"plugin_id": "translate", "kind": "translation", "required": True}]
    )
    session.execute(
        text(
            "update source set pipeline_chain = :chain, target_lang = 'zh'"
            " where id = :sid"
        ),
        {"chain": raw_chain, "sid": SOURCE},
    )
    session.commit()
    specs = parse_chain(raw_chain, target_lang="zh")

    drafts = collect(SOURCE, "valid.xml")
    ingested = normalise_and_store(session, drafts, chain=specs)
    session.commit()
    item_id = ingested[0].result.item.id

    statuses = session.execute(
        text("select publish_status from item where id = :i"), {"i": item_id}
    ).scalar()
    assert statuses == "pending", "the chain must hold the gate"

    row_sessions = sessionmaker(session.get_bind(), expire_on_commit=False)
    report = sweep_queue(
        session,
        processors={"translate": FakeTranslate},
        row_session_factory=row_sessions,
    )
    assert report["succeeded"] == len(drafts), report

    status = session.execute(
        text("select publish_status from item where id = :i"), {"i": item_id}
    ).scalar()
    assert status == "ready", "chain complete must open the gate"

    # The artifact is real: stored through filestore, referenced by the row.
    row = session.execute(
        text("select status, payload_ref from item_enrichment where item_id = :i"),
        {"i": item_id},
    ).first()
    assert row is not None
    assert row[0] == "succeeded"
    from adapters import filestore

    content = filestore.read_body(row[1])
    assert "Translated:" in content


@pytest.mark.integration
def test_a_failing_processor_holds_the_gate_and_records_the_class(
    session: Session,
) -> None:
    """A failed required enrichment stays pending with its error class.

    The gate's ENRICHMENT_FAILED path: the item must not leak unprocessed
    content, and the operator must be able to see why it is held.
    """
    import json as jsonlib

    from adapters.repository import parse_chain
    from app.enrichment_worker import sweep_queue
    from app.pipeline import normalise_and_store

    raw_chain = jsonlib.dumps(
        [{"plugin_id": "translate", "kind": "translation", "required": True}]
    )
    session.execute(
        text(
            "update source set pipeline_chain = :chain, target_lang = 'zh'"
            " where id = :sid"
        ),
        {"chain": raw_chain, "sid": SOURCE},
    )
    session.commit()
    specs = parse_chain(raw_chain, target_lang="zh")

    drafts = collect(SOURCE, "valid.xml")
    normalise_and_store(session, drafts, chain=specs)
    session.commit()

    row_sessions = sessionmaker(session.get_bind(), expire_on_commit=False)
    report = sweep_queue(
        session,
        processors={"translate": FailingTranslate},
        row_session_factory=row_sessions,
    )
    assert report["failed"] == len(drafts), report

    rows = session.execute(
        text("select status, error_class, detail from item_enrichment limit 1")
    ).first()
    assert rows is not None
    assert rows[0] == "failed"
    assert rows[1] == "auth"
    assert "provider rejected the key" in rows[2]

    statuses = session.execute(
        text("select publish_status from item where source_id = :s"), {"s": SOURCE}
    ).fetchall()
    assert all(row[0] == "pending" for row in statuses), (
        "a failed required enrichment must keep the gate shut"
    )


@pytest.mark.integration
def test_transient_failures_requeue_with_backoff_then_stop_at_limit(
    session: Session, tmp_path: Path
) -> None:
    """network failures requeue (attempt_count + backoff) up to the limit,
    then the row stops in `failed` for manual/agent re-trigger. auth fails
    immediately with no retry."""
    import json as jsonlib

    from adapters.db_models import Source as SourceRow
    from adapters.repository import backfill_chain
    from app.enrichment_worker import sweep_queue

    session.add(
        SourceRow(
            id="src-retry",
            tenant_id="default",
            type="rss",
            url="https://retry.example/feed",
            canonical_url="https://retry.example/feed",
            status="active",
            trust_level="standard",
            schedule_policy="{}",
            pipeline_chain=jsonlib.dumps(
                [{"plugin_id": "translate", "kind": "translation"}]
            ),
            target_lang="en",
            created_at="2026-01-01T00:00:00.000000Z",
            updated_at="2026-01-01T00:00:00.000000Z",
        )
    )
    from adapters.db_models import Item as ItemRow

    session.add(
        ItemRow(
            id="retry-item",
            source_id="src-retry",
            guid="g-retry",
            content_hash="hash-retry",
            title="Retry me",
            url="https://retry.example/1",
            publish_status="pending",
            tenant_id="default",
            created_at="2026-01-01T00:00:00.000000Z",
            updated_at="2026-01-01T00:00:00.000000Z",
        )
    )
    session.commit()
    backfill_chain(session, source_id="src-retry")
    session.commit()

    row_sessions = sessionmaker(session.get_bind(), expire_on_commit=False)

    # Sweeps 1..MAX_ATTEMPTS: attempts 1..2 requeue; the MAX_ATTEMPTS-th
    # failure stops the row as failed. (MAX_ATTEMPTS counts *tries*.)
    from app.enrichment_worker import BACKOFF_BASE_SECONDS, MAX_ATTEMPTS

    assert BACKOFF_BASE_SECONDS > 0, "a zero base makes backoff no backoff"

    observed_delays: list[float] = []
    for attempt in range(MAX_ATTEMPTS - 1):
        before = datetime.now(UTC)
        report = sweep_queue(
            session,
            processors={"translate": NetworkFailure},
            row_session_factory=row_sessions,
            concurrency=1,
        )
        assert report["retried"] == 1, (attempt, report)
        row = session.execute(
            text(
                "select status, attempt_count, next_attempt_at from item_enrichment"
                " where item_id = 'retry-item'"
            )
        ).first()
        assert row is not None, "the retry row must exist"
        assert row[0] == "queued", row
        assert row[1] == attempt + 1

        # Exponential backoff is a real schedule, not just "requeued anyway".
        # The delay must be within a tolerance band of base * 2**attempt; a
        # purely relative bound would be vacuous when the base is zero, which
        # is exactly the mutation this guards.
        expected = BACKOFF_BASE_SECONDS * (2**attempt)
        scheduled = datetime.fromisoformat(row[2].replace("Z", "+00:00"))
        # `before` is taken before the sweep, so the delay carries the sweep's
        # own duration as well as the backoff -- and that duration is what load
        # moves. The lower bound is therefore exact (the schedule is at least
        # the computed backoff) and the upper one scales with it: a fixed +5s
        # was the flake, since the sweep can take longer than that on a busy
        # machine (measured 126.08s against a 120s backoff, issue #213).
        delay = (scheduled - before).total_seconds()
        slack = max(5.0, expected * 0.1)
        assert expected <= delay <= expected + slack, (attempt, delay, expected)
        observed_delays.append(delay)

        # Clear the schedule so the next sweep can claim the row immediately;
        # the backoff assertion above already captured the value.
        session.execute(text("update item_enrichment set next_attempt_at = null"))
        session.commit()

    # Doubling is the property, not merely "some non-zero wait": each attempt
    # waits about twice as long as the one before.
    assert len(observed_delays) == 2
    # 1.5 rather than 2.0: the same sweep-duration slack above rides on both
    # values, so a busy machine compresses the ratio (issue #213). Still far
    # from the 1.0 that "no doubling" would produce.
    assert observed_delays[1] >= observed_delays[0] * 1.5, observed_delays

    # The MAX_ATTEMPTS-th failure exceeds the retries: the row stops failed.
    report = sweep_queue(
        session,
        processors={"translate": NetworkFailure},
        row_session_factory=row_sessions,
        concurrency=1,
    )
    assert report["failed"] == 1, report
    row = session.execute(
        text(
            "select status, attempt_count, error_class from item_enrichment"
            " where item_id = 'retry-item'"
        )
    ).first()
    assert row is not None
    assert row[0] == "failed" and row[2] == "network"

    # auth failures skip retries entirely: a fresh row fails in one sweep.
    session.execute(
        text(
            "insert into item (id, source_id, guid, content_hash, title, url,"
            " publish_status, tenant_id, created_at, updated_at)"
            " values ('auth-item', 'src-retry', 'g-auth', 'hash-auth', 'A',"
            " 'https://retry.example/2', 'pending', 'default',"
            " '2026-01-01T00:00:00.000000Z', '2026-01-01T00:00:00.000000Z')"
        )
    )
    session.commit()
    backfill_chain(session, source_id="src-retry")
    session.commit()

    report = sweep_queue(
        session,
        processors={"translate": AuthFailure},
        row_session_factory=row_sessions,
        concurrency=1,
    )
    assert report["failed"] == 1, report
    row = session.execute(
        text(
            "select status, attempt_count from item_enrichment"
            " where item_id = 'auth-item'"
        )
    ).first()
    assert row is not None
    assert row[0] == "failed" and row[1] == 0, "auth must not retry"


@pytest.mark.integration
def test_a_chain_naming_an_unregistered_processor_is_refused(
    session: Session,
) -> None:
    """The deadlock guard: unknown plugin_id raises at config time.

    Before this guard, a chain naming a processor the deployment could not
    run queued rows the worker skipped silently - the item stayed pending
    forever with no diagnostic anywhere.
    """
    import json as jsonlib

    import pytest as _pytest

    from adapters.repository import parse_chain

    with _pytest.raises(ValueError, match="unregistered processor"):
        parse_chain(
            jsonlib.dumps([{"plugin_id": "no-such-processor", "kind": "translation"}]),
            target_lang="en",
            known_plugins=frozenset({"translate"}),
        )
    # The legit processor still parses.
    specs = parse_chain(
        jsonlib.dumps([{"plugin_id": "translate", "kind": "translation"}]),
        target_lang="en",
        known_plugins=frozenset({"translate"}),
    )
    assert specs[0].plugin_id == "translate"


# --------------------------------------------------------------------------
# Queueing a chain: content already in the target language
# --------------------------------------------------------------------------


def _translation_chain(lang: str = "zh") -> tuple[Any, ...]:
    from core.enrichment import EnrichmentKind, EnrichmentSpec

    return (
        EnrichmentSpec(
            plugin_id="translate", kind=EnrichmentKind.TRANSLATION, lang=lang
        ),
    )


def _draft(external_id: str, *, content: str, title: str | None = None) -> ItemDraft:
    """A draft whose title doubles as the test's lookup key."""
    return ItemDraft(
        source_id=SOURCE,
        external_id=external_id,
        title=title if title is not None else f"item-{external_id}",
        canonical_url=f"https://blog.example.com/{external_id}",
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
        content=content,
    )


def _publish_state(session: Session, marker: str) -> tuple[str, int]:
    """(publish_status, enrichment row count) for one stored item.

    Keyed on the item's title, which these tests set to a unique marker. The
    guid is a derived hash and never contains the id it was derived from, so
    matching on it would silently find nothing and (as this helper used to)
    fall back to "the most recent row" -- which passes for the wrong reason
    the moment a test stores two items.
    """
    row = session.execute(
        text(
            "select i.publish_status,"
            " (select count(*) from item_enrichment e where e.item_id = i.id)"
            " from item i where i.title = :marker"
        ),
        {"marker": marker},
    ).one_or_none()
    if row is None:
        raise AssertionError(f"no stored item titled {marker!r}")
    return str(row[0]), int(row[1])


@pytest.mark.integration
def test_content_already_in_the_target_language_is_not_queued(session: Session) -> None:
    """A Chinese post targeting Chinese is published, not queued-and-skipped.

    Queueing it and letting the processor skip it later still occupies a slot
    and still holds the gate until a worker gets there, so a Chinese timeline
    waits behind its own English posts to be told there is nothing to do.
    """
    from app.pipeline import normalise_and_store

    normalise_and_store(
        session,
        [
            _draft(
                "zh-1",
                content="<p>这是一段足够长的中文内容，描述了一些具体的技术取舍和设计考虑。</p>",
            )
        ],
        tenant_id=TENANT,
        chain=_translation_chain("zh"),
    )
    session.commit()

    status, enrichments = _publish_state(session, "item-zh-1")
    assert enrichments == 0, "an already-translated item must not occupy the queue"
    assert status == PublishStatus.READY.value


@pytest.mark.integration
def test_english_content_is_still_queued(session: Session) -> None:
    """The filter must not swallow the work it exists to protect."""
    from app.pipeline import normalise_and_store

    normalise_and_store(
        session,
        [
            _draft(
                "en-1",
                content="<p>"
                + "This is a reasonably long English sentence. " * 3
                + "</p>",
            )
        ],
        tenant_id=TENANT,
        chain=_translation_chain("zh"),
    )
    session.commit()

    status, enrichments = _publish_state(session, "item-en-1")
    assert enrichments == 1, "English text still needs its translation queued"
    assert status == PublishStatus.PENDING.value


@pytest.mark.integration
def test_a_non_language_spec_survives_the_filter(session: Session) -> None:
    """Only translations are judged: a future summary has no such case.

    A Chinese body must not take its summarisation down with it just because
    translation was unnecessary.
    """
    from app.pipeline import normalise_and_store
    from core.enrichment import EnrichmentKind, EnrichmentSpec

    chain = (
        EnrichmentSpec(
            plugin_id="translate", kind=EnrichmentKind.TRANSLATION, lang="zh"
        ),
        EnrichmentSpec(plugin_id="summarise", kind=EnrichmentKind.SUMMARY),
    )
    normalise_and_store(
        session,
        [
            _draft(
                "zh-2",
                content="<p>这同样是一段足够长的中文内容，用来验证非语言类产物不会被误删。</p>",
            )
        ],
        tenant_id=TENANT,
        chain=chain,
    )
    session.commit()

    status, enrichments = _publish_state(session, "item-zh-2")
    assert enrichments == 1, "the summary spec must still be queued"
    assert status == PublishStatus.PENDING.value


@pytest.mark.integration
def test_an_entity_encoded_chinese_body_is_not_queued(session: Session) -> None:
    """The enqueue-time check and the processor must extract text alike.

    They use the same function on purpose. A feed encoding its CJK as numeric
    character references would otherwise be judged differently by each -- the
    pipeline seeing ASCII and queueing a translation the processor then skips
    -- which is the queue-slot-wasted-on-nothing coupling the enqueue-time
    check exists to remove.
    """
    from app.pipeline import normalise_and_store

    encoded = "&#20013;" * 20
    normalise_and_store(
        session,
        [_draft("zh-entity", content=f"<p>{encoded} 这是一段中文内容。</p>")],
        tenant_id=TENANT,
        chain=_translation_chain("zh"),
    )
    session.commit()

    status, enrichments = _publish_state(session, "item-zh-entity")
    assert enrichments == 0, "an entity-encoded Chinese body must not be queued"
    assert status == PublishStatus.READY.value


class FakeTranslate:
    @staticmethod
    def process(item_ctx: Any, *, target_lang: str, store: Any) -> str:
        return str(store(f"<p>Translated: {item_ctx['title']}</p>"))


class FailingTranslate:
    @staticmethod
    def process(item_ctx: Any, *, target_lang: str, store: Any) -> str:
        from core.enums import ErrorClass
        from core.plugins.hooks import ProcessorError

        raise ProcessorError(ErrorClass.AUTH, "provider rejected the key")


class NetworkFailure:
    @staticmethod
    def process(item_ctx: Any, *, target_lang: str, store: Any) -> str:
        import httpx

        raise httpx.ConnectError("provider down")


class AuthFailure:
    @staticmethod
    def process(item_ctx: Any, *, target_lang: str, store: Any) -> str:
        from core.enums import ErrorClass as _EC
        from core.plugins.hooks import ProcessorError

        raise ProcessorError(_EC.AUTH, "bad key")
