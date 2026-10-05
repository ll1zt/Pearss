"""Tests for the MCP tool surface.

Tools are driven through the real MCP client over Streamable HTTP rather than
by calling the functions, because the contract under test is the wire one:
schema generation, elicitation behaviour, and the fact that a tool reaches the
audit middleware the same way any other caller does.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from mcp.client._transport import Transport
from mcp.client.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult
from sqlalchemy import text

from adapters.filestore import store_body
from app.main import create_app, create_engine_for
from app.middleware import CorrelationAndAuditMiddleware
from tests.integration.conftest import (
    ServerInThread,
    free_port,
    run_work_to_completion,
)
from tests.migrated_schema import migrated_database

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
    """A migrated database and the environment the server runs with.

    Migrated through the shared helper: an in-process `command.upgrade` reaches
    `migrations/env.py`'s `fileConfig` and rebinds this session's root logger
    to the `sys.stderr` of whichever test ran first (issue #44).
    """
    root = tmp_path_factory.mktemp("mcp")
    url = migrated_database(root, "mcp.db")

    engine = create_engine_for(url)
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


@pytest.fixture(scope="module")
def mcp_url(mcp_env: dict[str, str]) -> Iterator[str]:
    """Serve the app for the module and yield its MCP endpoint.

    The port comes from the OS (`free_port`), never a constant: the same SHA
    can have two CI runs in flight (`push` and `pull_request` are separate
    events), and a fixed port made the second run fail to bind — its server
    thread died and the client waited on an endpoint that would never answer,
    hanging the job until the 60-minute cap (#37).
    """
    previous = {key: os.environ.get(key) for key in mcp_env}
    os.environ.update(mcp_env)
    try:
        app = create_app()
        with ServerInThread(app, free_port()) as server:
            yield server.base_url("/mcp")
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _rpc_error(excinfo: pytest.ExceptionInfo[BaseException]) -> MCPError:
    """The MCPError inside whatever the transport raised.

    The anyio task group wraps the error in an ExceptionGroup on its way out,
    which pytest.raises(MCPError) will not match; the code is what the test
    asserts on, so unwrap until it appears.
    """
    found: list[MCPError] = []

    def walk(exc: BaseException) -> None:
        if isinstance(exc, MCPError):
            found.append(exc)
        for sub in getattr(exc, "exceptions", []):
            walk(sub)

    walk(excinfo.value)
    assert found, f"no MCPError in {excinfo.value!r}"
    return found[0]


@asynccontextmanager
async def mcp_session(
    url: str, *, elicitation_callback: Any = None
) -> AsyncIterator[Client]:
    """A connected modern-protocol client, opened and closed in one task.

    A helper rather than a fixture on purpose: the transport enters an anyio
    cancel scope, and anyio requires the same task to exit it. An
    async-generator fixture tears down elsewhere and fails every test with
    "attempted to exit cancel scope in a different task".

    `Client` rather than `ClientSession` because the server speaks the
    2026-07-28 protocol: there is no initialize handshake to drive, and the
    client is what implements the MRTR retry loop that carries elicitation
    answers back to the server. The transport is handed in pre-built so the
    bearer token rides every request -- `Client` accepts any `Transport`
    (an async context manager yielding the stream pair), and
    `streamable_http_client` is one.
    """
    kwargs = (
        {"elicitation_callback": elicitation_callback}
        if elicitation_callback is not None
        else {}
    )
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
    transport = streamable_http_client(url, http_client=http_client)
    client = Client(cast("Transport", transport), mode="2026-07-28", **kwargs)
    try:
        async with client:
            yield client
    finally:
        # The transport borrows the http client but does not own it; without
        # this every test leaks a socket, which filterwarnings=error turns
        # into a session-fatal ResourceWarning.
        await http_client.aclose()


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
) -> dict[str, Any]:
    """Call a tool from a client that declares no elicitation capability.

    That is the client #270 is about -- it used to get `-32021` from every
    write, so it could call all the reads and none of the writes -- and it is
    why no test needs an elicitation callback any more.
    """
    async with mcp_session(url) as session:
        return _payload(await session.call_tool(tool, args))


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


@pytest.mark.asyncio
# AC-63: the pipeline_chain gap #71 raised was closed by a tool (the
# `set_source_pipeline_chain` entry below), not by documenting an invariant.
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
        # The task-level tool (ADR 0036, issue #73): "why is this source not
        # updating" was a fixed several-call sequence ending in a confident
        # answer built from whichever of them the caller happened to run.
        "diagnose_source",
        "list_jobs",
        "list_runs",
        # Diagnostics: one row in full, which `list_*` truncates and
        # `search_items` cannot address by id.
        "get_source",
        "get_item",
        # P2 plugin lifecycle (ADR 0017).
        "list_plugins",
        "set_plugin_status",
        "add_source",
        # The batch primitive (ADR 0044, issue #159): one confirmation covers a
        # list of URLs, because the single tool means one dialog per source.
        "add_sources",
        "update_schedule_policy",
        "set_source_status",
        "set_source_folder",
        # The fourth narrow tool on the same PATCH endpoint (ADR 0036, issue
        # #71): `add_source` sets a chain only at creation, so without this one
        # "subscribe first, decide to translate later" had no path but deleting
        # the source.
        "set_source_pipeline_chain",
        "remove_source",
        # Deletion (ADR 0021): remove_source stops flagging and starts
        # deleting; these two clear items while keeping the subscription,
        # or name exact items.
        "delete_items",
        "delete_source_items",
        # Consumption state (ADR 0046 D-E, issue #218): the /v1 atom is
        # first-class, and this is its write side. Read state is what a Reader
        # writes, so an agent without it could not mark anything read.
        "set_item_read_state",
        "trigger_job",
        # P3 processor chain (ADR 0018): one-off backfill for legacy items,
        # plus chain visibility and the agent re-trigger the retry policy
        # stops at.
        "backfill_source",
        "list_enrichments",
        "retry_enrichments",
        # A3 ingest (ADR 0022): the write side of an agent source.
        "submit_items",
        # The other direction (issue #171): the caller discovered addresses and
        # the deployment fetches them through the source's own collector. Since
        # #191 it answers with a work handle rather than a per-address report.
        "fetch_source_items",
        # The read side of the work mechanism (#191, ADR 0046): the handle a
        # submission returns is only useful if it leads somewhere, and the
        # ledger is where per-target results live once the response stops
        # carrying them.
        "get_work",
        "work_units",
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
# Writes need no confirmation (ADR 0058, issue #270)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_write_runs_without_any_elicitation(mcp_url: str) -> None:
    """AC-262: a client that cannot answer an elicitation can still write.

    It used to be refused with `-32021 MissingRequiredClientCapability` before
    the tool body ran, so this client could call every read and no write. The
    counter is the other half of the scenario: not only does the call go
    through, no `elicitation/create` is sent at all.
    """
    elicitations: list[Any] = []

    async def _counting(_ctx: Any, params: Any) -> Any:
        from mcp import types

        elicitations.append(params)
        return types.ElicitResult(action="accept", content={})

    async with mcp_session(mcp_url, elicitation_callback=_counting) as session:
        payload = _payload(
            await session.call_tool(
                "add_source",
                {
                    "url": "https://no-elicitation.example/feed.xml",
                    "schedule_policy": {"type": "fixed", "interval": "30m"},
                },
            )
        )

    assert payload.get("error") is None, payload
    assert payload["canonical_url"] == "https://no-elicitation.example/feed.xml"
    assert elicitations == [], "a write must not elicit"


@pytest.mark.asyncio
async def test_no_write_tool_has_a_confirmation_parameter(mcp_url: str) -> None:
    """AC-263: the retired machinery left no parameter on the tool face.

    Read from the live registry rather than a hand-written list: a tool left out
    is a tool whose schema is unverified, and the hand-written version of this
    list had already drifted (issue #125).
    """
    async with mcp_session(mcp_url) as session:
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}

    writes = {
        name
        for name, tool in tools.items()
        if tool.annotations is not None and tool.annotations.read_only_hint is False
    }
    assert len(writes) == 16, sorted(writes)

    offenders: list[str] = []
    for name in sorted(writes):
        properties = tools[name].input_schema.get("properties") or {}
        offenders.extend(
            f"{name}.{prop}"
            for prop in properties
            if re.search(r"confirm|ack", prop, re.IGNORECASE)
        )
    assert not offenders, (
        "a write tool still shows a confirmation parameter, so the machinery "
        f"was retired only halfway: {offenders}"
    )


@pytest.mark.asyncio
async def test_a_refused_write_is_not_a_capability_error(mcp_url: str) -> None:
    """AC-264: a client without elicitation gets the API's answer, not `-32021`.

    The retired requirement answered every write with
    `-32021 MissingRequiredClientCapability` *before* the tool body ran, so the
    caller could not even learn that the source did not exist. A
    `source_not_found` says which mistake was made; a capability error says the
    client is the wrong kind of client, which is no longer true.
    """
    payload = await _call(mcp_url, "trigger_job", {"source_id": "does-not-exist"})

    assert payload["error"] == "source_not_found", payload
    assert "elicitation" not in json.dumps(payload).lower()


# --------------------------------------------------------------------------
# Audit coverage
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_reads_and_writes_share_one_audit_path(
    mcp_url: str, mcp_env: dict[str, str]
) -> None:
    """AC-265: a tool write is audited exactly like a REST call.

    This is the payoff of routing tools through /v1: the same middleware sees
    both, so there is one ledger rather than one per entry point. Retiring the
    confirmation changed none of it -- the write still has an actor and a
    correlation id.

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
    )
    CorrelationAndAuditMiddleware.drain_audit()
    engine = create_engine_for(mcp_env["PEARSS_DATABASE_URL"])
    with engine.connect() as conn:
        rows = conn.execute(text("select actor, operation from audit_log")).fetchall()
    engine.dispose()
    writes = [(actor, op) for actor, op in rows if "POST /v1/sources" in op]
    assert writes, "a write must leave an audit row"
    assert {actor for actor, _ in writes} == {"api:full"}, (
        "the write must be attributed to the authenticated caller, not anonymous"
    )


# --------------------------------------------------------------------------
# The write path: executed, then audited
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_write_reaches_the_core_api(mcp_url: str) -> None:
    """The write must actually happen, and be attributed like any other."""
    payload = await _call(
        mcp_url,
        "add_source",
        {
            "url": "https://confirmed.example/feed.xml",
            "schedule_policy": {"type": "fixed", "interval": "30m"},
        },
    )
    assert payload.get("declined") is None, payload
    assert payload.get("aborted") is None, payload
    assert payload["id"], payload
    assert payload["canonical_url"] == "https://confirmed.example/feed.xml"


def _submitted_item(**overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "external_id": "agent-entry-1",
        "title": "Gathered Elsewhere",
        "canonical_url": "https://example.com/gathered",
        "content": "<p>Body.</p>",
    }
    item.update(overrides)
    return item


async def _an_agent_source(mcp_url: str) -> str:
    """Create (or find) the ingest target, and return its id.

    Created per test rather than seeded in `mcp_env`: the shared fixture's
    source list is asserted elsewhere as exactly `["src-1"]`, and widening it
    to save a call here would have meant weakening those assertions. `add_source`
    is idempotent by URL, so repeated calls are safe.
    """
    payload = await _call(
        mcp_url,
        "add_source",
        {
            "url": "agent://intake",
            "type": "agent",
            "schedule_policy": {"type": "fixed", "interval": "1h"},
        },
    )
    source_id = payload["id"]
    assert isinstance(source_id, str)
    return source_id


@pytest.mark.asyncio
async def test_submit_items_queues_and_then_stores(
    mcp_url: str, mcp_env: dict[str, str]
) -> None:
    """AC-211: the confirmed path queues one item, and the worker lands it where told.

    Since #199 the answer is a handle: the tool used to inline the report, which
    meant an agent pushing a long batch held a response proportional to it. Both
    moments are asserted, because "queued" and "stored" are now different ones.
    """
    source_id = await _an_agent_source(mcp_url)
    payload = await _call(
        mcp_url,
        "submit_items",
        {"source_id": source_id, "items": [_submitted_item()]},
    )
    assert payload.get("declined") is None, payload
    assert payload.get("aborted") is None, payload
    assert payload["counts"]["total"] == 1, payload

    # Queued, not stored: the response is a handle, not a deed.
    empty = await _call(mcp_url, "search_items", {"limit": 100, "source_id": source_id})
    assert empty["data"] == []

    await run_work_to_completion(mcp_env, payload["work_id"])

    stored = await _call(
        mcp_url, "search_items", {"limit": 100, "source_id": source_id}
    )
    rows = stored["data"]
    assert [row["title"] for row in rows] == ["Gathered Elsewhere"]


@pytest.mark.asyncio
async def test_submit_items_refuses_a_non_agent_source(
    mcp_url: str, mcp_env: dict[str, str]
) -> None:
    """Pushing into an rss source would launder provenance.

    src-1 is an rss source: its items are supposed to come from one origin, so
    the work refuses it rather than mixing in content from somewhere else. The
    refusal is the *source's*, so every target carries the same reason -- which
    is what the ledger shows instead of a 409 the tool would have to relay.
    """
    payload = await _call(
        mcp_url,
        "submit_items",
        {"source_id": "src-1", "items": [_submitted_item()]},
    )
    assert payload.get("work_id"), payload

    await run_work_to_completion(mcp_env, payload["work_id"])

    units = await _call(mcp_url, "work_units", {"work_id": payload["work_id"]})
    [unit] = units["data"]
    assert unit["outcome"] == "failed", unit
    assert unit["error_class"] == "structure_changed", unit
    assert "only agent sources accept pushed items" in unit["detail"], unit


@pytest.mark.asyncio
async def test_submitted_content_is_sanitised(
    mcp_url: str, mcp_env: dict[str, str]
) -> None:
    """The tool must not become a way to store live markup.

    Ingest routes through the same store-boundary sanitiser as collection, so
    this pins that the MCP path inherits it rather than reimplementing a write.
    """
    source_id = await _an_agent_source(mcp_url)
    payload = await _call(
        mcp_url,
        "submit_items",
        {
            "source_id": source_id,
            "items": [
                _submitted_item(
                    external_id="agent-entry-script",
                    content="<p>ok</p><script>alert(1)</script>",
                )
            ],
        },
    )
    assert payload.get("work_id"), payload
    await run_work_to_completion(mcp_env, payload["work_id"])

    units = await _call(mcp_url, "work_units", {"work_id": payload["work_id"]})
    item_id = units["data"][0]["ref"]

    body = await _call(mcp_url, "get_item_body", {"item_id": item_id})
    assert "alert(1)" not in str(body), body
    assert "ok" in str(body), body


@pytest.mark.asyncio
async def test_trigger_records_a_run(mcp_url: str) -> None:
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
    )
    result = await _call(mcp_url, "trigger_job", {"source_id": created["id"]})
    assert result.get("declined") is None, result
    assert result["run_status"] in {"succeeded", "failed", "timed_out"}

    runs = await _call(mcp_url, "list_runs", {"job_id": f"job-{created['id']}"})
    assert runs.get("data"), "a confirmed trigger should leave a run behind"


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
    )
    assert payload.get("declined") is None and payload.get("aborted") is None, payload
    assert payload["type"] == "web", payload
    # Clean up for the rest of the suite (list_sources shape assertions).
    await _call(mcp_url, "remove_source", {"source_id": payload["id"]})


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
    )
    assert explicit["type"] == "rss", explicit
    await _call(mcp_url, "remove_source", {"source_id": feed["id"]})
    await _call(mcp_url, "remove_source", {"source_id": explicit["id"]})


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
    )
    assert enabled.get("status") == "enabled", enabled
    plugins = await _call(mcp_url, "list_plugins", {"limit": 50})
    rss = next(row for row in plugins["data"] if row["id"] == "rss")
    assert rss["status"] == "enabled"


