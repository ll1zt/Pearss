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
from typing import Any, cast

import httpx
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

from adapters.filestore import store_body
from app.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]
MCP_EDGE_TOKEN = "synthetic-mcp-edge-token"
API_TOKEN = "mcp-test-token"
# Long enough that the default cap truncates it, so `truncated` is
# exercised rather than merely implemented.
LONG_BODY = "<p>" + ("body text. " * 900) + "</p>"

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
                " published_at, publish_status, tenant_id, created_at, updated_at,"
                " content_ref)"
                " values ('i-long', 'src-1', 'g-long', 'h-long', 'Item with a body',"
                " 'https://blog.example.com/long',"
                " '2026-06-01T12:00:00.000000Z', 'ready', 'default',"
                " '2026-06-01T12:00:00.000000Z', '2026-06-01T12:00:00.000000Z', :ref)"
            ),
            {"ref": store_body(LONG_BODY, root=root / "storage")},
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
        "PEARSS_MCP_TOKEN": MCP_EDGE_TOKEN,
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
    # The MCP endpoint now requires its own bearer token, so the client is
    # built with the header rather than left to the default.
    # The SDK vendors its own renamed httpx (`httpx2`), so the parameter is
    # typed against a different class than the one this project depends on.
    # Structurally identical for what the client uses; the cast records that
    # rather than suppressing the error.
    http_client = cast(
        "Any",
        httpx.AsyncClient(
            headers={"authorization": f"Bearer {MCP_EDGE_TOKEN}"},
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
        ),
    )
    async with (
        http_client,
        streamable_http_client(url, http_client=http_client) as (read, write),
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
        # A read added under ADR 0013: bounded, self-describing, and the
        # fallback for clients that cannot read the item:// Resource.
        "get_item_body",
        "get_system_snapshot",
        "list_jobs",
        "list_runs",
        # P2 plugin lifecycle (ADR 0017).
        "list_plugins",
        "set_plugin_status",
        "add_source",
        "update_schedule_policy",
        "set_source_status",
        "remove_source",
        "trigger_job",
        # P3 processor chain (ADR 0018): one-off backfill for legacy items,
        # plus chain visibility and the agent re-trigger the retry policy
        # stops at.
        "backfill_source",
        "list_enrichments",
        "retry_enrichments",
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
    # Two ready items: `i-1` (body reference points nowhere) and `i-long`.
    assert payload["items_by_publish_status"]["ready"] == 2
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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_item_body_returns_the_stored_html(mcp_url: str) -> None:
    payload = await _call(mcp_url, "get_item_body", {"item_id": "i-long"})
    assert payload["body"].startswith("<p>")
    assert payload["item_id"] == "i-long"
    assert payload["untrusted"] is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_item_body_truncates_and_says_so(mcp_url: str) -> None:
    """A silently shortened body reads as the whole thing.

    An agent that cannot tell would summarise content it never received, so
    `truncated` and `total_chars` are the load-bearing fields here, not the
    clipping itself.
    """
    payload = await _call(
        mcp_url, "get_item_body", {"item_id": "i-long", "max_chars": 100}
    )
    assert len(payload["body"]) == 100
    assert payload["truncated"] is True
    assert payload["total_chars"] == len(LONG_BODY)
    assert payload["max_chars"] == 100


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_item_body_default_cap_actually_bites(mcp_url: str) -> None:
    """The default must be a real bound, not a number that never applies."""
    payload = await _call(mcp_url, "get_item_body", {"item_id": "i-long"})
    assert payload["truncated"] is True
    assert len(payload["body"]) == 4000


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_item_body_refuses_an_over_cap_request(mcp_url: str) -> None:
    """Otherwise the tool is the easy way around the granularity rule."""
    payload = await _call(
        mcp_url, "get_item_body", {"item_id": "i-long", "max_chars": 999_999}
    )
    assert payload["error"] == "max_chars_too_large"
    assert payload["max_chars_allowed"] == 20000
    assert "body" not in payload


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_item_body_rejects_a_non_positive_cap(mcp_url: str) -> None:
    payload = await _call(
        mcp_url, "get_item_body", {"item_id": "i-long", "max_chars": 0}
    )
    assert payload["error"] == "invalid_max_chars"
    assert "body" not in payload


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_item_body_honours_the_publish_gate(mcp_url: str) -> None:
    """A pending item is invisible on every path, this one included."""
    payload = await _call(mcp_url, "get_item_body", {"item_id": "i-pending"})
    # Not merely "some error": the gate reports the item as absent, so an
    # agent cannot distinguish pending from nonexistent by probing (and a
    # regression to a 500 would be caught rather than accepted).
    assert payload.get("error") == "item_not_found", payload
    assert "body" not in payload


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_item_body_reports_a_missing_item(mcp_url: str) -> None:
    payload = await _call(mcp_url, "get_item_body", {"item_id": "does-not-exist"})
    assert payload.get("error") == "item_not_found", payload
    assert "body" not in payload


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_item_body_on_an_item_without_a_stored_body(mcp_url: str) -> None:
    """`i-1` points at a CAS object that does not exist.

    The honest answer is an explicit error, not a silent "" an agent could read
    as "this post has no content".
    """
    payload = await _call(mcp_url, "get_item_body", {"item_id": "i-1"})
    assert payload.get("error") in {"body_not_stored", "body_unreadable"}, payload
    assert "body" not in payload


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_item_body_is_read_only(mcp_url: str) -> None:
    """A read must not require elicitation."""
    payload = await _call(mcp_url, "get_item_body", {"item_id": "i-long"})
    assert "declined" not in payload and "aborted" not in payload
    assert payload["body"].startswith("<p>")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_narrowing_fields_cuts_what_reaches_the_model(mcp_url: str) -> None:
    """The point of filter pushdown, measured rather than asserted.

    A wide list the model then filters costs context in proportion to how
    unselective the request was. This pins the saving so a future change that
    stopped forwarding `fields` cannot pass silently.
    """
    wide_payload = await _call(mcp_url, "search_items", {"limit": 100})
    narrow_payload = await _call(
        mcp_url, "search_items", {"limit": 100, "fields": "id"}
    )

    # The stable property: rows carry exactly the fields requested, nothing
    # more. A wrapper that accepted `fields` and ignored it could still be
    # "smaller" for unrelated reasons, so this is what pins the behaviour.
    assert all(set(row) == {"id"} for row in narrow_payload["data"])
    assert {"title", "url"} <= set(wide_payload["data"][0])

    # The saving scales with row count, since a fixed envelope is paid either
    # way; two seeded rows should still show it clearly.
    wide = json.dumps(wide_payload)
    narrow = json.dumps(narrow_payload)
    assert len(narrow) < len(wide) * 0.75, (
        f"fields=id ({len(narrow)} chars) should be clearly smaller than all "
        f"fields ({len(wide)} chars)"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_every_documented_filter_is_forwarded(mcp_url: str) -> None:
    """Filters exist to be used; one that is accepted and ignored is a trap.

    Each is applied to a value that selects something, so a filter silently
    dropped by the wrapper leaves the result set unchanged and this fails.
    """
    all_items = await _call(mcp_url, "search_items", {"limit": 100})

    by_source = await _call(
        mcp_url, "search_items", {"limit": 100, "source_id": "src-1"}
    )
    assert [row["id"] for row in by_source["data"]]
    assert all(row["source_id"] == "src-1" for row in by_source["data"])

    by_status = await _call(
        mcp_url, "search_items", {"limit": 100, "publish_status": "pending"}
    )
    # The gate excludes pending rows from every output path, this one included.
    assert by_status["data"] == []

    by_time = await _call(
        mcp_url,
        "search_items",
        {"limit": 100, "since": "2099-01-01T00:00:00.000000Z"},
    )
    assert by_time["data"] == [], f"since was ignored: {by_time}"
    assert len(all_items["data"]) >= len(by_source["data"])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_wide_request_is_told_what_it_cost(mcp_url: str) -> None:
    """The hint appears exactly when the request was unselective."""
    narrow = await _call(mcp_url, "search_items", {"limit": 5})
    assert "cost_hint" not in narrow

    wide = await _call(mcp_url, "search_items", {"limit": 100})
    assert "cost_hint" in wide


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
        ("retry_enrichments", {"source_id": "src-1"}),
        # Every write tool belongs here: a tool left out is a tool whose
        # confirmation requirement is unverified.
        ("backfill_source", {"source_id": "src-1"}),
        ("set_plugin_status", {"plugin_id": "rss", "status": "disabled"}),
        ("remove_source", {"source_id": "src-1"}),
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

    The consequence worth pinning is that MCP-layer refusals produce no *Core
    API* rows, while a confirmed write does (asserted by the next test), so the
    audit trail of `/v1/*` means exactly "something reached the API".

    Rows are filtered to `POST /v1/` rather than counted wholesale: the MCP
    transport itself is a POST to `/mcp/mcp`, so every protocol frame
    (`initialize`, `list_tools`, each `tools/call`) leaves an audit row for the
    mounted edge route. A plain before/after count therefore moves even when
    the refused write is correctly refused, and the earlier substring-search
    version could not fail at all — the audit row carries the route, never the
    request URL.
    """

    def _core_api_writes() -> list[str]:
        engine = sa.create_engine(mcp_env["PEARSS_DATABASE_URL"])
        try:
            with engine.connect() as conn:
                return [
                    op
                    for (op,) in conn.execute(
                        text(
                            "select operation from audit_log"
                            " where operation like 'POST /v1/%'"
                        )
                    ).fetchall()
                ]
        finally:
            engine.dispose()

    before = _core_api_writes()

    await _call(mcp_url, "list_sources", {"limit": 5})
    refused = await _call(
        mcp_url,
        "add_source",
        {
            "url": "https://never-applied.example/feed",
            "schedule_policy": {"type": "fixed", "interval": "30m"},
        },
    )
    assert refused.get("declined") is True, "the write must have been refused"

    assert _core_api_writes() == before, (
        "a declined write must not produce a Core API audit row"
    )


@pytest.mark.asyncio
async def test_confirmed_reads_and_writes_share_one_audit_path(
    mcp_url: str, mcp_env: dict[str, str]
) -> None:
    """A confirmed tool write is audited exactly like a REST call.

    This is the payoff of routing tools through /v1: the same middleware sees
    both, so there is one ledger rather than one per entry point.

    The actor assertion is deliberately exact for the row this test creates:
    a subset check against {"api:full","anonymous"} would pass on an empty
    table, and the point is that the *write* was attributed to the caller.
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
    writes = [(actor, op) for actor, op in rows if "POST /v1/sources" in op]
    assert writes, "a confirmed write must leave an audit row"
    assert {actor for actor, _ in writes} == {"api:full"}, (
        "the write must be attributed to the authenticated caller, not anonymous"
    )


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


@pytest.mark.asyncio
async def test_add_source_infers_web_type_for_a_collector_url(mcp_url: str) -> None:
    """A URL a non-rss collector claims defaults to type=web, not rss.

    Subscribing a Zhihu profile used to create an rss-typed source; the rss
    collector then fetched the origin's HTML page and answered 403. The
    matchers know which collector claims the URL, so the inference uses
    them - and the elicitation dialog carries the inferred type so the user
    confirms what will actually run.
    """
    payload = await _call(
        mcp_url,
        "add_source",
        {
            "url": "https://www.zhihu.com/people/someone",
            "schedule_policy": {"type": "fixed", "interval": "2h"},
        },
        confirm=True,
    )
    assert payload.get("declined") is None and payload.get("aborted") is None, payload
    assert payload["type"] == "web", payload
    # Clean up for the rest of the suite (list_sources shape assertions).
    await _call(mcp_url, "remove_source", {"source_id": payload["id"]}, confirm=True)


@pytest.mark.asyncio
async def test_add_source_rss_default_unchanged_for_feed_urls(mcp_url: str) -> None:
    """A URL no non-rss collector claims stays type=rss (the explicit form
    too: an operator passing type='rss' gets rss even for a zhihu URL)."""
    feed = await _call(
        mcp_url,
        "add_source",
        {
            "url": "https://plain.example/feed.xml",
            "schedule_policy": {"type": "fixed", "interval": "30m"},
        },
        confirm=True,
    )
    assert feed["type"] == "rss", feed
    explicit = await _call(
        mcp_url,
        "add_source",
        {
            "url": "https://www.zhihu.com/people/other",
            "type": "rss",
            "schedule_policy": {"type": "fixed", "interval": "30m"},
        },
        confirm=True,
    )
    assert explicit["type"] == "rss", explicit
    await _call(mcp_url, "remove_source", {"source_id": feed["id"]}, confirm=True)
    await _call(mcp_url, "remove_source", {"source_id": explicit["id"]}, confirm=True)


# --------------------------------------------------------------------------
# The confirmed path for tools the suite previously only named
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_plugin_status_maps_its_operation_and_executes(
    mcp_url: str,
) -> None:
    """`enabled` and `disabled` must map to opposite operations.

    The tool builds the URL as `:enable` for "enabled" and `:disable`
    otherwise, so a typo in the comparison would silently send every status to
    the same endpoint. Asserting the observed effect (not the request shape)
    is what pins the mapping.
    """
    disabled = await _call(
        mcp_url,
        "set_plugin_status",
        {"plugin_id": "rss", "status": "disabled"},
        confirm=True,
    )
    assert disabled.get("declined") is None and disabled.get("aborted") is None
    assert disabled.get("status") == "disabled", disabled

    plugins = await _call(mcp_url, "list_plugins", {"limit": 50})
    rss = next(row for row in plugins["data"] if row["id"] == "rss")
    assert rss["status"] == "disabled"

    # And back: the opposite status reaches the opposite endpoint.
    enabled = await _call(
        mcp_url,
        "set_plugin_status",
        {"plugin_id": "rss", "status": "enabled"},
        confirm=True,
    )
    assert enabled.get("status") == "enabled", enabled
    plugins = await _call(mcp_url, "list_plugins", {"limit": 50})
    rss = next(row for row in plugins["data"] if row["id"] == "rss")
    assert rss["status"] == "enabled"


@pytest.mark.asyncio
async def test_backfill_source_executes_and_reports_queued(
    mcp_url: str, mcp_env: dict[str, str]
) -> None:
    """The backfill tool routes to :backfill-chain and returns its count.

    The source needs a chain and stored items for the count to be non-zero;
    both are set up directly against the shared database, since the MCP
    surface has no tool that seeds items.
    """
    engine = sa.create_engine(mcp_env["PEARSS_DATABASE_URL"])
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "update source set pipeline_chain = :chain, target_lang = 'en'"
                    " where id = 'src-1'"
                ),
                {
                    "chain": json.dumps(
                        [{"plugin_id": "translate", "kind": "translation"}]
                    )
                },
            )
            conn.execute(
                text(
                    "insert into item (id, source_id, guid, content_hash, title, url,"
                    " publish_status, tenant_id, created_at, updated_at)"
                    " values ('bf-mcp', 'src-1', 'g-bf', 'h-bf', 'Backfill me',"
                    " 'https://bf.example/1', 'ready', 'default', :ts, :ts)"
                ),
                {"ts": "2026-01-01T00:00:00.000000Z"},
            )
    finally:
        engine.dispose()

    payload = await _call(
        mcp_url, "backfill_source", {"source_id": "src-1"}, confirm=True
    )
    assert payload.get("declined") is None and payload.get("aborted") is None
    # The module database is shared, so other tests' items for this source may
    # already be queued; what must hold is that this source now has products
    # and the count is honest (non-zero means the verb did its job).
    assert payload["queued"] >= 1, payload

    listed = await _call(mcp_url, "list_enrichments", {"source_id": "src-1"})
    assert listed["data"], listed
    assert {row["plugin_id"] for row in listed["data"]} == {"translate"}


