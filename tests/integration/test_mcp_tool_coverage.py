"""The MCP tools that no test had ever executed.

Scenario register (normative source: docs/api/mcp-tool-guide.md section 3;
files the #64 audit-id change touched). This exists because the coverage gate
flagged six tool bodies that had never been run by any test: `get_item`,
`list_jobs`, `update_schedule_policy`, `set_source_status`,
`set_source_folder` and `retry_enrichments`. They appeared in the surface
snapshot (`tools/list`) and in the annotation tests, but no test ever *called*
them -- so their request plumbing, their error mapping and their result
shaping were all unverified.

Each scenario drives the real MCP client over Streamable HTTP and asserts a
user-visible outcome -- the row a read returns, or the state a write leaves
behind -- rather than that some method was called.

  AC-47 get_item returns the one item's fields, and only the requested ones
    Given a source with one ready item
    When agent 调用 get_item(item_id, fields="id,title,url")
    Then 结果是该条目, 且只带请求的字段
    And 缺省 (不传 fields) 时带上 publish_status

  AC-48 list_jobs 报告已声明的采集作业
    Given 一个已配置的源 (其作业由订阅自动建立)
    When agent 调用 list_jobs
    Then 结果含该源的作业, 且 total 与 data 一致

  AC-49 update_schedule_policy 改排程并落库
    Given 一个已配置的源
    When agent 确认后调用 update_schedule_policy
    Then get_source 读回的排程是新策略
    And 结果带本次写入的 audit_id

  AC-50 set_source_status 暂停后恢复
    Given 一个 active 的源
    When agent 确认后调用 set_source_status("paused")
    Then get_source 读回 paused
    And 再确认调用 ("active") 后读回 active

  AC-51 set_source_folder 改分组并落库
    Given 一个源
    When agent 确认后调用 set_source_folder("阅读")
    Then get_source 读回该分组

  AC-52 retry_enrichments 对无失败行的源成功返回
    Given 一个源, 其处理链没有失败行
    When agent 确认后调用 retry_enrichments
    Then 返回 200 形状的结果, 带 audit_id

  AC-220 读状态在工具面上是一等公民, 且不碰条目事实 (#218)
    Given 一个 ready 条目
    When  agent 确认后调用 set_item_read_state(is_read=true)
    Then  get_item 同一次调用读回 is_read=true (读侧不新增工具)
    And   省略的字段保留原值 (只传 is_starred 时 is_read 仍为 true)
    And   条目自身列 (guid/content_hash/publish_status/content_ref) 不变
    And   一个变化都没点名的调用被拒 (empty_patch), 且不落库
    And   不存在的 item 被拒 (item_not_found), 且不落库
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
import sqlalchemy as sa
from mcp.client._transport import Transport
from mcp.client.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult
from sqlalchemy.orm import Session, sessionmaker

from adapters.repository import create_source, timestamp
from app.main import create_app, create_engine_for
from tests.integration.conftest import ServerInThread, free_port
from tests.migrated_schema import migrated_database

pytestmark = pytest.mark.integration

MCP_EDGE_TOKEN = "tool-coverage-edge-token"
API_TOKEN = "tool-coverage-api-token"


def _seed(factory: sessionmaker[Session], url: str) -> str:
    """One rss source and one ready item, through the production write paths."""
    from sqlalchemy import select as _select

    from adapters.db_models import Item as ItemRow
    from app.pipeline import normalise_and_store
    from core.enums import PublishStatus
    from core.models import ItemDraft

    with factory() as seed:
        source = create_source(
            seed,
            source_type="rss",
            url="https://tools.example/feed",
            canonical_url="https://tools.example/feed",
            schedule_policy="{}",
            tenant_id="default",
            at=timestamp(datetime.now(UTC)),
        )
        items = normalise_and_store(
            seed,
            [
                ItemDraft(
                    source_id=source.id,
                    external_id="covered-1",
                    title="A tool-coverage post",
                    canonical_url="https://tools.example/post-1",
                    published_at=None,
                    content="<p>Body.</p>",
                )
            ],
            tenant_id="default",
        )
        seed.commit()
        item_id = items[0].result.item.id

    # `normalise_and_store` leaves a chainless item ready already; assert the
    # assumption rather than hoping, so a future gate change fails loudly here.
    engine = create_engine_for(url)
    try:
        with engine.connect() as conn:
            status = conn.execute(
                _select(ItemRow.publish_status).where(ItemRow.id == item_id)
            ).scalar_one()
    finally:
        engine.dispose()
    assert status == PublishStatus.READY.value, status
    return source.id


@pytest.fixture(scope="module")
def mcp_env(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    root = tmp_path_factory.mktemp("toolcov")
    url = migrated_database(root, "toolcov.db")
    return {
        "PEARSS_DATABASE_URL": url,
        "PEARSS_STORAGE_DIR": str(root / "storage"),
        "PEARSS_API_TOKEN": API_TOKEN,
        "PEARSS_MCP_TOKEN": MCP_EDGE_TOKEN,
        "PEARSS_SCHEDULER_ENABLED": "0",
    }


@pytest.fixture(scope="module")
def base_url(mcp_env: dict[str, str]) -> Iterator[str]:
    previous = {key: os.environ.get(key) for key in mcp_env}
    os.environ.update(mcp_env)
    try:
        engine = create_engine_for(mcp_env["PEARSS_DATABASE_URL"])
        factory = sessionmaker(engine, expire_on_commit=False)
        try:
            _seed(factory, mcp_env["PEARSS_DATABASE_URL"])
        finally:
            engine.dispose()
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
async def mcp_session(
    url: str, *, elicitation_callback: Any = None
) -> AsyncIterator[Client]:
    """A connected client, opened and closed in one task (anyio requirement)."""
    kwargs = (
        {"elicitation_callback": elicitation_callback}
        if elicitation_callback is not None
        else {}
    )
    http_client = cast(
        "Any",
        httpx.AsyncClient(
            headers={"authorization": f"Bearer {MCP_EDGE_TOKEN}"},
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
        ),
    )
    transport = streamable_http_client(url, http_client=http_client)
    client = Client(cast("Transport", transport), mode="2026-07-28", **kwargs)
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


async def _call(
    url: str,
    tool: str,
    args: dict[str, Any],
    *,
    confirm: bool = False,
) -> dict[str, Any]:
    """Call a tool; `confirm` echoes the server's own prefilled defaults."""
    from mcp import types

    async def _auto_accept(_ctx: Any, params: Any) -> Any:
        properties = (params.requested_schema or {}).get("properties", {})
        content = {
            name: (
                spec.get("default", False)
                if spec.get("type") == "boolean"
                else spec.get("default", "")
            )
            for name, spec in properties.items()
        }
        return types.ElicitResult(action="accept", content=content)

    async with mcp_session(
        url, elicitation_callback=_auto_accept if confirm else None
    ) as session:
        return _payload(await session.call_tool(tool, args))


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_item_returns_the_requested_fields(mcp_url: str) -> None:
    """AC-47: one row, only the fields asked for, and the gate honoured."""
    listed = await _call(mcp_url, "search_items", {"limit": 5})
    item_id = listed["data"][0]["id"]

    narrow = await _call(
        mcp_url, "get_item", {"item_id": item_id, "fields": "id,title"}
    )
    assert narrow["id"] == item_id
    assert narrow["title"] == "A tool-coverage post"
    # Requested fields only: a wrapper that returned everything would defeat
    # the whole point of `fields`, which is what `search_items` advertises.
    assert set(narrow) == {"id", "title"}, narrow

    default = await _call(mcp_url, "get_item", {"item_id": item_id})
    assert default["publish_status"] == "ready"
    assert default["id"] == item_id