@pytest.mark.asyncio
async def test_backfill_source_executes_and_reports_queued(
    mcp_url: str, mcp_env: dict[str, str]
) -> None:
    """AC-214: the backfill tool routes to :backfill-chain and returns a handle.

    The source needs a chain and stored items for the count to be non-zero;
    both are set up directly against the shared database, since the MCP
    surface has no tool that seeds items.
    """
    engine = create_engine_for(mcp_env["PEARSS_DATABASE_URL"])
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

    payload = await _call(mcp_url, "backfill_source", {"source_id": "src-1"})
    assert payload.get("declined") is None and payload.get("aborted") is None
    # The module database is shared, so other tests' items for this source may
    # already be queued; what must hold is that the answer is a handle with a
    # non-empty selection, and that running it leaves products behind.
    assert payload.get("work_id"), payload
    assert payload["counts"]["total"] >= 1, payload

    await run_work_to_completion(mcp_env, payload["work_id"])

    listed = await _call(mcp_url, "list_enrichments", {"source_id": "src-1"})
    assert listed["data"], listed
    assert {row["plugin_id"] for row in listed["data"]} == {"translate"}


# --------------------------------------------------------------------------
# Deletion (ADR 0021)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_source_removes_its_items(mcp_url: str) -> None:
    """The tool now deletes; the counts in its answer are the evidence.

    This is the behaviour change ADR 0021 describes: remove_source used to
    answer with a `deleted` status and leave the items published.
    """
    created = await _call(
        mcp_url,
        "add_source",
        {
            "url": "https://plain.example/todelete.xml",
            "schedule_policy": {"type": "fixed", "interval": "30m"},
        },
    )
    source_id = created["id"]

    result = await _call(mcp_url, "remove_source", {"source_id": source_id})
    assert result.get("declined") is None and result.get("aborted") is None, result
    assert result["sources"] == 1

    listed = await _call(mcp_url, "list_sources", {"limit": 50})
    assert source_id not in [row["id"] for row in listed["data"]]


