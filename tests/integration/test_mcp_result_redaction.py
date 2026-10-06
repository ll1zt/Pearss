"""MCP tool results must not carry private feed credentials.

Scenario register (normative source: docs/api/mcp-tool-guide.md section 5,
rule 2; issue #63). Tools are `/v1` projections, and `/v1` deliberately echoes
the source URL as given, so the redaction rule the audit middleware already
enforces has to be enforced again on the way out of a tool.

  AC-36 列表工具不把私有 feed 的 token 交给模型
    Given 一个 url 含 ?token=<secret> 的 rss 源
    When agent 调用 list_sources
    Then 结果里不出现该 token 的值
    And 结果仍能看出这是一个带查询串的 URL (host 与 path 保留)

  AC-37 单条查询同样脱敏
    Given 同上的源
    When agent 调用 get_source(source_id)
    Then 结果里不出现该 token 的值

  AC-38 条目列表的默认字段不含 token
    Given 该源下有条目, 其 url 也带 ?token=<secret>
    When agent 调用 search_items (不传 fields)
    Then 结果里不出现该 token 的值

  AC-39 operator 视图不被削弱 (这是边界, 不是漏洞)
    Given 同上的源
    When operator 走 Core API 读 /v1/sources
    Then 仍能看到完整 URL

AC-39 is pinned as already-correct: the point of the fix is to narrow the
*agent* surface, and the operator path is supposed to keep the credential
visible (an operator who cannot see their own feed URL cannot debug it). A
redaction that leaked into `/v1` would pass AC-36..38 while breaking the
operator, which is exactly the regression this scenario exists to catch.

Seeded through `create_source` and `normalise_and_store` rather than INSERTs:
those are the production write paths, so a schema change breaks here the way
it would break collection rather than silently.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, cast

import httpx
import pytest
from mcp.client._transport import Transport
from mcp.client.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult
from sqlalchemy.orm import Session, sessionmaker

from adapters.repository import create_source, timestamp
from app.main import create_app, create_engine_for
from app.pipeline import normalise_and_store
from core.models import ItemDraft
from tests.integration.conftest import ServerInThread, free_port
from tests.migrated_schema import migrated_database

MCP_EDGE_TOKEN = "redaction-edge-token"
API_TOKEN = "redaction-api-token"

# The credential under test. Distinct from every other fixture value in this
# suite so a substring search for it can only be satisfied by this URL.
SECRET = "SUPERSECRETTOKEN123"
FEED_URL = f"https://feeder.example.com/private/abc123.xml?token={SECRET}"
ITEM_URL = f"https://feeder.example.com/private/article.html?token={SECRET}&id=7"

pytestmark = pytest.mark.integration


def _seed(factory: sessionmaker[Session]) -> str:
    """One rss source (token in its URL) and one item (token in its URL)."""
    with factory() as seed:
        source = create_source(
            seed,
            source_type="rss",
            url=FEED_URL,
            # The canonical key is what identity is compared on, and a
            # non-tracking query survives canonicalisation -- so the token is
            # genuinely stored in the dedupe key, not only in the echoed `url`.
            canonical_url=FEED_URL,
            schedule_policy="{}",
            tenant_id="default",
            at=timestamp(datetime.now(UTC)),
        )
        normalise_and_store(
            seed,
            [
                ItemDraft(
                    origin_id=source.id,
                    external_id="tokenised-1",
                    title="A post from a private feed",
                    canonical_url=ITEM_URL,
                    published_at=None,
                    content="<p>Body.</p>",
                )
            ],
            tenant_id="default",
        )
        seed.commit()
        return source.id


@pytest.fixture(scope="module")
def mcp_env(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    """A migrated database with the tokenised source, and the server env."""
    root = tmp_path_factory.mktemp("redaction")
    url = migrated_database(root, "redaction.db")
    engine = create_engine_for(url)
    try:
        _seed(sessionmaker(engine, expire_on_commit=False))
    finally:
        engine.dispose()
    return {
        "PEARSS_DATABASE_URL": url,
        "PEARSS_STORAGE_DIR": str(root / "storage"),
        "PEARSS_API_TOKEN": API_TOKEN,
        "PEARSS_MCP_TOKEN": MCP_EDGE_TOKEN,
        "PEARSS_SCHEDULER_ENABLED": "0",
    }


@pytest.fixture(scope="module")
def base_url(mcp_env: dict[str, str]) -> Iterator[str]:
    """Serve the app on an OS-assigned port (never a constant; see #37)."""
    previous = {key: os.environ.get(key) for key in mcp_env}
    os.environ.update(mcp_env)
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


@pytest.fixture(scope="module")
def mcp_url(base_url: str) -> str:
    return f"{base_url}/mcp"


@asynccontextmanager
async def mcp_session(url: str) -> AsyncIterator[Client]:
    """A connected modern-protocol client, opened and closed in one task.

    Local to this module on purpose (same reason as the conformance suite's):
    the transport enters an anyio cancel scope that the same task has to exit,
    and a cross-module helper would be a shared dependency between suites that
    are otherwise free to move.
    """
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
# AC-36..AC-38: the token never reaches the model
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_sources_does_not_hand_the_model_a_private_feed_token(
    mcp_url: str,
) -> None:
    """AC-36: the list tool is the widest, so it is the first leak to close."""
    payload = await _call(mcp_url, "list_sources", {"limit": 10})
    rendered = json.dumps(payload)
    assert SECRET not in rendered, payload
    # The shape survives: an agent must still be able to tell this is a feed
    # URL with a query, otherwise it cannot reason about it at all.
    row = next(
        entry for entry in payload["data"] if "feeder.example.com" in entry["url"]
    )
    assert row["url"].startswith("https://feeder.example.com/private/abc123.xml")
    assert "token=" in row["url"]


@pytest.mark.asyncio
async def test_get_source_does_not_hand_the_model_a_private_feed_token(
    mcp_url: str,
) -> None:
    """AC-37: same rule for the single-row read."""
    source_id = (await _call(mcp_url, "list_sources", {"limit": 10}))["data"][0]["id"]
    payload = await _call(mcp_url, "get_source", {"source_id": source_id})
    assert SECRET not in json.dumps(payload), payload


@pytest.mark.asyncio
async def test_search_items_default_fields_do_not_carry_a_token(
    mcp_url: str,
) -> None:
    """AC-38: the default `fields` includes `url`, which can be tokenised too."""
    payload = await _call(mcp_url, "search_items", {"limit": 10})
    assert payload["data"], payload
    assert SECRET not in json.dumps(payload), payload


# --------------------------------------------------------------------------
# AC-39: the fix narrows the agent surface, not the operator's
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_operator_still_sees_the_full_url_through_the_core_api(
    base_url: str,
) -> None:
    """AC-39 (already correct, pinned): redaction must stop at the MCP edge.

    An operator debugging a feed needs the exact URL they subscribed to,
    credential included -- that is why `/v1` echoes it as given. This asserts
    the boundary so a fix that simply redacted everywhere cannot pass.
    """
    async with httpx.AsyncClient(base_url=base_url, timeout=20.0) as client:
        response = await client.get(
            "/v1/sources",
            headers={"Authorization": f"Bearer {API_TOKEN}"},
        )
    assert response.status_code == 200, response.text
    assert SECRET in response.text, response.text
