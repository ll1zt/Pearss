"""Tests for the MCP tool surface.

Tools are driven through the real MCP client over Streamable HTTP rather than
by calling the functions, because the contract under test is the wire one:
schema generation, elicitation behaviour, and the fact that a tool reaches the
audit middleware the same way any other caller does.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult
from sqlalchemy import text
from uvicorn.config import Config as UvicornConfig
from uvicorn.server import Server

from app.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]
API_TOKEN = "mcp-test-token"
FEED_URL = "https://planetpython.org/rss20.xml"

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def mcp_env(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    """A migrated database and the environment the server runs with."""
    root = tmp_path_factory.mktemp("mcp")
    url = str(sa.URL.create("sqlite+pysqlite", database=str(root / "mcp.db")))
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")

    engine = sa.create_engine(url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "insert into source (id, type, url, canonical_url, schedule_policy,"
                " status, trust_level, tenant_id, created_at, updated_at)"
                " values ('src-1', 'rss', :u, :u, '{}', 'active', 'standard',"
                " 'default', '2026-01-01T00:00:00.000000Z',"
                " '2026-01-01T00:00:00.000000Z')"
            ),
            {"u": FEED_URL},
        )
        conn.execute(
            text(
                "insert into item (id, source_id, guid, content_hash, title, url,"
                " published_at, publish_status, tenant_id, created_at, updated_at,"
                " content_ref)"
                " values ('i-1', 'src-1', 'g-1', 'h-1', 'A searchable title',"
                " 'https://blog.example.com/1',"
                " '2026-06-01T10:00:00.000000Z', 'ready', 'default',"
                " '2026-06-01T10:00:00.000000Z',"
                " '2026-06-01T10:00:00.000000Z', 'cas://missing')"
            )
        )
        conn.execute(
            text(
                "insert into item (id, source_id, guid, content_hash, title, url,"
                " published_at, publish_status, tenant_id, created_at, updated_at)"
                " values ('i-pending', 'src-1', 'g-p', 'h-p', 'Hidden pending item',"
                " 'https://blog.example.com/pending',"
                " '2026-06-01T11:00:00.000000Z', 'pending', 'default',"
                " '2026-06-01T11:00:00.000000Z', '2026-06-01T11:00:00.000000Z')"
            )
        )
    engine.dispose()
    return {
        "PEARSS_DATABASE_URL": url,
        "PEARSS_STORAGE_DIR": str(root / "storage"),
        "PEARSS_API_TOKEN": API_TOKEN,
        "PEARSS_READER_PASSWORD": "reader-pw",
        "PEARSS_READER_USERNAME": "reader",
        "PEARSS_SCHEDULER_ENABLED": "0",
    }


class _ServerInThread:
    """The app served on its own thread and event loop.

    Keeping uvicorn off the test loop means the server's anyio cancel scopes
    are created and destroyed on one thread.
    """

    def __init__(self, app: Any, port: int) -> None:
        self._server = Server(
            UvicornConfig(app, host="127.0.0.1", port=port, log_level="critical")
        )
        self._port = port

    def __enter__(self) -> _ServerInThread:
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        # The lifespan must finish before the MCP session manager accepts
        # connections; polling the port is more reliable than a fixed sleep.
        for _ in range(120):
            if self._listening():
                break
            time.sleep(0.05)
        return self

    def _listening(self) -> bool:
        import socket

        with socket.socket() as probe:
            probe.settimeout(0.2)
            return probe.connect_ex(("127.0.0.1", self._port)) == 0

    def __exit__(self, *exc: object) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=15)


@pytest.fixture(scope="module")
def mcp_url(mcp_env: dict[str, str]) -> Iterator[str]:
    """Serve the app for the module and yield its MCP endpoint."""
    previous = {key: os.environ.get(key) for key in mcp_env}
    os.environ.update(mcp_env)
    try:
        app = create_app()
        with _ServerInThread(app, 8091):
            yield "http://127.0.0.1:8091/mcp/mcp"
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


async def _auto_accept(_ctx: Any, params: Any) -> Any:
    """Answer an elicitation by echoing the schema's prefilled defaults.

    The server seeds each field with the value it intends to execute, so
    returning the defaults is exactly "the user pressed confirm without
    editing". It also proves the prefill reaches the client: if it did not, the
    server's `_matches` check would abort and the write would not happen.
    """
    from mcp import types

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


@asynccontextmanager
async def mcp_session(
    url: str, *, elicitation_callback: Any = None
) -> AsyncIterator[ClientSession]:
    """A connected session, opened and closed inside the calling task.

    A helper rather than a fixture on purpose: the MCP transport enters an anyio
    cancel scope, and anyio requires the same task to exit it. An
    async-generator fixture tears down elsewhere and fails every test with
    "attempted to exit cancel scope in a different task".
    """
    kwargs = (
        {"elicitation_callback": elicitation_callback}
        if elicitation_callback is not None
        else {}
    )
    async with (
        streamable_http_client(url) as (read, write),
        ClientSession(read, write, **kwargs) as session,
    ):
        await session.initialize()
        yield session


def _payload(result: CallToolResult) -> dict[str, Any]:
    """Decode the first text content block of a tool result."""
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
    async with mcp_session(
        url, elicitation_callback=_auto_accept if confirm else None
    ) as session:
        return _payload(await session.call_tool(tool, args))


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_planned_tools_are_registered(mcp_url: str) -> None:
    async with mcp_session(mcp_url) as session:
        names = {tool.name for tool in (await session.list_tools()).tools}
    assert names == {
        "list_sources",
        "search_items",
        "get_system_snapshot",
        "list_jobs",
        "list_runs",
        "add_source",
        "update_schedule_policy",
        "set_source_status",
        "remove_source",
        "trigger_job",
    }


@pytest.mark.asyncio
async def test_every_tool_has_a_description(mcp_url: str) -> None:
    # Descriptions are part of the contract: an agent selects tools from them.
    async with mcp_session(mcp_url) as session:
        tools = (await session.list_tools()).tools
    for tool in tools:
        assert tool.description, tool.name


@pytest.mark.asyncio
async def test_item_resource_is_advertised_as_a_template(mcp_url: str) -> None:
    async with mcp_session(mcp_url) as session:
        templates = (await session.list_resource_templates()).resource_templates
    assert any("item://" in template.uri_template for template in templates)


# --------------------------------------------------------------------------
# Reads reach the Core API
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_sources_returns_rows_and_a_correlation_id(mcp_url: str) -> None:
    payload = await _call(mcp_url, "list_sources", {"limit": 10})
    assert [row["id"] for row in payload["data"]] == ["src-1"]
    # The correlation id proves the request went through the Core API, since
    # that is what mints it.
    assert payload["correlation_id"]


@pytest.mark.asyncio
async def test_search_items_returns_summaries_and_no_body(mcp_url: str) -> None:
    payload = await _call(mcp_url, "search_items", {"q": "searchable"})
    assert len(payload["data"]) == 1
    row = payload["data"][0]
    assert "title" in row
    # Bodies are Resources, never tool results.
    assert "content" not in row and "body" not in row
    assert "body_hint" in payload


@pytest.mark.asyncio
async def test_pending_items_are_invisible_to_the_tools(mcp_url: str) -> None:
    # The gate applies to the MCP surface as much as to RSS and the Reader API.
    payload = await _call(mcp_url, "search_items", {"limit": 50})
    ids = [row["id"] for row in payload["data"]]
    assert "i-pending" not in ids


@pytest.mark.asyncio
async def test_snapshot_reports_counts(mcp_url: str) -> None:
    payload = await _call(mcp_url, "get_system_snapshot", {})
    assert payload["sources_by_status"]["active"] == 1
    assert payload["items_by_publish_status"]["ready"] == 1
    assert payload["items_by_publish_status"]["pending"] == 1


@pytest.mark.asyncio
async def test_long_strings_are_truncated(mcp_url: str) -> None:
    payload = await _call(mcp_url, "list_sources", {"limit": 10})
    for row in payload["data"]:
        for value in row.values():
            if isinstance(value, str):
                assert len(value) <= 260, value


@pytest.mark.asyncio
async def test_error_codes_match_the_core_api(mcp_url: str) -> None:
    # Shared codes, so an agent seeing `job_not_found` from a tool and from the
    # REST API is seeing one fact rather than two vocabularies.
    payload = await _call(mcp_url, "list_runs", {"job_id": "nope"})
    assert payload["error"] == "job_not_found"
    assert payload["next_actions"]


# --------------------------------------------------------------------------
# Writes require confirmation
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_writes_decline_when_the_client_cannot_confirm(mcp_url: str) -> None:
    """The test client does not implement elicitation.

    The write must be refused rather than executed. A tool that proceeded when
    the client could not confirm would make the requirement decorative.
    """
    payload = await _call(mcp_url, "remove_source", {"source_id": "src-1"})
    assert payload.get("declined") is True
    assert "elicitation" in payload["reason"]

    # The refusal must be real: the source is still there.
    still = await _call(mcp_url, "list_sources", {"limit": 10})
    assert [row["id"] for row in still["data"]] == ["src-1"]


@pytest.mark.asyncio
async def test_every_write_tool_refuses_without_elicitation(mcp_url: str) -> None:
    cases: list[tuple[str, dict[str, Any]]] = [
        (
            "add_source",
            {
                "url": "https://x.example/feed",
                "schedule_policy": {"type": "fixed", "interval": "30m"},
            },
        ),
        (
            "update_schedule_policy",
            {
                "source_id": "src-1",
                "schedule_policy": {"type": "fixed", "interval": "1h"},
            },
        ),
        ("set_source_status", {"source_id": "src-1", "status": "paused"}),
        ("trigger_job", {"source_id": "src-1"}),
    ]
    for tool, args in cases:
        payload = await _call(mcp_url, tool, args)
        assert payload.get("declined") is True, tool

    # Nothing changed anywhere.
    sources = await _call(mcp_url, "list_sources", {"limit": 10})
    assert [row["status"] for row in sources["data"]] == ["active"]


# --------------------------------------------------------------------------
# Audit coverage
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refused_writes_reach_no_audit_row(
    mcp_url: str, mcp_env: dict[str, str]
) -> None:
    """A write declined at the MCP layer never reaches the Core API.

    This is the honest shape rather than the one first assumed here: the
    refusal happens before any HTTP call, so there is no Core API request to
    audit. Recording it would need the MCP layer to write AuditLog directly,
    which is a separate decision — and AGENTS.md asks for audit at the Core API
    covering every caller, not for the MCP layer to keep its own ledger.

    The consequence worth pinning is that reads and MCP-layer refusals produce
    no rows, while a confirmed write does (asserted by the next test), so the
    audit trail means exactly "something reached the API".
    """
    await _call(mcp_url, "list_sources", {"limit": 5})
    await _call(
        mcp_url,
        "add_source",
        {
            "url": "https://never-applied.example/feed",
            "schedule_policy": {"type": "fixed", "interval": "30m"},
        },
    )

    engine = sa.create_engine(mcp_env["PEARSS_DATABASE_URL"])
    with engine.connect() as conn:
        rows = conn.execute(text("select operation from audit_log")).fetchall()
    engine.dispose()

    assert not any("never-applied" in row[0] for row in rows), (
        "a declined write should not have reached the API"
    )


@pytest.mark.asyncio
async def test_confirmed_reads_and_writes_share_one_audit_path(
    mcp_url: str, mcp_env: dict[str, str]
) -> None:
    """A confirmed tool write is audited exactly like a REST call.

    This is the payoff of routing tools through /v1: the same middleware sees
    both, so there is one ledger rather than one per entry point.
    """
    await _call(
        mcp_url,
        "add_source",
        {
            "url": "https://shared-audit.example/feed.xml",
            "schedule_policy": {"type": "fixed", "interval": "30m"},
        },
        confirm=True,
    )
    engine = sa.create_engine(mcp_env["PEARSS_DATABASE_URL"])
    with engine.connect() as conn:
        rows = conn.execute(text("select actor, operation from audit_log")).fetchall()
    engine.dispose()
    assert any("POST /v1/sources" in op for _, op in rows)
    assert {actor for actor, _ in rows} <= {"api:full", "anonymous"}


# --------------------------------------------------------------------------
# The confirmed path: elicitation accepted, write executed, run audited
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirmed_write_reaches_the_core_api(mcp_url: str) -> None:
    """With a client that can confirm, the write must actually happen.

    Refusing without confirmation is only half the requirement; a tool that
    refused *with* confirmation would be unusable.
    """
    payload = await _call(
        mcp_url,
        "add_source",
        {
            "url": "https://confirmed.example/feed.xml",
            "schedule_policy": {"type": "fixed", "interval": "30m"},
        },
        confirm=True,
    )
    assert payload.get("declined") is None, payload
    assert payload.get("aborted") is None, payload
    assert payload["id"], payload
    assert payload["canonical_url"] == "https://confirmed.example/feed.xml"


@pytest.mark.asyncio
async def test_confirmed_trigger_records_a_run(mcp_url: str) -> None:
    """A confirmed trigger goes through collect_now, so a run exists.

    The endpoint used to call the pipeline helper directly, which collected the
    source and left no JobRun — invisible to list_runs and to the backoff that
    reads run history.
    """
    created = await _call(
        mcp_url,
        "add_source",
        {
            "url": "https://trig.example/feed.xml",
            "schedule_policy": {"type": "fixed", "interval": "30m"},
        },
        confirm=True,
    )
    result = await _call(
        mcp_url, "trigger_job", {"source_id": created["id"]}, confirm=True
    )
    assert result.get("declined") is None, result
    assert result["run_status"] in {"succeeded", "failed", "timed_out"}

    runs = await _call(mcp_url, "list_runs", {"job_id": f"job-{created['id']}"})
    assert runs.get("data"), "a confirmed trigger should leave a run behind"


@pytest.mark.asyncio
async def test_confirmation_that_does_not_match_the_request_is_aborted(
    mcp_url: str,
) -> None:
    """A confirmation of one thing must not authorise another.

    The client here accepts but returns a different url, which is what a
    tampered or buggy client would do.
    """

    async def _tampered(_ctx: Any, params: Any) -> Any:
        from mcp import types

        properties = (params.requested_schema or {}).get("properties", {})
        content = {
            name: (
                "https://evil.example/feed"
                if name == "url"
                else spec.get("default", False if spec.get("type") == "boolean" else "")
            )
            for name, spec in properties.items()
        }
        return types.ElicitResult(action="accept", content=content)

    async with mcp_session(mcp_url, elicitation_callback=_tampered) as session:
        result = await session.call_tool(
            "add_source",
            {
                "url": "https://honest.example/feed.xml",
                "schedule_policy": {"type": "fixed", "interval": "30m"},
            },
        )
    payload = _payload(result)
    assert payload.get("aborted") is True
    assert "differ" in payload["reason"]

    # Neither the honest nor the evil URL was added.
    sources = await _call(mcp_url, "list_sources", {"limit": 50})
    urls = [row["canonical_url"] for row in sources["data"]]
    assert "https://honest.example/feed.xml" not in urls
    assert "https://evil.example/feed" not in urls


@pytest.mark.asyncio
async def test_unchecking_the_confirmation_declines(mcp_url: str) -> None:
    """Accepting the dialog with `confirmed` false is a refusal.

    The checkbox exists so a user can decline without cancelling, and honouring
    it is the difference between a confirmation and a form field.
    """

    async def _unchecked(_ctx: Any, params: Any) -> Any:
        from mcp import types

        properties = (params.requested_schema or {}).get("properties", {})
        content = {
            name: (False if name == "confirmed" else spec.get("default", ""))
            for name, spec in properties.items()
        }
        return types.ElicitResult(action="accept", content=content)

    async with mcp_session(mcp_url, elicitation_callback=_unchecked) as session:
        result = await session.call_tool("remove_source", {"source_id": "src-1"})
    payload = _payload(result)
    assert payload.get("declined") is True

    still = await _call(mcp_url, "list_sources", {"limit": 10})
    assert "src-1" in [row["id"] for row in still["data"]]


# --------------------------------------------------------------------------
# Tool-surface snapshot
# --------------------------------------------------------------------------

SNAPSHOT_PATH = REPO_ROOT / "tests" / "contract" / "mcp_tools.json"


def _surface(tools: list[Any]) -> dict[str, Any]:
    """The parts of the surface a client depends on.

    Names, descriptions and input schemas only. Deliberately excluding the
    server version and anything time-dependent, which would churn the snapshot
    on unrelated releases and train reviewers to ignore its diffs.
    """
    return {
        tool.name: {
            "description": tool.description,
            "input_schema": tool.input_schema,
        }
        for tool in sorted(tools, key=lambda item: item.name)
    }


@pytest.mark.contract
@pytest.mark.asyncio
async def test_tool_surface_matches_the_snapshot(mcp_url: str) -> None:
    """docs/api/mcp-tool-guide.md section 6 and architecture section 7.1.

    An agent that has adapted to this surface sees behaviour drift when it
    changes quietly, so a change must be a visible diff in a committed file.
    Set MCP_SNAPSHOT_UPDATE=1 to rewrite the snapshot after reviewing the
    change, never to make the test pass.
    """
    async with mcp_session(mcp_url) as session:
        current = _surface((await session.list_tools()).tools)

    if os.environ.get("MCP_SNAPSHOT_UPDATE") == "1":
        SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT_PATH.write_text(
            json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        pytest.skip("snapshot rewritten; review the diff and commit it")

    assert SNAPSHOT_PATH.exists(), (
        "tests/contract/mcp_tools.json is missing; run "
        "MCP_SNAPSHOT_UPDATE=1 just contract to create it"
    )
    expected = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    assert current == expected, (
        "the MCP tool surface changed; review the difference, then run "
        "MCP_SNAPSHOT_UPDATE=1 just contract and commit the new snapshot"
    )
