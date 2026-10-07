"""Unsubscribing is not destroying (issue #373, ADR 0063).

Scenario register (normative source: the AC-216..217 row of SCENARIOS.md)

  AC-216 unsubscribing keeps the content
    Given a subscription with items
    When  it is unsubscribed
    Then  the items are still there (by id) and the item count is unchanged
    And   re-subscribing the same source shows them again, the same item ids

  AC-217 only an explicit, reasoned purge deletes content
    Given a subscription with items and their bodies
    When  it is unsubscribed
    Then  no body file is removed
    And   a purge without a reason is refused, and nothing is deleted
    And   the purge's audit row carries the reason
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy.orm import Session, sessionmaker

from adapters import filestore
from app.main import create_app, create_engine_for
from app.middleware import CorrelationAndAuditMiddleware
from app.pipeline import normalise_and_store
from core.models import ItemDraft
from tests.migrated_schema import migrated_database
from tests.source_seed import seed_source

pytestmark = pytest.mark.integration

WRITE_TOKEN = "unsub-write-token"
READ_TOKEN = "unsub-read-token"
FEED_URL = "https://unsubscribe.example/feed"


@pytest.fixture
def storage(tmp_path: Path) -> Path:
    return tmp_path / "storage"


@pytest.fixture
def session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    url = migrated_database(tmp_path, "unsub.db")
    engine = create_engine_for(url)
    try:
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        engine.dispose()


@pytest.fixture
def app(
    session_factory: sessionmaker[Session],
    storage: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> FastAPI:
    monkeypatch.setenv("PEARSS_API_TOKEN", WRITE_TOKEN)
    monkeypatch.setenv("PEARSS_API_READ_TOKEN", READ_TOKEN)
    monkeypatch.setenv("PEARSS_STORAGE_DIR", str(storage))
    return create_app(session_factory)


@pytest_asyncio.fixture
async def writer(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {WRITE_TOKEN}"},
    ) as opened:
        yield opened


@pytest_asyncio.fixture
async def reader(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {READ_TOKEN}"},
    ) as opened:
        yield opened


async def _create_source(writer: httpx.AsyncClient, *, key: str) -> str:
    created = await writer.post(
        "/v1/sources",
        headers={"Idempotency-Key": key},
        json={
            "url": FEED_URL,
            "schedule_policy": {"type": "fixed", "interval": "30m"},
        },
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


def _store_an_item(
    session_factory: sessionmaker[Session],
    *,
    origin_id: str,
    external_id: str = "one",
    body: str = "<p>A body worth keeping.</p>",
) -> tuple[str, str]:
    """A visible item with a body on disk. Chainless, so the gate is open."""
    with session_factory() as session:
        stored = normalise_and_store(
            session,
            [
                ItemDraft(
                    origin_id=origin_id,
                    external_id=external_id,
                    title="An article",
                    canonical_url=f"https://unsubscribe.example/{external_id}",
                    published_at=None,
                    content=body,
                )
            ],
        )[0]
        session.commit()
        # `outcome.item` is the stored copy: the pipeline attaches the body
        # reference after normalising, so `result.item` never carries one.
        return stored.result.item.id, str(stored.outcome.item.content_ref)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unsubscribing_keeps_the_content_and_stops_collecting(
    writer: httpx.AsyncClient,
    reader: httpx.AsyncClient,
    session_factory: sessionmaker[Session],
) -> None:
    """AC-216: the policy goes, the content stays, and the receipt says which."""
    subscription_id = await _create_source(writer, key="unsub-1")
    item_id, _ref = _store_an_item(session_factory, origin_id=subscription_id)
    assert [row["id"] for row in (await reader.get("/v1/items")).json()["data"]] == [
        item_id
    ]

    response = await writer.delete(
        f"/v1/subscriptions/{subscription_id}:unsubscribe",
        headers={"Idempotency-Key": "unsub-2"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["subscriptions"] == 1
    assert body["subscription_id"] == subscription_id
    assert body["correlation_id"]

    # The content is still there, by id and in the count...
    one = await reader.get(f"/v1/items/{item_id}")
    assert one.status_code == 200, one.text
    assert [row["id"] for row in (await reader.get("/v1/items")).json()["data"]] == [
        item_id
    ]
    # ...and the channel reports that it is not collecting any more.
    channel = await reader.get(f"/v1/subscriptions/{subscription_id}")
    assert channel.status_code == 200, channel.text
    assert channel.json()["policy"]["status"] == "disabled"

    # A replay is not a second unsubscribe.
    again = await writer.delete(
        f"/v1/subscriptions/{subscription_id}:unsubscribe",
        headers={"Idempotency-Key": "unsub-3"},
    )
    assert again.status_code == 200, again.text
    assert again.json()["subscriptions"] == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resubscribing_the_same_source_shows_the_same_items_again(
    writer: httpx.AsyncClient,
    reader: httpx.AsyncClient,
    session_factory: sessionmaker[Session],
) -> None:
    """AC-216, second half: the same source and the same item ids come back."""
    subscription_id = await _create_source(writer, key="unsub-4")
    item_id, _ref = _store_an_item(session_factory, origin_id=subscription_id)
    await writer.delete(
        f"/v1/subscriptions/{subscription_id}:unsubscribe",
        headers={"Idempotency-Key": "unsub-5"},
    )

    again = await writer.post(
        "/v1/sources",
        headers={"Idempotency-Key": "unsub-6"},
        json={
            "url": FEED_URL,
            "schedule_policy": {"type": "fixed", "interval": "30m"},
        },
    )
    assert again.status_code == 201, again.text
    assert again.json()["id"] == subscription_id, "the same source, not a new one"

    channel = await reader.get(f"/v1/subscriptions/{subscription_id}")
    assert channel.json()["policy"]["status"] == "active"
    assert [row["id"] for row in (await reader.get("/v1/items")).json()["data"]] == [
        item_id
    ]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unsubscribing_removes_no_body_file(
    writer: httpx.AsyncClient,
    session_factory: sessionmaker[Session],
    storage: Path,
) -> None:
    """AC-217: the bodies are content, and only a purge touches content."""
    subscription_id = await _create_source(writer, key="unsub-7")
    _item_id, ref = _store_an_item(session_factory, origin_id=subscription_id)
    path = filestore.path_for(ref, root=storage)
    assert path is not None, "the body has to be on disk to be kept"
    assert path.exists()

    await writer.delete(
        f"/v1/subscriptions/{subscription_id}:unsubscribe",
        headers={"Idempotency-Key": "unsub-8"},
    )

    assert path.exists(), "unsubscribing is not deletion"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_purge_without_a_reason_is_refused_and_deletes_nothing(
    writer: httpx.AsyncClient,
    reader: httpx.AsyncClient,
    session_factory: sessionmaker[Session],
    storage: Path,
) -> None:
    """AC-217: the refusal has to leave the system untouched, not just answer."""
    origin_id = await _create_source(writer, key="purge-1")
    item_id, ref = _store_an_item(session_factory, origin_id=origin_id)

    refused = await writer.delete(
        f"/v1/origins/{origin_id}:purge",
        headers={"Idempotency-Key": "purge-2"},
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["error"]["code"] == "reason_required"

    assert (await reader.get(f"/v1/items/{item_id}")).status_code == 200
    kept = filestore.path_for(ref, root=storage)
    assert kept is not None and kept.exists()
    # A blank reason is not a reason.
    blank = await writer.delete(
        f"/v1/origins/{origin_id}:purge",
        params={"reason": "   "},
        headers={"Idempotency-Key": "purge-3"},
    )
    assert blank.status_code == 422, blank.text
    assert (await reader.get(f"/v1/items/{item_id}")).status_code == 200


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_reasoned_purge_deletes_the_content_and_the_audit_row_keeps_the_reason(
    writer: httpx.AsyncClient,
    reader: httpx.AsyncClient,
    session_factory: sessionmaker[Session],
    storage: Path,
    app: FastAPI,
) -> None:
    """AC-217: destruction is explicit, and the reason survives in the record."""
    origin_id = await _create_source(writer, key="purge-4")
    item_id, ref = _store_an_item(session_factory, origin_id=origin_id)

    purged = await writer.delete(
        f"/v1/origins/{origin_id}:purge",
        params={"reason": "the feed turned into a scraper"},
        headers={"Idempotency-Key": "purge-5"},
    )
    assert purged.status_code == 200, purged.text
    body = purged.json()
    assert body["sources"] == 1
    assert body["items"] == 1
    assert body["reason"] == "the feed turned into a scraper"

    assert (await reader.get(f"/v1/items/{item_id}")).status_code == 404
    gone = filestore.path_for(ref, root=storage)
    assert gone is None or not gone.exists()

    CorrelationAndAuditMiddleware.drain_audit()
    with session_factory() as session:
        row = session.execute(
            sa.text(
                "select args_redacted, operation from audit_log"
                " where operation like '%purge%'"
                " order by occurred_at desc"
            )
        ).first()
    assert row is not None, "a destructive write has to be audited"
    assert "the feed turned into a scraper" in row[0], row


@pytest.mark.integration
def test_the_reader_unsubscribe_stops_collecting_without_deleting(
    session_factory: sessionmaker[Session],
) -> None:
    """The Reader's `ac=unsubscribe` means the same thing (ADR 0063).

    It used to delete the source and everything it collected, which is what a
    client pressing "unsubscribe" least expects -- and what made the endpoint
    unusable as the Reader's whole notion of removing a feed.
    """
    from app.reader import operations

    with session_factory() as session:
        seed_source(session, source_id="src-reader", url=FEED_URL)
        session.commit()
    item_id, _ref = _store_an_item(
        session_factory, origin_id="src-reader", external_id="reader-one"
    )

    removed = operations.unsubscribe(session_factory, source_ids=["src-reader"])

    assert [row.source_id for row in removed] == ["src-reader"]
    assert removed[0].was_collected is True, "it had been collecting"
    with session_factory() as session:
        assert (
            session.execute(sa.text("select count(*) from item")).scalar_one() == 1
        ), "the Reader's unsubscribe must not delete content"
        assert (
            session.execute(
                sa.text("select status from subscription where id = 'src-reader'")
            ).scalar_one()
            == "disabled"
        )
        assert (
            session.execute(
                sa.text("select id from item where id = :id"), {"id": item_id}
            ).scalar_one()
            == item_id
        )


@pytest.mark.integration
def test_a_disabled_channel_disappears_from_every_read_view(
    session_factory: sessionmaker[Session],
) -> None:
    """AC-216's "again": while unsubscribed there is no channel to see.

    The items stay readable by id -- that is the other half of the scenario --
    but the *channel* is gone from the Reader's sidebar, from the folder list
    and from the RSS output, and comes back when the source is subscribed
    again. Without this the "again" in AC-216 would be vacuous.
    """
    from adapters.repository import (
        channel_for_subscription,
        resolve_channel,
        unsubscribe,
    )
    from app.reader import operations

    with session_factory() as session:
        seed_source(session, source_id="src-view", url=FEED_URL, folder="Tech")
        session.commit()

    with session_factory() as session:
        assert resolve_channel(session, channel_id="src-view", tenant_id="default")
        assert channel_for_subscription(
            session, subscription_id="src-view", tenant_id="default"
        )
    assert [row.id for row in operations.list_subscriptions(session_factory)] == [
        "src-view"
    ]
    assert operations.list_folder_names(session_factory) == ["Tech"]

    with session_factory() as session:
        assert unsubscribe(session, "src-view").unsubscribed is True
        session.commit()

    with session_factory() as session:
        assert (
            resolve_channel(session, channel_id="src-view", tenant_id="default") is None
        )
        assert (
            channel_for_subscription(
                session, subscription_id="src-view", tenant_id="default"
            )
            is None
        )
    assert operations.list_subscriptions(session_factory) == []
    assert operations.list_folder_names(session_factory) == []
