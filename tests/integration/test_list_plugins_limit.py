"""`limit` on the plugin list must bound the result, at both surfaces.

Scenario register (normative source: docs/api/mcp-tool-guide.md section 2
rule 2 and section 7; issue #68).

  AC-53 list_plugins 的 limit 真的起作用
    Given 注册了 5 个插件
    When agent 调用 list_plugins(limit=1)
    Then 返回的插件数不超过 1

  AC-54 分页是诚实的(不是静默截断)
    Given 注册了 5 个插件
    When 调用 /v1/plugins?limit=1
    Then 响应带 next_cursor, 用它取第二页能拿到不同的插件
    And 不带 limit 超上限时不被接受(校验生效)

AC-54 的措辞是刻意的: 一个只截断却不告知「还有更多」的 `limit` 仍然是静默截断,
换了个位置而已(这正是 #65/#66 记的那一类)。`next_cursor` 是「还有更多」的信号,
而且必须能被消费 —— 所以 MCP 侧也暴露 `cursor`, 否则 agent 看得到一个拿不到的游标,
就是又造了一个 #68 自己抱怨的那种旋钮。
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any, cast

import httpx
import pytest
from mcp.client._transport import Transport
from mcp.client.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult

from app.main import create_app, create_engine_for
from tests.integration.conftest import ServerInThread, free_port
from tests.migrated_schema import migrated_database

pytestmark = pytest.mark.integration

MCP_EDGE_TOKEN = "plugins-limit-edge-token"
API_TOKEN = "plugins-limit-api-token"

# The shipped collectors; the listing is registry-backed, so this is what the
# surface holds without installing anything.
REGISTERED = 5


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    root = tmp_path_factory.mktemp("pluginslimit")
    url = migrated_database(root, "pluginslimit.db")
    create_engine_for(url).dispose()
    return {
        "PEARSS_DATABASE_URL": url,
        "PEARSS_STORAGE_DIR": str(root / "storage"),
        "PEARSS_API_TOKEN": API_TOKEN,
        "PEARSS_MCP_TOKEN": MCP_EDGE_TOKEN,
        "PEARSS_SCHEDULER_ENABLED": "0",
    }


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


async def _call(mcp_url: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    async with mcp_session(mcp_url) as session:
        return _payload(await session.call_tool(tool, args))


def _api(base_url: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=base_url,
        timeout=20.0,
        headers={"Authorization": f"Bearer {API_TOKEN}"},
    )


# --------------------------------------------------------------------------
# AC-53: the knob the model sees actually does something
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_mcp_tool_honours_its_limit(base_url: str) -> None:
    """AC-53: `limit=1` returns at most one plugin.

    The tool declared `limit` in its schema, so the model can see it and will
    try to use it. A declared-but-ignored parameter is worse than none: the
    model retries, or falls back to a clumsier strategy, having been told the
    knob was real.
    """
    payload = await _call(f"{base_url}/mcp", "list_plugins", {"limit": 1})
    assert len(payload["data"]) <= 1, payload


@pytest.mark.asyncio
async def test_the_core_api_honours_its_limit(base_url: str) -> None:
    """AC-53 (the projection's source): the bound is enforced where it is real."""
    async with _api(base_url) as client:
        response = await client.get("/v1/plugins", params={"limit": 1})
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["data"]) <= 1, body


@pytest.mark.asyncio
async def test_a_limit_wider_than_the_surface_still_returns_everything(
    base_url: str,
) -> None:
    """The bound must not hide plugins when it is not the constraint.

    Guards the opposite failure: a paging bug that returns only the first page
    whatever was asked would pass the two tests above while losing plugins.
    """
    async with _api(base_url) as client:
        response = await client.get("/v1/plugins", params={"limit": 100})
    body = response.json()
    assert len(body["data"]) == REGISTERED, body
    assert body["next_cursor"] is None, body


# --------------------------------------------------------------------------
# AC-54: paging is honest, and its cursor is consumable
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_short_page_says_there_is_more_and_the_cursor_works(
    base_url: str,
) -> None:
    """AC-54: truncated means announced, and the announcement is actionable."""
    async with _api(base_url) as client:
        first = await client.get("/v1/plugins", params={"limit": 2})
        first_body = first.json()
        assert len(first_body["data"]) == 2, first_body
        cursor = first_body["next_cursor"]
        assert cursor, "a short page must say there is more, not truncate silently"

        second = await client.get("/v1/plugins", params={"limit": 2, "cursor": cursor})
        second_body = second.json()

    first_ids = {row["id"] for row in first_body["data"]}
    second_ids = {row["id"] for row in second_body["data"]}
    assert second_ids, second_body
    assert first_ids.isdisjoint(second_ids), (first_ids, second_ids)


@pytest.mark.asyncio
async def test_the_mcp_tool_can_consume_the_cursor_it_reports(base_url: str) -> None:
    """The cursor an agent is shown must be one it can actually use.

    `/v1` returning `next_cursor` while the tool cannot pass a `cursor` back
    would recreate this very issue one layer up: a visible knob that does
    nothing.
    """
    first = await _call(f"{base_url}/mcp", "list_plugins", {"limit": 2})
    cursor = first.get("next_cursor")
    assert cursor, first

    second = await _call(
        f"{base_url}/mcp", "list_plugins", {"limit": 2, "cursor": cursor}
    )
    first_ids = {row["id"] for row in first["data"]}
    second_ids = {row["id"] for row in second["data"]}
    assert second_ids, second
    assert first_ids.isdisjoint(second_ids), (first_ids, second_ids)


@pytest.mark.asyncio
async def test_a_limit_above_the_ceiling_is_refused(base_url: str) -> None:
    """An out-of-range bound is an error, not a silently clamped value.

    Clamping would make `limit=1000` look like it worked while returning 100,
    which is the same class of lie as ignoring the parameter.
    """
    async with _api(base_url) as client:
        response = await client.get("/v1/plugins", params={"limit": 1000})
    assert response.status_code == 422, response.text
