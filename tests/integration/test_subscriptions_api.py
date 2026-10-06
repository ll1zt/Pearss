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