@pytest.mark.asyncio
async def test_delete_items_takes_the_ids_it_was_given(
    mcp_url: str, mcp_env: dict[str, str]
) -> None:
    """AC-211: a batch delete reaches the API with the ids the caller named.

    The list travels as a comma-joined string through the elicitation schema, so
    this also pins that the round trip survives: a bug in that encoding would
    abort every batch as "arguments differ". The answer is a job handle now, and
    the ids are still there until the worker runs.
    """
    found = await _call(mcp_url, "search_items", {"limit": 5, "fields": "id"})
    ids = [row["id"] for row in found.get("data", [])]
    if not ids:
        pytest.skip("no items seeded in this environment")

    result = await _call(mcp_url, "delete_items", {"item_ids": ids[:1]})
    assert result.get("declined") is None and result.get("aborted") is None, result
    assert result["counts"]["total"] == 1, result

    # Nothing is gone yet: the response is a handle, and the row is still there.
    before = await _call(mcp_url, "search_items", {"limit": 5, "fields": "id"})
    assert ids[0] in [row["id"] for row in before.get("data", [])]

    await run_work_to_completion(mcp_env, result["work_id"])

    after = await _call(mcp_url, "search_items", {"limit": 5, "fields": "id"})
    assert ids[0] not in [row["id"] for row in after.get("data", [])]