@pytest.mark.asyncio
async def test_get_item_reports_a_missing_item(mcp_url: str) -> None:
    """A missing id is an error an agent can branch on, not a 500."""
    payload = await _call(mcp_url, "get_item", {"item_id": "does-not-exist"})
    assert payload.get("error") == "item_not_found", payload


@pytest.mark.asyncio
async def test_list_jobs_reports_the_job_a_trigger_declares(mcp_url: str) -> None:
    """AC-48: a triggered collection leaves a job that list_jobs reports.

    A job exists only once a source has been collected (the declaration is
    made by the scheduler, not by subscribing), so this drives `trigger_job`
    first -- which is also the agent's own path to it.
    """
    source_id = (await _call(mcp_url, "list_sources", {"limit": 5}))["data"][0]["id"]
    await _call(mcp_url, "trigger_job", {"source_id": source_id}, confirm=True)

    payload = await _call(mcp_url, "list_jobs", {"limit": 10})
    assert isinstance(payload["data"], list), payload
    assert payload["total"] >= 1, payload
    assert payload["total"] >= len(payload["data"])
    job = next(row for row in payload["data"] if row["source_id"] == source_id)
    assert job["id"], job


# --------------------------------------------------------------------------
# Confirmed writes
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_schedule_policy_changes_the_stored_policy(
    mcp_url: str,
) -> None:
    """AC-49: the confirmed change is what the source now has."""
    source_id = (await _call(mcp_url, "list_sources", {"limit": 5}))["data"][0]["id"]

    result = await _call(
        mcp_url,
        "update_schedule_policy",
        {
            "source_id": source_id,
            "schedule_policy": {"type": "fixed", "interval": "3h"},
        },
        confirm=True,
    )
    assert result.get("declined") is None and result.get("aborted") is None, result

    source = await _call(mcp_url, "get_source", {"source_id": source_id})
    assert source["schedule_policy"] == {"type": "fixed", "interval": "3h"}, source
    # The write is attributable: the result names the audit row it produced.
    assert result["audit_id"], result


