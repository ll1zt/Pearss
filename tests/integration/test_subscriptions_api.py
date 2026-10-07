"""`/v1/subscriptions`: the channel surface, and the legacy id it replaces.

Scenario register (normative source: the AC-343..344 row of SCENARIOS.md)

  AC-343 the new endpoints serve a subscription with its origin beside it
    Given a source created through the deprecated /v1/sources
    When  GET /v1/subscriptions and GET /v1/subscriptions/{id}
    Then  the payload carries the subscription's policy and the origin's facts
          as two halves, the list is one page plus an opaque cursor, and an id
          nothing carries is a 404 with its own code
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.orm import Session, sessionmaker

from app.main import create_app, create_engine_for
from tests.migrated_schema import migrated_database

pytestmark = pytest.mark.integration

WRITE_TOKEN = "subscriptions-write-token"
READ_TOKEN = "subscriptions-read-token"
FEED_URL = "https://subscriptions.example/feed"


@pytest.fixture
def session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    url = migrated_database(tmp_path, "subs.db")
    engine = create_engine_for(url)
    try:
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        engine.dispose()


@pytest.fixture
def app(
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> FastAPI:
    monkeypatch.setenv("PEARSS_API_TOKEN", WRITE_TOKEN)
    monkeypatch.setenv("PEARSS_API_READ_TOKEN", READ_TOKEN)
    monkeypatch.setenv("PEARSS_STORAGE_DIR", str(tmp_path / "storage"))
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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_subscription_is_served_with_its_origin_beside_it(
    writer: httpx.AsyncClient, reader: httpx.AsyncClient
) -> None:
    """AC-343: the two halves, and the 1:1 ids a client already holds."""
    created = await writer.post(
        "/v1/sources",
        headers={"Idempotency-Key": "subs-1"},
        json={
            "url": FEED_URL,
            "schedule_policy": {"type": "fixed", "interval": "30m"},
            "folder": "Tech",
        },
    )
    assert created.status_code == 201, created.text
    source_id = created.json()["id"]

    listed = await reader.get("/v1/subscriptions", params={"limit": 1})
    assert listed.status_code == 200, listed.text
    payload = listed.json()
    assert [row["id"] for row in payload["data"]] == [source_id]
    # One page plus an opaque cursor: the response does not grow with the
    # deployment.
    assert payload["next_cursor"] is None

    one = await reader.get(f"/v1/subscriptions/{source_id}")
    assert one.status_code == 200, one.text
    body = one.json()
    # The subscription's identity and policy...
    assert body["id"] == source_id
    assert body["policy"]["folder"] == "Tech"
    assert body["policy"]["schedule_policy"] == {"type": "fixed", "interval": "30m"}
    assert body["policy"]["status"] == "active"
    # ...with the origin's facts beside them, not flattened into it.
    assert body["origin"]["id"] == source_id
    assert body["origin"]["canonical_url"] == FEED_URL
    assert body["origin"]["type"] == "rss"

    missing = await reader.get("/v1/subscriptions/no-such-subscription")
    assert missing.status_code == 404, missing.text
    assert missing.json()["detail"]["error"]["code"] == "subscription_not_found"


async def _create_source(writer: httpx.AsyncClient, *, key: str) -> str:
    created = await writer.post(
        "/v1/sources",
        headers={"Idempotency-Key": key},
        json={
            "url": FEED_URL,
            "schedule_policy": {"type": "fixed", "interval": "30m"},
            "folder": "Tech",
        },
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_adding_a_subscription_creates_a_second_channel_over_one_origin(
    writer: httpx.AsyncClient, reader: httpx.AsyncClient
) -> None:
    """Issue #371: two channels, one set of origin facts."""
    origin_id = await _create_source(writer, key="add-sub-1")

    added = await writer.post(
        f"/v1/origins/{origin_id}/subscriptions",
        headers={"Idempotency-Key": "add-sub-2"},
        json={
            "schedule_policy": {"type": "fixed", "interval": "1h"},
            "folder": "Translations",
            "pipeline_chain": [
                {"plugin_id": "translate", "kind": "translation", "lang": "zh"}
            ],
            "target_lang": "zh",
        },
    )
    assert added.status_code == 201, added.text
    body = added.json()
    # The new subscription carries its own policy...
    assert body["policy"]["folder"] == "Translations"
    assert body["policy"]["schedule_policy"] == {"type": "fixed", "interval": "1h"}
    assert body["policy"]["target_lang"] == "zh"
    # ...and the origin's facts are the same ones, not a second origin.
    assert body["origin"]["id"] == origin_id
    assert body["origin"]["canonical_url"] == FEED_URL
    assert body["id"] != origin_id, "a second channel needs its own identity"
    assert body["correlation_id"]

    # The new channel reads back as itself, not as the one it shares an
    # origin with: the id is derived, so nothing equals the origin id here.
    one = await reader.get(f"/v1/subscriptions/{body['id']}")
    assert one.status_code == 200, one.text
    assert one.json()["policy"]["folder"] == "Translations"
    assert one.json()["origin"]["canonical_url"] == FEED_URL

    listed = await reader.get("/v1/subscriptions", params={"limit": 10})
    assert sorted(row["origin"]["id"] for row in listed.json()["data"]) == [
        origin_id,
        origin_id,
    ]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_adding_the_same_subscription_twice_returns_the_first_one(
    writer: httpx.AsyncClient, reader: httpx.AsyncClient
) -> None:
    """The identity is (origin, folder), so a repeat is not a second channel."""
    origin_id = await _create_source(writer, key="add-sub-3")
    body = {
        "schedule_policy": {"type": "fixed", "interval": "30m"},
        "folder": "Translations",
    }
    first = await writer.post(
        f"/v1/origins/{origin_id}/subscriptions",
        headers={"Idempotency-Key": "add-sub-4"},
        json=body,
    )
    second = await writer.post(
        f"/v1/origins/{origin_id}/subscriptions",
        headers={"Idempotency-Key": "add-sub-5"},
        json=body,
    )
    assert first.status_code == second.status_code == 201, second.text
    assert second.json()["id"] == first.json()["id"]

    listed = await reader.get("/v1/subscriptions", params={"limit": 10})
    assert [row["id"] for row in listed.json()["data"]] == [
        origin_id,
        first.json()["id"],
    ], "the repeat must not have added a row"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_subscription_for_an_unknown_origin_is_rejected_and_stores_nothing(
    writer: httpx.AsyncClient, reader: httpx.AsyncClient
) -> None:
    """A refused write leaves no row behind."""
    refused = await writer.post(
        "/v1/origins/no-such-origin/subscriptions",
        headers={"Idempotency-Key": "add-sub-6"},
        json={
            "schedule_policy": {"type": "fixed", "interval": "30m"},
            "folder": "Translations",
        },
    )
    assert refused.status_code == 404, refused.text
    assert refused.json()["detail"]["error"]["code"] == "origin_not_found"

    listed = await reader.get("/v1/subscriptions", params={"limit": 10})
    assert listed.json()["data"] == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_unusable_policy_is_rejected_and_stores_nothing(
    writer: httpx.AsyncClient, reader: httpx.AsyncClient
) -> None:
    origin_id = await _create_source(writer, key="add-sub-7")
    refused = await writer.post(
        f"/v1/origins/{origin_id}/subscriptions",
        headers={"Idempotency-Key": "add-sub-8"},
        json={"schedule_policy": {"type": "hourly-ish"}, "folder": "Translations"},
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["error"]["code"] == "invalid_schedule_policy"

    listed = await reader.get("/v1/subscriptions", params={"limit": 10})
    assert [row["id"] for row in listed.json()["data"]] == [origin_id]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_chain_naming_an_unknown_plugin_is_rejected_and_stores_nothing(
    writer: httpx.AsyncClient, reader: httpx.AsyncClient
) -> None:
    origin_id = await _create_source(writer, key="add-sub-9")
    refused = await writer.post(
        f"/v1/origins/{origin_id}/subscriptions",
        headers={"Idempotency-Key": "add-sub-10"},
        json={
            "schedule_policy": {"type": "fixed", "interval": "30m"},
            "folder": "Translations",
            "pipeline_chain": [{"plugin_id": "no-such-plugin", "kind": "translation"}],
        },
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["error"]["code"] == "invalid_pipeline_chain"

    listed = await reader.get("/v1/subscriptions", params={"limit": 10})
    assert [row["id"] for row in listed.json()["data"]] == [origin_id]