@pytest.mark.asyncio
async def test_delete_source_items_keeps_the_subscription(mcp_url: str) -> None:
    """Clearing a crawl must not remove the source or its position."""
    created = await _call(
        mcp_url,
        "add_source",
        {
            "url": "https://plain.example/clearcrawl.xml",
            "schedule_policy": {"type": "fixed", "interval": "30m"},
        },
    )
    source_id = created["id"]

    result = await _call(mcp_url, "delete_source_items", {"source_id": source_id})
    assert result.get("declined") is None and result.get("aborted") is None, result

    listed = await _call(mcp_url, "list_sources", {"limit": 50})
    assert source_id in [row["id"] for row in listed["data"]]

    await _call(mcp_url, "remove_source", {"source_id": source_id})


# --------------------------------------------------------------------------
# AC-201: the row-level retry through the tool face
# --------------------------------------------------------------------------


def _seed_retry_rows(
    env: dict[str, str], *, slug: str, statuses: list[str]
) -> list[str]:
    """One item with one chain row per status, written through the ORM.

    The same shape the API-level scenarios seed: there is no public call that
    creates a failed product, and what is needed here is that state.
    """
    from datetime import UTC, datetime

    from sqlalchemy.orm import sessionmaker as _sessionmaker

    from adapters.db_models import Item as ItemRow
    from adapters.db_models import ItemEnrichment
    from adapters.repository import timestamp

    engine = create_engine_for(env["PEARSS_DATABASE_URL"])
    now = timestamp(datetime.now(UTC))
    ids: list[str] = []
    try:
        with _sessionmaker(engine, expire_on_commit=False)() as session:
            session.add(
                ItemRow(
                    id=f"{slug}-item",
                    source_id="src-1",
                    guid=f"{slug}-guid",
                    content_hash=f"{slug}-hash",
                    title="Retry me",
                    url=f"https://blog.example.com/{slug}",
                    publish_status="ready",
                    tenant_id="default",
                    created_at=now,
                    updated_at=now,
                )
            )
            for index, status in enumerate(statuses):
                enrichment_id = f"{slug}-enr-{index}"
                ids.append(enrichment_id)
                session.add(
                    ItemEnrichment(
                        id=enrichment_id,
                        item_id=f"{slug}-item",
                        plugin_id="translate",
                        plugin_version="0.1.0",
                        kind="translation",
                        lang="en",
                        input_hash=f"{slug}-hash-{index}",
                        status=status,
                        error_class=None if status == "queued" else "auth",
                        detail=None if status == "queued" else f"no ({index})",
                        attempt_count=0 if status == "queued" else 3,
                        tenant_id="default",
                        created_at=now,
                        updated_at=now,
                    )
                )
            session.commit()
    finally:
        engine.dispose()
    return ids