@pytest.mark.asyncio
async def test_set_source_status_pauses_and_resumes(mcp_url: str) -> None:
    """AC-50: pause, observe, resume -- each state visible through get_source."""
    source_id = (await _call(mcp_url, "list_sources", {"limit": 5}))["data"][0]["id"]

    await _call(
        mcp_url,
        "set_source_status",
        {"source_id": source_id, "status": "paused"},
        confirm=True,
    )
    source = await _call(mcp_url, "get_source", {"source_id": source_id})
    assert source["status"] == "paused", source

    await _call(
        mcp_url,
        "set_source_status",
        {"source_id": source_id, "status": "active"},
        confirm=True,
    )
    source = await _call(mcp_url, "get_source", {"source_id": source_id})
    assert source["status"] == "active", source


@pytest.mark.asyncio
async def test_set_source_folder_moves_the_source(mcp_url: str) -> None:
    """AC-51: the folder change is what a later read reports."""
    source_id = (await _call(mcp_url, "list_sources", {"limit": 5}))["data"][0]["id"]

    result = await _call(
        mcp_url,
        "set_source_folder",
        {"source_id": source_id, "folder": "阅读"},
        confirm=True,
    )
    assert result.get("declined") is None and result.get("aborted") is None, result

    source = await _call(mcp_url, "get_source", {"source_id": source_id})
    assert source["folder"] == "阅读", source
    assert result["audit_id"], result


@pytest.mark.asyncio
async def test_retry_enrichments_succeeds_with_nothing_to_requeue(
    mcp_url: str,
) -> None:
    """AC-52: requeueing on a source with no failed rows is a success, not a 500."""
    source_id = (await _call(mcp_url, "list_sources", {"limit": 5}))["data"][0]["id"]

    result = await _call(
        mcp_url, "retry_enrichments", {"source_id": source_id}, confirm=True
    )
    assert result.get("declined") is None and result.get("aborted") is None, result
    assert "error" not in result, result
    assert result["audit_id"], result


# --------------------------------------------------------------------------
# Consumption state (AC-220, issue #218)
# --------------------------------------------------------------------------


