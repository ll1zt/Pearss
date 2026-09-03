"""Tests for the FastAPI application.

Uses httpx's ASGI transport so requests exercise the real app without binding
a port. The session factory is injected so each test gets a temporary
database.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from adapters.raw_payload import RawPayload
from adapters.repository import store_item
from app.main import create_app
from core.normalize import normalize
from plugins.collectors import rss

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "src/plugins/collectors/rss/fixtures"
TENANT = "default"


@pytest.fixture
def session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    db_path = tmp_path / "app.db"
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{db_path}")
    command.upgrade(config, "head")

    engine = create_engine(f"sqlite+pysqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "insert into source (id, type, schedule_policy, status, tenant_id,"
                " created_at, updated_at) values ('src-1', 'rss', '{}', 'active',"
                " 'default', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
            )
        )
    try:
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        engine.dispose()


@pytest.fixture
def app(session_factory: sessionmaker[Session]) -> FastAPI:
    return create_app(session_factory)


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """An async client bound straight to the ASGI app.

    ASGITransport is async-only, so a sync httpx.Client cannot use it. Binding
    to the app rather than a port keeps requests in-process, and using the
    async client means a blocking handler would show up as serialised latency
    instead of being masked by a test client.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as opened:
        yield opened


def draft_payload() -> dict[str, object]:
    return {
        "source_id": "src-1",
        "external_id": "entry-api-1",
        "title": "Posted Through The API",
        "canonical_url": "https://blog.example.com/2026/01/posted?utm_source=x",
        "published_at": None,
        "content": "<p>Posted body.</p>",
    }


# --------------------------------------------------------------------------
# Health and feeds
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_healthz_reports_ok(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_empty_feed_is_valid_rss(client: httpx.AsyncClient) -> None:
    response = await client.get("/feeds/default.xml")
    assert response.status_code == 200
    assert "application/rss+xml" in response.headers["content-type"]
    assert "<rss" in response.text
    assert "<item>" not in response.text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unknown_feed_is_404(client: httpx.AsyncClient) -> None:
    assert (await client.get("/feeds/nonexistent.xml")).status_code == 404


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ingested_item_appears_in_the_feed(client: httpx.AsyncClient) -> None:
    created = await client.post("/internal/items", json=draft_payload())
    assert created.status_code == 200
    guid = created.json()["guid"]
    assert created.json()["action"] == "insert"

    feed = await client.get("/feeds/default.xml")
    assert guid in feed.text
    # Tracking parameter stripped during normalisation.
    assert "utm_source" not in feed.text
    assert "https://blog.example.com/2026/01/posted" in feed.text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ingest_is_idempotent(client: httpx.AsyncClient) -> None:
    first = (await client.post("/internal/items", json=draft_payload())).json()
    second = (await client.post("/internal/items", json=draft_payload())).json()

    assert first["guid"] == second["guid"]
    assert second["action"] == "skip_same_source"

    feed = await client.get("/feeds/default.xml")
    assert feed.text.count("<item>") == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pending_items_never_appear(
    client: httpx.AsyncClient, session_factory: sessionmaker[Session]
) -> None:
    # Store an item directly, bypassing the endpoint's promotion, so it stays
    # pending: the feed must omit it even though a row exists.
    drafts = rss.parse(
        RawPayload(
            url="https://blog.example.com/feed",
            status_code=200,
            body=(FIXTURES / "valid.xml").read_bytes(),
            source_id="src-1",
        )
    )
    items = [normalize(draft, tenant_id=TENANT).item for draft in drafts]

    with session_factory() as session:
        for item in items:
            store_item(session, item)
        session.commit()

    feed = await client.get("/feeds/default.xml")
    for item in items:
        assert item.guid not in feed.text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_feed_leaks_no_internal_ids(client: httpx.AsyncClient) -> None:
    created = (await client.post("/internal/items", json=draft_payload())).json()
    feed = await client.get("/feeds/default.xml")

    # The internal row id and the public guid are different values, and only
    # the guid belongs in the feed.
    assert created["id"] not in feed.text
    assert created["guid"] in feed.text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_openapi_is_served(client: httpx.AsyncClient) -> None:
    # Swagger is the documented fallback admin UI, so it must not regress.
    assert (await client.get("/docs")).status_code == 200
    assert (await client.get("/openapi.json")).status_code == 200


# --------------------------------------------------------------------------
# Concurrency: the app must not block the event loop
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_feed_requests_do_not_serialise(app: FastAPI) -> None:
    """Ten concurrent requests must not take ten times as long as one.

    A blocking call inside an async handler would serialise them, which is
    invisible in a single-request test.
    """
    transport = httpx.ASGITransport(app=app)

    async def once(aclient: httpx.AsyncClient) -> None:
        await aclient.get("/feeds/default.xml")

    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as aclient:
        start = time.perf_counter()
        await asyncio.gather(*(once(aclient) for _ in range(10)))
        elapsed = time.perf_counter() - start

    # Generous bound: this is a smoke test for serialisation, not a benchmark.
    assert elapsed < 5.0, f"10 concurrent feed requests took {elapsed:.2f}s"