@pytest.mark.asyncio
async def test_a_row_level_retry_requeues_exactly_one(
    mcp_url: str, mcp_env: dict[str, str]
) -> None:
    """The tool's second granularity: one row, named by its id.

    `retry_enrichments` was source-only, so 23 permanently failed products with
    three worth retrying had no expression (issue #195). The other row is
    asserted by value: a requeue that ignored the id would change it too and
    still look like a success.
    """
    first, second = _seed_retry_rows(
        mcp_env, slug="mcp-retry", statuses=["failed", "failed"]
    )

    result = await _call(mcp_url, "retry_enrichments", {"enrichment_id": second})

    assert result.get("error") is None, result
    assert result["enrichment_id"] == second
    assert result["status"] == "queued"
    assert result["requeued"] == 1

    listed = await _call(mcp_url, "list_enrichments", {"item_id": "mcp-retry-item"})
    by_id = {row["id"]: row for row in listed["data"]}
    assert by_id[second]["status"] == "queued"
    assert by_id[second]["attempt_count"] == 0
    assert by_id[second]["detail"] is None
    assert by_id[first]["status"] == "failed"
    assert by_id[first]["attempt_count"] == 3


@pytest.mark.asyncio
async def test_the_row_level_retry_says_what_it_needs(mcp_url: str) -> None:
    """No target is a fixable mistake, not a protocol error.

    The answer is a *string* code with the tools that supply an id, so a caller
    that forgot the target can fix it with a call it already has -- and the
    error shape is the one `_error_from` uses rather than a second, nested one
    (issue #216).
    """
    result = await _call(mcp_url, "retry_enrichments", {})

    assert isinstance(result.get("error"), str), result
    assert result["error"] == "missing_target"
    assert "list_enrichments" in result["next_actions"]


