"""A list tool must return the rows the caller asked for.

Scenario register (normative source: docs/api/mcp-tool-guide.md section 2
rule 2; issue #65).

  AC-41 调用方要多少行就能拿到多少行
    Given 某源下有 60 条条目
    When agent 调用 search_items(limit=100)
    Then 返回的行数等于实际匹配数 (60), 而不是被兜底砍成 20
    And 没有 truncated_items 标记

  AC-41b 兜底没有把分页变成谎言
    Given 同一批条目
    When 调用 search_items(limit=5)
    Then 返回 5 行 (调用方的 limit 生效)
    And 也没有 truncated_items 标记 —— 5 是请求量, 不是截断

`_truncate` had a global `MAX_LIST_ITEMS = 20` that took the first 20 items of
**any** list at any depth, and every list tool ends with `_truncate`. So the
tool's own `limit` was silently overruled: `/v1` returned the 100 rows that were
asked for, and the wrapper cut them to 20 afterwards. The caller could not tell
the difference between "there are only 20" and "20 of them were shown".

The fallback's job is to stop an unexpected nested array from eating the
context, not to overrule an explicit page request -- which is what AC-42 in
`tests/unit/test_mcp_list_truncation.py` pins from the other side.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
import pytest
from mcp.client._transport import Transport
from mcp.client.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult
from sqlalchemy.orm import sessionmaker

from adapters.repository import create_source, timestamp
from app.main import create_app, create_engine_for
from app.pipeline import normalise_and_store
from core.models import ItemDraft
from tests.integration.conftest import ServerInThread, free_port
from tests.migrated_schema import migrated_database

pytestmark = pytest.mark.integration

MCP_EDGE_TOKEN = "list-truncation-edge-token"
API_TOKEN = "list-truncation-api-token"

# Past the old fallback (20) and below the ceiling (100), so "did the caller get
# what it asked for" is unambiguous.
SEEDED_ITEMS = 60


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    root = tmp_path_factory.mktemp("listtrunc")
    url = migrated_database(root, "listtrunc.db")
    return {
        "PEARSS_DATABASE_URL": url,
        "PEARSS_STORAGE_DIR": str(root / "storage"),
        "PEARSS_API_TOKEN": API_TOKEN,
        "PEARSS_MCP_TOKEN": MCP_EDGE_TOKEN,
        "PEARSS_SCHEDULER_ENABLED": "0",
    }


@pytest.fixture(scope="module")
def seeded(env: dict[str, str]) -> str:
    """One source with 60 ready items, through the production write path."""
    engine = create_engine_for(env["PEARSS_DATABASE_URL"])
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        with factory() as seed:
            source = create_source(
                seed,
                source_type="rss",
                url="https://many.example/feed",
                canonical_url="https://many.example/feed",
                schedule_policy="{}",
                tenant_id="default",
                at=timestamp(datetime.now(UTC)),
            )
            published = datetime(2026, 1, 1, tzinfo=UTC)
            normalise_and_store(
                seed,
                [
                    ItemDraft(
                        source_id=source.id,
                        external_id=f"many-{index:03d}",
                        title=f"Item {index:03d}",
                        canonical_url=f"https://many.example/post-{index:03d}",
                        published_at=published + timedelta(minutes=index),
                        content=f"<p>Body {index}</p>",
                    )
                    for index in range(SEEDED_ITEMS)
                ],
                tenant_id="default",
            )
            seed.commit()
            return source.id
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def base_url(env: dict[str, str]) -> Iterator[str]:
    previous = {key: os.environ.get(key) for key in env}
    os.environ.update(env)
    try:
        app = create_app()
        with ServerInThread(app, free_port()) as server:
            yield server.base_url("")
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@asynccontextmanager
async def mcp_session(url: str) -> AsyncIterator[Client]:
    http_client = cast(
        "Any",
        httpx.AsyncClient(
            headers={"authorization": f"Bearer {MCP_EDGE_TOKEN}"},
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
        ),
    )
    transport = streamable_http_client(url, http_client=http_client)
    client = Client(cast("Transport", transport), mode="2026-07-28")
    try:
        async with client:
            yield client
    finally:
        await http_client.aclose()


def _payload(result: CallToolResult) -> dict[str, Any]:
    for block in result.content:
        text_value = getattr(block, "text", None)
        if text_value:
            parsed: dict[str, Any] = json.loads(text_value)
            return parsed
    return {}


async def _call(url: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    async with mcp_session(url) as session:
        return _payload(await session.call_tool(tool, args))


# --------------------------------------------------------------------------
# AC-41: the requested page size is the returned page size
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_large_page_is_not_cut_down_to_the_fallback(
    base_url: str, seeded: str
) -> None:
    """AC-41: `limit=100` over 60 matching items returns all 60.

    The old fallback returned 20 rows plus a marker, so a caller that asked for
    a wide page -- precisely to filter it, or to see everything -- got a fifth
    of it and no way to ask for the rest.
    """
    payload = await _call(
        f"{base_url}/mcp",
        "search_items",
        {"source_id": seeded, "limit": 100, "fields": "id"},
    )

    assert len(payload["data"]) == SEEDED_ITEMS, len(payload["data"])
    assert not any("truncated_items" in row for row in payload["data"]), payload


@pytest.mark.asyncio
async def test_a_small_page_is_exactly_what_was_asked_for(
    base_url: str, seeded: str
) -> None:
    """AC-41b: `limit=5` returns 5, and says nothing about truncation.

    A page size is a request, not a truncation: marking it would tell the model
    it had received everything and then imply otherwise.
    """
    payload = await _call(
        f"{base_url}/mcp",
        "search_items",
        {"source_id": seeded, "limit": 5, "fields": "id"},
    )

    assert len(payload["data"]) == 5, payload
    assert not any("truncated_items" in row for row in payload["data"]), payload


@pytest.mark.asyncio
async def test_the_whole_page_reaches_the_model_through_json(
    base_url: str, seeded: str
) -> None:
    """The rows survive serialisation, counted where the model sees them.

    Counts are taken from the decoded content rather than from the tool's own
    view, so a wrapper that filtered rows on the way out would be caught.
    """
    async with mcp_session(f"{base_url}/mcp") as session:
        result = await session.call_tool(
            "search_items", {"source_id": seeded, "limit": 100, "fields": "id"}
        )
    text = result.content[0].text  # type: ignore[union-attr]
    decoded = json.loads(text)
    assert len(decoded["data"]) == SEEDED_ITEMS


# --------------------------------------------------------------------------
# The exemption is only safe because every `data` list is endpoint-bounded
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_list_route_refuses_a_limit_above_the_ceiling(
    base_url: str, seeded: str
) -> None:
    """The exemption in `_truncate` rests on this: `data` is bounded upstream.

    Exempting the result set from the presentation fallback is only sound while
    the endpoint the tool calls actually bounds it. `/v1/sources/{id}/enrichments`
    was the one list route with no bound at all (survivable only because the
    wrapper used to cut everything to 20), so this pins that it now refuses an
    out-of-range limit like the others.
    """
    async with httpx.AsyncClient(
        base_url=base_url,
        timeout=20.0,
        headers={"Authorization": f"Bearer {API_TOKEN}"},
    ) as client:
        response = await client.get(
            f"/v1/sources/{seeded}/enrichments", params={"limit": 1000}
        )
    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_the_enrichments_page_reports_whether_there_is_more(
    base_url: str, seeded: str
) -> None:
    """A bounded page announces itself: `total` and `next_cursor` are present.

    No enrichment rows are seeded here -- the point is the shape the agent gets
    back, which is what makes a short page legible instead of ambiguous.
    """
    async with httpx.AsyncClient(
        base_url=base_url,
        timeout=20.0,
        headers={"Authorization": f"Bearer {API_TOKEN}"},
    ) as client:
        response = await client.get(f"/v1/sources/{seeded}/enrichments")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 0
    assert body["next_cursor"] is None
    assert body["data"] == []
