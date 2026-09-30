"""The batch source endpoint: many URLs, one request, per-item outcomes.

Scenario register (normative source: SCENARIOS.md; issue #159; AC-161..166).

`POST /v1/sources` answers one URL. Subscribing a list through it is a loop,
and through MCP a loop of confirmations (one per write). This endpoint is the
batch primitive ADR 0044 asks for: shared options, one Idempotency-Key, and
per-item results -- so a URL that cannot be used fails alone instead of taking
the rest of the list down with it.
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

WRITE_TOKEN = "batch-write-token"
BATCH_LIMIT = 50


def policy(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {"type": "fixed", "interval": "30m"}
    base.update(overrides)
    return base


def feed(name: str) -> str:
    return f"https://feeds.example/{name}.xml"


@pytest.fixture
def session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    url = migrated_database(tmp_path, "batch.db")
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


async def _batch(
    writer: httpx.AsyncClient,
    urls: list[str],
    *,
    key: str = "batch-key",
    **options: object,
) -> httpx.Response:
    return await writer.post(
        "/v1/sources:batch",
        headers={"Idempotency-Key": key},
        json={"urls": urls, "schedule_policy": policy(), **options},
    )


async def _subscribed_urls(writer: httpx.AsyncClient) -> set[str]:
    response = await writer.get("/v1/sources", params={"limit": 100})
    assert response.status_code == 200
    return {row["url"] for row in response.json()["data"]}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_batch_creates_every_url_and_reports_each_item(
    writer: httpx.AsyncClient,
) -> None:
    """AC-161 (API half): one request subscribes the list, one result per URL."""
    urls = [feed("a"), feed("b"), feed("c")]

    response = await _batch(writer, urls)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["created"] == 3
    assert payload["failed"] == 0
    assert [item["url"] for item in payload["results"]] == urls
    assert {item["status"] for item in payload["results"]} == {"created"}
    assert await _subscribed_urls(writer) == set(urls)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_bad_url_fails_alone_and_the_others_are_created(
    writer: httpx.AsyncClient,
) -> None:
    """AC-163: one unusable URL must not discard the rest of the list."""
    good_a, bad, good_b = feed("ok-a"), "ftp://feeds.example/nope.xml", feed("ok-b")

    response = await _batch(writer, [good_a, bad, good_b])

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["created"] == 2
    assert payload["failed"] == 1
    by_url = {item["url"]: item for item in payload["results"]}
    assert by_url[bad]["status"] == "failed"
    # A classification from the existing taxonomy, not a new string.
    assert by_url[bad]["error_class"] == "parse"
    assert by_url[good_a]["status"] == "created"
    assert await _subscribed_urls(writer) == {good_a, good_b}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_already_subscribed_url_is_reported_as_existing(
    writer: httpx.AsyncClient,
) -> None:
    """The endpoint keeps the single endpoint's idempotent meaning, per item."""
    url = feed("already")
    await _batch(writer, [url], key="first")

    response = await _batch(writer, [url, feed("new")], key="second")

    payload = response.json()
    by_url = {item["url"]: item for item in payload["results"]}
    assert by_url[url]["status"] == "existing"
    assert payload["created"] == 1
    assert await _subscribed_urls(writer) == {url, feed("new")}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_replaying_the_same_key_returns_the_same_batch(
    writer: httpx.AsyncClient,
) -> None:
    """AC-164: a retry with the same key replays, and creates nothing new."""
    urls = [feed("r1"), feed("r2")]

    first = await _batch(writer, urls, key="replay")
    second = await _batch(writer, urls, key="replay")

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.json() == first.json()
    assert second.headers.get("idempotent-replay") == "true"
    assert await _subscribed_urls(writer) == set(urls)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_more_urls_than_the_cap_are_refused_before_anything_is_created(
    writer: httpx.AsyncClient,
) -> None:
    """AC-165: over the cap is one refusal, never a partial batch."""
    urls = [feed(f"cap-{index}") for index in range(BATCH_LIMIT + 1)]

    response = await _batch(writer, urls, key="too-many")

    assert response.status_code == 422
    error = response.json()["detail"]["error"]
    assert error["code"] == "batch_too_large"
    assert error["suggestion"]
    assert await _subscribed_urls(writer) == set()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_invalid_shared_policy_is_refused_before_anything_is_created(
    writer: httpx.AsyncClient,
) -> None:
    """AC-165: shared options are validated exactly like the single endpoint."""
    response = await writer.post(
        "/v1/sources:batch",
        headers={"Idempotency-Key": "bad-policy"},
        json={"urls": [feed("p1")], "schedule_policy": {"type": "nonsense"}},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["error"]["code"] == "invalid_source"
    assert await _subscribed_urls(writer) == set()