@pytest.mark.asyncio
async def test_a_source_level_retry_submits_a_job(
    mcp_url: str, mcp_env: dict[str, str]
) -> None:
    """AC-213 through the tool face: the whole-source retry answers with a handle.

    The selection is the server's -- the caller cannot name the failed rows
    without paging the chain -- so the tool's job is to hand back the handle and
    let `work_units` answer per row. Asserted on the unit for *this* row, because
    the module's database is shared and other tests may leave failures behind.
    """
    [enrichment_id] = _seed_retry_rows(
        mcp_env, slug="mcp-src-retry", statuses=["failed"]
    )

    result = await _call(mcp_url, "retry_enrichments", {"source_id": "src-1"})

    assert result.get("error") is None, result
    assert result["work_id"], result
    assert result["counts"]["total"] >= 1, result

    await run_work_to_completion(mcp_env, result["work_id"])

    units = await _call(mcp_url, "work_units", {"work_id": result["work_id"]})
    by_target = {unit["target"]: unit for unit in units["data"]}
    assert by_target[enrichment_id]["outcome"] == "ok", by_target[enrichment_id]
    assert by_target[enrichment_id]["detail"] == "requeued"


@pytest.mark.asyncio
async def test_a_source_items_delete_submits_a_job(
    mcp_url: str, mcp_env: dict[str, str]
) -> None:
    """AC-215: clearing a source's items is a job, and the source survives it.

    A *fresh* source rather than src-1: the module shares one database, and
    deleting another test's items to prove this one works would be this test
    reaching outside its own fixture.
    """
    from sqlalchemy.orm import sessionmaker as _sessionmaker

    from adapters.db_models import Item as ItemRow

    created = await _call(
        mcp_url,
        "add_source",
        {
            "url": "https://clear.example/feed",
            "schedule_policy": {"type": "fixed", "interval": "30m"},
        },
    )
    source_id = created["id"]

    engine = create_engine_for(mcp_env["PEARSS_DATABASE_URL"])
    try:
        with _sessionmaker(engine, expire_on_commit=False)() as session:
            session.add(
                ItemRow(
                    id="clear-mcp-item",
                    source_id=source_id,
                    guid="clear-mcp-guid",
                    content_hash="clear-mcp-hash",
                    title="Bad crawl",
                    url="https://clear.example/1",
                    publish_status="ready",
                    tenant_id="default",
                    created_at="2026-01-01T00:00:00.000000Z",
                    updated_at="2026-01-01T00:00:00.000000Z",
                )
            )
            session.commit()
    finally:
        engine.dispose()

    result = await _call(mcp_url, "delete_source_items", {"source_id": source_id})

    assert result.get("declined") is None and result.get("aborted") is None, result
    assert result.get("work_id"), result
    assert result["counts"]["total"] == 1, result

    # Nothing is gone until the work runs.
    before = await _call(mcp_url, "search_items", {"limit": 5, "source_id": source_id})
    assert [row["id"] for row in before["data"]] == ["clear-mcp-item"]

    await run_work_to_completion(mcp_env, result["work_id"])

    after = await _call(mcp_url, "search_items", {"limit": 5, "source_id": source_id})
    assert after["data"] == []
    source = await _call(mcp_url, "get_source", {"source_id": source_id})
    assert source["id"] == source_id, "the subscription survives the clear"
    assert source["status"] == "active", source


# --------------------------------------------------------------------------
# AC-243..245: the edit path -- choose a field, give it a value, confirm
# --------------------------------------------------------------------------