@pytest.mark.asyncio
async def test_every_write_tool_aborts_on_a_tampered_confirmation(
    mcp_url: str,
) -> None:
    """A confirmation of one request must not authorise another -- for every
    write tool, not just add_source.

    Each tool has its own `_matches(...)` guard; a missing one lets a tampered
    client execute something the user never saw. The tamper rewrites the first
    string field the tool asks about, so the guard for that tool is exercised.
    """
    # (tool, args, field the tamper will flip)
    cases: list[tuple[str, dict[str, Any], str]] = [
        (
            "add_source",
            {
                "url": "https://tamper-add.example/feed",
                "schedule_policy": {"type": "fixed", "interval": "30m"},
            },
            "url",
        ),
        (
            "update_schedule_policy",
            {
                "source_id": "src-1",
                "schedule_policy": {"type": "fixed", "interval": "1h"},
            },
            "source_id",
        ),
        ("set_source_status", {"source_id": "src-1", "status": "paused"}, "status"),
        ("trigger_job", {"source_id": "src-1"}, "source_id"),
        ("retry_enrichments", {"source_id": "src-1"}, "source_id"),
        ("backfill_source", {"source_id": "src-1"}, "source_id"),
        ("set_plugin_status", {"plugin_id": "rss", "status": "disabled"}, "status"),
        ("remove_source", {"source_id": "src-1"}, "source_id"),
    ]

    def _tampering(field: str) -> Any:
        async def _cb(_ctx: Any, params: Any) -> Any:
            from mcp import types

            properties = (params.requested_schema or {}).get("properties", {})
            content = {
                name: (
                    "tampered-value"
                    if name == field and spec.get("type") != "boolean"
                    else spec.get(
                        "default", False if spec.get("type") == "boolean" else ""
                    )
                )
                for name, spec in properties.items()
            }
            return types.ElicitResult(action="accept", content=content)

        return _cb

    for tool, args, field in cases:
        async with mcp_session(
            mcp_url, elicitation_callback=_tampering(field)
        ) as session:
            result = await session.call_tool(tool, args)
        payload = _payload(result)
        assert payload.get("aborted") is True, (tool, payload)
        assert "differ" in payload["reason"], (tool, payload)

    # And nothing was applied: src-1 is still active, the tampered URLs were
    # not added, and rss is still enabled. Asserted per-item rather than by
    # exact list, because the module database is shared and other tests leave
    # sources behind.
    sources = await _call(mcp_url, "list_sources", {"limit": 50})
    src1 = next(row for row in sources["data"] if row["id"] == "src-1")
    assert src1["status"] == "active", src1
    assert not any("tamper-add" in row["canonical_url"] for row in sources["data"]), (
        "the tampered URL must not have been added"
    )
    plugins = await _call(mcp_url, "list_plugins", {"limit": 50})
    assert next(r for r in plugins["data"] if r["id"] == "rss")["status"] == "enabled"