def _item_facts(url: str, item_id: str) -> tuple[Any, ...]:
    """The item's own columns -- the ones a read-state write must not touch.

    Read from the database rather than through the tool that writes them: a
    tool that stopped selecting one of them would hide the regression.
    """
    engine = create_engine_for(url)
    try:
        with engine.connect() as conn:
            return tuple(
                conn.execute(
                    sa.text(
                        "select guid, content_hash, publish_status, content_ref,"
                        " updated_at from item where id = :id"
                    ),
                    {"id": item_id},
                ).one()
            )
    finally:
        engine.dispose()


def _read_state_row(url: str, item_id: str) -> tuple[Any, ...] | None:
    engine = create_engine_for(url)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "select is_read, is_starred from read_state where item_id = :id"
                ),
                {"id": item_id},
            ).one_or_none()
        return None if row is None else tuple(row)
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_set_item_read_state_marks_read_and_leaves_the_item_alone(
    mcp_url: str, mcp_env: dict[str, str]
) -> None:
    """AC-220: the agent can mark read, and the item keeps every fact of its own.

    Both halves are asserted here because the capability is whole or absent.
    The read side: `get_item` answers "what is this item and what is my state
    with it" in one call -- the two are one user intent, and a second read tool
    would cost every conversation ~400 characters. The write side: consumption
    state is a row in a different table, so "mark it read" must leave `guid`,
    `content_hash`, `publish_status` and `content_ref` exactly as they were (a
    published item's content is frozen, ADR 0008).
    """
    url = mcp_env["PEARSS_DATABASE_URL"]
    item_id = (await _call(mcp_url, "search_items", {"limit": 5}))["data"][0]["id"]

    before = await _call(mcp_url, "get_item", {"item_id": item_id})
    assert before["is_read"] is False, before
    assert before["is_starred"] is False, before
    facts = _item_facts(url, item_id)
    assert facts[2] == "ready", facts

    written = await _call(
        mcp_url,
        "set_item_read_state",
        {"item_id": item_id, "is_read": True},
        confirm=True,
    )
    assert written["is_read"] is True, written
    assert written["audit_id"], written

    after = await _call(mcp_url, "get_item", {"item_id": item_id})
    assert after["is_read"] is True, after
    assert after["is_starred"] is False, after
    assert after["id"] == item_id and after["title"] == "A tool-coverage post"
    assert _item_facts(url, item_id) == facts
    assert _read_state_row(url, item_id) == (1, 0)

    # An omitted field keeps its value: starring does not un-read.
    starred = await _call(
        mcp_url,
        "set_item_read_state",
        {"item_id": item_id, "is_starred": True},
        confirm=True,
    )
    assert starred["is_starred"] is True, starred
    assert starred["is_read"] is True, starred
    assert _item_facts(url, item_id) == facts


@pytest.mark.asyncio
async def test_set_item_read_state_refuses_a_change_that_names_nothing(
    mcp_url: str, mcp_env: dict[str, str]
) -> None:
    """A call that names no change is refused before anyone is asked to confirm.

    The code is the one `/v1` answers with, so an agent branches on one
    vocabulary; it is raised before elicitation because a dialog about a no-op
    spends a human round trip on nothing. Nothing reaches the table either --
    the refusal is the whole outcome.
    """
    url = mcp_env["PEARSS_DATABASE_URL"]
    item_id = (await _call(mcp_url, "search_items", {"limit": 5}))["data"][0]["id"]
    rows_before = _read_state_row(url, item_id)

    payload = await _call(
        mcp_url, "set_item_read_state", {"item_id": item_id}, confirm=True
    )

    assert payload["error"] == "empty_patch", payload
    assert _read_state_row(url, item_id) == rows_before


@pytest.mark.asyncio
async def test_set_item_read_state_reports_a_missing_item(
    mcp_url: str, mcp_env: dict[str, str]
) -> None:
    """A state for an id nobody has is not a state: refused, and nothing written."""
    url = mcp_env["PEARSS_DATABASE_URL"]

    payload = await _call(
        mcp_url,
        "set_item_read_state",
        {"item_id": "does-not-exist", "is_read": True},
        confirm=True,
    )

    assert payload["error"] == "item_not_found", payload
    assert _read_state_row(url, "does-not-exist") is None
