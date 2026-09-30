"""Content-level discovery: a caller's addresses, fetched into one source.

Scenario register (normative source: `SCENARIOS.md`; issue #170; AC-172..175,
plus AC-168's endpoint form).

`POST /v1/sources/{id}:fetch-items` is the service side of "an agent enumerates
a site's history": the caller discovers *addresses* (which the deployment
cannot, without crawling the site), and the source's own collector fetches
them. Everything lands through the same write path as any collection.

The origin is a `MockTransport` over recorded payloads, and it records every
request it sees: two of the scenarios are about requests that must *not*
happen, and "the origin was never asked" is only observable at the origin.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.orm import Session, sessionmaker

from app.main import create_app, create_engine_for
from tests.migrated_schema import migrated_database

WRITE_TOKEN = "fetch-items-write-token"
SITE = "https://bear.example.com"
FEED_URL = f"{SITE}/blog/feed"
ARTICLE_URL = f"{SITE}/blog/archives/2379"
OFF_SITE_URL = "https://elsewhere.example/blog/archives/1"
REST_PATH = "/blog/wp-json/wp/v2/posts"
FIXTURES = Path(__file__).parents[2] / "src/plugins/collectors/rss/fixtures"

REST_CONTENT = (
    "<p>UAE で免許を更新するには政府指定の眼科で視力検査を受ける必要がある。"
    "日本だと両眼で 0.7 以上、UAE だと 0.5 見えれば良いらしい。</p>"
)
PAGE_HTML = (
    "<html><head><title>Page title - Bear Blog</title>"
    f'<link rel="canonical" href="{SITE}/blog/posts/page">'
    "</head><body><article><h1>Headline</h1><p>"
    + ("Body prose. " * 40)
    + "</p></article></body></html>"
)


def policy(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {"type": "fixed", "interval": "30m"}
    base.update(overrides)
    return base


class Origin:
    """A recorded origin: it answers the shapes this path fetches, or 404s."""

    def __init__(self) -> None:
        self.requests: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        if request.url.path == REST_PATH:
            post = {
                "id": 2379,
                "link": ARTICLE_URL,
                "guid": {"rendered": f"{SITE}/blog/?p=2379"},
                "title": {"rendered": "視力検査と昼食"},
                "content": {"rendered": REST_CONTENT},
                "date_gmt": "2026-07-27T15:00:00",
            }
            include = request.url.params.get("include")
            body = [post] if include in (None, "2379") else []
            return httpx.Response(
                200, json=body, headers={"content-type": "application/json"}
            )
        if request.url.path.startswith("/blog/posts/"):
            return httpx.Response(
                200, text=PAGE_HTML, headers={"content-type": "text/html"}
            )
        return httpx.Response(404, text="no such address")


@pytest.fixture
def session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    url = migrated_database(tmp_path, "fetch-items.db")
    engine = create_engine_for(url)
    try:
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        engine.dispose()


@pytest.fixture
def origin() -> Origin:
    return Origin()


@pytest.fixture
def app(
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    origin: Origin,
) -> Iterator[FastAPI]:
    monkeypatch.setenv("PEARSS_API_TOKEN", WRITE_TOKEN)
    monkeypatch.setenv("PEARSS_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("PEARSS_PLUGIN_DIR", str(tmp_path / "plugins"))
    application = create_app(session_factory)
    # The lifespan does not run under a bare ASGI transport, so the shared
    # client is installed by hand -- the same seam the trigger tests use.
    application.state.http_client = httpx.Client(
        transport=httpx.MockTransport(origin.handler)
    )
    try:
        yield application
    finally:
        application.state.http_client.close()
        del application.state.http_client


@pytest_asyncio.fixture
async def writer(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {WRITE_TOKEN}"},
    ) as opened:
        yield opened


async def _add_source(
    writer: httpx.AsyncClient, *, url: str = FEED_URL, kind: str = "rss"
) -> str:
    response = await writer.post(
        "/v1/sources",
        headers={"Idempotency-Key": f"src-{kind}-{abs(hash(url))}"},
        json={"url": url, "type": kind, "schedule_policy": policy()},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _fetch_items(
    writer: httpx.AsyncClient,
    source_id: str,
    urls: list[str],
    *,
    key: str = "fetch-items-key",
    **options: Any,
) -> httpx.Response:
    return await writer.post(
        f"/v1/sources/{source_id}:fetch-items",
        headers={"Idempotency-Key": key},
        json={"urls": urls, **options},
    )


async def _stored_urls(writer: httpx.AsyncClient) -> set[str]:
    response = await writer.get("/v1/items", params={"limit": 100})
    assert response.status_code == 200, response.text
    return {row["url"] for row in response.json()["data"]}


# --------------------------------------------------------------------------
# AC-172: same-origin addresses land in the source
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ac172_addresses_on_the_sources_host_are_stored(
    writer: httpx.AsyncClient,
) -> None:
    """AC-172: the list is fetched, each address reports action and id."""
    source_id = await _add_source(writer)

    response = await _fetch_items(writer, source_id, [ARTICLE_URL])

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["requested"] == 1
    assert payload["inserted"] == 1
    [result] = payload["results"]
    assert result["url"] == ARTICLE_URL
    assert result["action"] == "stored"
    assert result["item_id"]
    assert result["error_class"] is None

    # The article is a real item, in full: the REST body, not a summary.
    assert await _stored_urls(writer) == {ARTICLE_URL}


# --------------------------------------------------------------------------
# AC-173: an address off the source's host is refused, and never fetched
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ac173_an_address_on_another_host_is_refused_untouched(
    writer: httpx.AsyncClient, origin: Origin
) -> None:
    """AC-173: refused per address, with no request and no row.

    A caller's address list must not become requests against servers this
    source never agreed to talk to -- `source_id` is the provenance boundary
    (ADR 0022), and this is where it is enforced for addresses.
    """
    source_id = await _add_source(writer)
    origin.requests.clear()

    response = await _fetch_items(writer, source_id, [OFF_SITE_URL])

    assert response.status_code == 200, response.text
    [result] = response.json()["results"]
    assert result["action"] == "rejected"
    assert result["error_class"] == "structure_changed"
    assert result["item_id"] is None
    assert origin.requests == [], "a refused address must not be fetched"
    assert await _stored_urls(writer) == set()


# --------------------------------------------------------------------------
# AC-174: one bad address does not take the list down
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ac174_one_failing_address_leaves_the_others_intact(
    writer: httpx.AsyncClient,
) -> None:
    """AC-174: 200, the good address stored, the bad one classified."""
    source_id = await _add_source(writer)
    missing = f"{SITE}/blog/archives/999999"

    response = await _fetch_items(writer, source_id, [missing, ARTICLE_URL])

    assert response.status_code == 200, response.text
    payload = response.json()
    actions = {result["url"]: result for result in payload["results"]}
    assert actions[missing]["action"] == "failed"
    assert actions[missing]["error_class"]
    assert actions[ARTICLE_URL]["action"] == "stored"
    assert payload["inserted"] == 1
    assert await _stored_urls(writer) == {ARTICLE_URL}


# --------------------------------------------------------------------------
# AC-175: the caller's pacing is clamped, and a replay changes nothing
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ac175_pacing_is_clamped_to_the_servers_bounds(
    writer: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-175: the effective pacing is reported back, clamped and floored.

    A deployment's interval is a floor under whatever a caller asks for: it is
    the operator's statement about how hard this deployment may hit someone
    else's server, and a caller cannot talk it down.
    """
    monkeypatch.setenv("PEARSS_FETCH_MIN_INTERVAL", "5")
    source_id = await _add_source(writer)

    response = await _fetch_items(
        writer,
        source_id,
        [ARTICLE_URL],
        batch_size=999,
        min_interval_seconds=999,
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["batch_size"] == 50, "the batch ceiling"
    assert payload["min_interval_seconds"] == 60.0, "the interval ceiling"
    assert payload["deployment_min_interval_seconds"] == 5.0

    floored = await _fetch_items(
        writer,
        source_id,
        [ARTICLE_URL],
        key="fetch-items-floor",
        min_interval_seconds=0,
    )
    assert floored.status_code == 200, floored.text
    assert floored.json()["min_interval_seconds"] == 5.0, (
        "the deployment's interval is a floor under the caller's zero"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ac175_replaying_the_same_request_does_not_duplicate(
    writer: httpx.AsyncClient,
) -> None:
    """AC-175: the Idempotency-Key replays the answer without re-storing.

    The first call already stored the article; the replay must return the same
    report -- not insert a second row, and not even re-fetch.
    """
    source_id = await _add_source(writer)
    first = await _fetch_items(writer, source_id, [ARTICLE_URL])
    assert first.status_code == 200

    replay = await _fetch_items(writer, source_id, [ARTICLE_URL])

    assert replay.status_code == first.status_code
    assert replay.json() == first.json(), "a replay is the same answer"
    assert await _stored_urls(writer) == {ARTICLE_URL}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ac175_a_capability_the_collector_lacks_is_refused_before_fetching(
    writer: httpx.AsyncClient, origin: Origin
) -> None:
    """AC-168's endpoint form: no item capability, no request, no rows.

    An `agent` source has no collector at all, which is the extreme case of
    "this source cannot fetch an address". The refusal has to be the *source's*
    answer rather than a per-address failure, because nothing about the list
    could have made it work.
    """
    source_id = await _add_source(writer, url="agent://discovered", kind="agent")
    origin.requests.clear()

    response = await _fetch_items(writer, source_id, [ARTICLE_URL])

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["error"]["code"] == "unsupported_capability"
    assert origin.requests == []
    assert await _stored_urls(writer) == set()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_list_over_the_cap_is_refused_as_a_whole(
    writer: httpx.AsyncClient, origin: Origin
) -> None:
    """The request ceiling is one request's worth of work, refused up front."""
    source_id = await _add_source(writer)
    origin.requests.clear()

    response = await _fetch_items(
        writer, source_id, [f"{SITE}/blog/archives/{n}" for n in range(201)]
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["error"]["code"] == "batch_too_large"
    assert origin.requests == []


@pytest.mark.fixture
def test_the_rest_fixture_still_carries_a_guid() -> None:
    """The identity this path relies on is the one the fixture records.

    Cheap guard on the corpus: the WordPress REST payload is what proves
    AC-169's identity alignment, and a fixture trimmed back to the fields the
    *older* code read would quietly undo it.
    """
    posts = json.loads((FIXTURES / "wp_rest_posts.json").read_text(encoding="utf-8"))
    assert all(post["guid"]["rendered"].startswith(SITE) for post in posts)
