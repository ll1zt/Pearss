"""Spec-conformance tests for the MCP surface.

These pin behaviour the 2026-07-28 specification requires, at the wire level,
so a regression shows up as a protocol violation rather than as an agent
quietly misreading a result.

Each test names the requirement it enforces. Two of them (the resource error
code and the method/name header check) failed when first written: they are the
gaps this module exists to close, and they are kept as the regression net for
them.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from mcp.client._transport import Transport
from mcp.client.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult, TextResourceContents
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from adapters.repository import (
    claim_job_run,
    create_job_run,
    ensure_collect_job,
    finish_job_run,
)
from app.main import create_app, create_engine_for
from core.enums import ErrorClass
from core.job_state import JobRunEvent
from tests.integration.conftest import ServerInThread, free_port

REPO_ROOT = Path(__file__).resolve().parents[2]
MCP_EDGE_TOKEN = "conformance-edge-token"
API_TOKEN = "conformance-api-token"
# The SDK app routes on `/mcp` and is mounted at the root, so the endpoint is
# `/mcp`. It used to be `/mcp/mcp` (the SDK path under a `/mcp` mount), which
# only worked for clients that follow redirects on POST.
MCP_PATH = "/mcp"

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def mcp_env(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    root = tmp_path_factory.mktemp("conformance")
    url = str(sa.URL.create("sqlite+pysqlite", database=str(root / "conf.db")))
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")

    engine = create_engine_for(url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "insert into source (id, type, url, canonical_url, schedule_policy,"
                " status, tenant_id, created_at, updated_at)"
                " values ('src-1', 'rss', 'https://e.com/f.xml',"
                " 'https://e.com/f.xml', '{}', 'active', 'default',"
                " '2026-01-01T00:00:00.000000Z', '2026-01-01T00:00:00.000000Z')"
            )
        )
        conn.execute(
            text(
                "insert into item (id, source_id, guid, content_hash, title, url,"
                " publish_status, tenant_id, created_at, updated_at, content_ref)"
                " values ('i-ready', 'src-1', 'g-ready', 'h-ready', 'Ready',"
                " 'https://e.com/1', 'ready', 'default',"
                " '2026-01-01T00:00:00.000000Z', '2026-01-01T00:00:00.000000Z',"
                " null)"
            )
        )
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
    """Serve the app for the module; the port comes from the OS (#37).

    Never a constant: the same SHA can have two CI runs in flight (`push` and
    `pull_request` are separate events), and a fixed port made the second run
    fail to bind — its server thread died and the client waited on an
    endpoint that would never answer, hanging the job to the 60-minute cap.
    """
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


@pytest.fixture
def mcp_url(base_url: str) -> str:
    return f"{base_url}{MCP_PATH}"


def _payload(result: CallToolResult) -> dict[str, Any]:
    for block in result.content:
        text_value = getattr(block, "text", None)
        if text_value:
            return dict(json.loads(text_value))
    return {}


def _decode_response(response: httpx.Response) -> dict[str, Any]:
    """The JSON-RPC payload, whether it arrived as JSON or as one SSE event.

    The transport may answer either way and the client must support both, so a
    raw probe has to as well: an SSE answer is `event: message` followed by a
    `data:` line holding the same JSON object.
    """
    content_type = response.headers.get("content-type", "")
    if "text/event-stream" not in content_type:
        try:
            return dict(response.json())
        except ValueError:
            return {}
    for line in response.text.splitlines():
        if line.startswith("data:"):
            try:
                return dict(json.loads(line[len("data:") :].strip()))
            except ValueError:
                continue
    return {}


async def _rpc(
    mcp_url: str,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    """One raw JSON-RPC POST, so the transport layer is observable.

    `_meta` carries the protocol version and client capabilities, which the
    current protocol requires on every request instead of a handshake.
    """
    body: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": {
            **(params or {}),
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": {},
            },
        },
    }
    request_headers = {
        "authorization": f"Bearer {MCP_EDGE_TOKEN}",
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
        "mcp-protocol-version": "2026-07-28",
        "mcp-method": method,
        **(headers or {}),
    }
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(mcp_url, json=body, headers=request_headers)
    return response.status_code, _decode_response(response)


@asynccontextmanager
async def mcp_session(url: str) -> AsyncIterator[Client]:
    """A modern-protocol client with the edge token attached.

    Same shape as the integration suite's helper; local to this file so the
    conformance layer has no dependency on it.
    """
    http_client = cast(
        "Any",
        httpx.AsyncClient(
            headers={"authorization": f"Bearer {MCP_EDGE_TOKEN}"},
            timeout=httpx.Timeout(30.0),
        ),
    )
    transport = streamable_http_client(url, http_client=http_client)
    try:
        async with Client(cast("Transport", transport), mode="2026-07-28") as client:
            yield client
    finally:
        await http_client.aclose()


# --------------------------------------------------------------------------
# Resource error reporting (spec: resources, Error Handling)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reading_a_missing_resource_is_an_error_not_empty_content(
    mcp_url: str,
) -> None:
    """`resources/read` on a non-existent item must not return empty content.

    Spec, resources Error Handling:

        Servers MUST NOT return an empty `contents` array for a non-existent
        resource. An empty array is ambiguous -- it could mean the resource
        exists but has no content, or that it doesn't exist at all.

    The registered handler returned `""` for a missing item, which is worse
    than an empty array: an agent reads it as "this article has no body" and
    reports that to the user, when the truth is the id does not exist. The
    distinction is exactly what the client cannot recover on its own.
    """
    http_client = cast(
        "Any",
        httpx.AsyncClient(
            headers={"authorization": f"Bearer {MCP_EDGE_TOKEN}"},
            timeout=httpx.Timeout(30.0),
        ),
    )
    transport = streamable_http_client(mcp_url, http_client=http_client)
    try:
        async with Client(cast("Transport", transport), mode="2026-07-28") as client:
            with pytest.raises(MCPError) as excinfo:
                await client.read_resource(cast("Any", "item://does-not-exist"))
    finally:
        await http_client.aclose()

    error = excinfo.value
    # -32602 (Invalid Params) is the spec's code for a missing resource;
    # -32002 is what earlier revisions used and clients still accept.
    assert error.code in {-32602, -32002}, error.error
    assert "not found" in error.message.lower(), error.message
    assert error.data.get("uri") == "item://does-not-exist", error.data


@pytest.mark.asyncio
async def test_reading_an_existing_item_resource_still_works(mcp_url: str) -> None:
    """The positive control: the error path must not swallow real content."""
    from adapters.filestore import store_body

    digest = store_body("<p>Real body.</p>")
    engine = create_engine_for(os.environ["PEARSS_DATABASE_URL"])
    with engine.begin() as conn:
        conn.execute(
            text("update item set content_ref = :ref where id = 'i-ready'"),
            {"ref": digest},
        )
    engine.dispose()

    http_client = cast(
        "Any",
        httpx.AsyncClient(
            headers={"authorization": f"Bearer {MCP_EDGE_TOKEN}"},
            timeout=httpx.Timeout(30.0),
        ),
    )
    transport = streamable_http_client(mcp_url, http_client=http_client)
    try:
        async with Client(cast("Transport", transport), mode="2026-07-28") as client:
            result = await client.read_resource(cast("Any", "item://i-ready"))
    finally:
        await http_client.aclose()

    assert result.contents, "an existing item must return its body"
    first = result.contents[0]
    assert isinstance(first, TextResourceContents), first
    assert "Real body." in first.text


# --------------------------------------------------------------------------
# Transport headers (spec: streamable http, SEP-2243)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_mismatched_mcp_method_header_is_refused(mcp_url: str) -> None:
    """`Mcp-Method` must agree with the JSON-RPC method in the body.

    The header exists so a proxy can route and reject without parsing the body.
    If a server accepts a request whose header disagrees with its body, the
    proxy's decision and the server's action are about different requests --
    which is the whole failure the header was added to prevent.
    """
    status, payload = await _rpc(
        mcp_url,
        "tools/list",
        headers={"mcp-method": "tools/call", "mcp-name": "add_source"},
    )

    assert status != 200 or "error" in payload, payload
    if "error" in payload:
        assert payload["error"]["code"] == -32020, payload


@pytest.mark.asyncio
async def test_a_matching_mcp_method_header_is_accepted(mcp_url: str) -> None:
    """The positive control: correct headers must not be refused."""
    status, payload = await _rpc(
        mcp_url,
        "tools/list",
        headers={"mcp-method": "tools/list", "mcp-name": "tools/list"},
    )

    assert status == 200, payload
    assert "result" in payload, payload


# --------------------------------------------------------------------------
# Stateless transport (spec 2026-07-28: sessions removed)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_request_needs_no_session_id(mcp_url: str) -> None:
    """The current spec removed protocol-level sessions and `Mcp-Session-Id`.

    A request carrying its protocol version and client capabilities in `_meta`
    must be answerable without a prior handshake. Demanding a session id makes
    every client do a two-request dance for no state we keep, and it puts
    `server/discover` (a MUST) out of reach on the legacy wire.
    """
    status, payload = await _rpc(
        mcp_url,
        "tools/list",
        headers={"mcp-method": "tools/list", "mcp-name": "tools/list"},
    )

    assert status == 200, payload
    assert "result" in payload, payload


@pytest.mark.asyncio
async def test_server_discover_answers(mcp_url: str) -> None:
    """`server/discover` is a MUST for servers on the current protocol.

    It advertises supported versions, capabilities and identity before any
    other call, which is what lets a client pick a version up front instead of
    probing and falling back.
    """
    status, payload = await _rpc(
        mcp_url,
        "server/discover",
        headers={"mcp-method": "server/discover", "mcp-name": "server/discover"},
    )

    assert status == 200, payload
    result = payload.get("result", {})
    # Wire names are camelCase (`supportedVersions`); the snake_case spellings
    # are the Python-side model fields.
    versions = result.get("supportedVersions") or result.get("supported_versions")
    assert "2026-07-28" in (versions or []), result
    assert result.get("capabilities"), result
    # The identity a client uses to decide whether to keep talking.
    assert result.get("instructions"), "discover must carry the server instructions"


@pytest.mark.asyncio
async def test_cacheable_results_carry_ttl_and_scope(mcp_url: str) -> None:
    """`tools/list` and `resources/read` MUST carry `ttlMs` and `cacheScope`.

    Without them a client re-fetches the tool surface on every turn. The values
    are ours to choose; the presence of the fields is not.
    """
    status, payload = await _rpc(
        mcp_url,
        "tools/list",
        headers={"mcp-method": "tools/list", "mcp-name": "tools/list"},
    )

    assert status == 200, payload
    result = payload["result"]
    assert "ttlMs" in result, result
    assert result["cacheScope"] in {"public", "private"}, result
    assert result["ttlMs"] >= 0, result


@pytest.mark.asyncio
async def test_results_carry_a_result_type(mcp_url: str) -> None:
    """Every result on the current protocol states `resultType`."""
    status, payload = await _rpc(
        mcp_url,
        "tools/list",
        headers={"mcp-method": "tools/list", "mcp-name": "tools/list"},
    )

    assert status == 200, payload
    assert payload["result"]["resultType"] == "complete", payload["result"]


# --------------------------------------------------------------------------
# MCP Apps: the health panel (progressive enhancement over get_system_snapshot)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_tool_declares_its_ui_resource(mcp_url: str) -> None:
    """The tool's _meta.ui points at the panel, per the Apps extension.

    A host that supports Apps renders the bound resource instead of leaving
    the model to lay out the counts; a host that does not support Apps never
    sees the binding matter. Either way the binding itself is contract.
    """
    async with mcp_session(mcp_url) as client:
        tools = (await client.list_tools()).tools
    snapshot = next(t for t in tools if t.name == "get_system_snapshot")
    resource_uri = (snapshot.meta or {}).get("ui", {}).get("resourceUri")
    assert resource_uri == "ui://pearss/health", snapshot.meta


@pytest.mark.asyncio
async def test_the_health_panel_is_served_as_an_app_resource(mcp_url: str) -> None:
    """Reading the panel yields app HTML, rendered from current state.

    The resource is re-rendered per read, so the counts reflect the database
    at display time rather than at registration.
    """
    async with mcp_session(mcp_url) as client:
        result = await client.read_resource(cast("Any", "ui://pearss/health"))

    first = result.contents[0]
    assert isinstance(first, TextResourceContents), first
    assert first.mime_type == "text/html;profile=mcp-app", first.mime_type
    assert "Pearss" in first.text


@pytest.mark.asyncio
async def test_the_health_panel_renders_the_decay_signals(
    mcp_url: str, base_url: str
) -> None:
    """Section 7.7's signals appear with their live counts.

    Two failed network runs and one pending item are planted directly; the
    panel must carry those exact numbers -- the whole point of rendering
    server-side is that the agent neither computes nor mis-transcribes them.
    """
    engine = create_engine_for(os.environ["PEARSS_DATABASE_URL"])
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        job_id = ensure_collect_job(
            session,
            source_id="src-1",
            tenant_id="default",
            at="2026-01-01T00:00:00.000000Z",
        )
        for index in range(2):
            run_id = f"run-h{index}"
            create_job_run(
                session,
                run_id=run_id,
                job_id=job_id,
                correlation_id="c",
                at="2026-01-01T00:00:00.000000Z",
            )
            claim_job_run(session, run_id=run_id, at="2026-01-01T00:00:01.000000Z")
            finish_job_run(
                session,
                run_id=run_id,
                event=JobRunEvent.FAIL,
                correlation_id="c",
                error_class=ErrorClass.NETWORK,
                at="2026-01-01T00:00:02.000000Z",
            )
        session.execute(
            text(
                "insert into item (id, source_id, guid, content_hash, title,"
                " url, publish_status, tenant_id, created_at, updated_at)"
                " values ('i-hold', 'src-1', 'g-hold', 'h-hold', 'Held',"
                " 'https://e.com/held', 'pending', 'default',"
                " '2026-01-01T00:00:00.000000Z',"
                " '2026-01-01T00:00:00.000000Z')"
            )
        )
        session.commit()
    engine.dispose()

    async with mcp_session(mcp_url) as client:
        result = await client.read_resource(cast("Any", "ui://pearss/health"))
    first = result.contents[0]
    assert isinstance(first, TextResourceContents), first
    html = first.text

    assert '<tr data-error="network"><td>network</td><td>2</td></tr>' in html
    assert '<tr data-status="pending"><td>pending</td><td>1</td></tr>' in html


@pytest.mark.asyncio
async def test_the_health_panel_is_self_contained(mcp_url: str) -> None:
    """No scripts, no external references: the iframe needs nothing else.

    The sandbox's default posture is deny; a panel that pulled a stylesheet or
    script from the network would turn a read-only dashboard into a channel
    from the reader's context outward. Self-containment is pinned here so the
    CSP conversation never has to happen.
    """
    async with mcp_session(mcp_url) as client:
        result = await client.read_resource(cast("Any", "ui://pearss/health"))
    first = result.contents[0]
    assert isinstance(first, TextResourceContents), first
    html = first.text

    assert "<script" not in html.lower()
    assert "http://" not in html and "https://" not in html


# --------------------------------------------------------------------------
# ToolAnnotations: the standard hint set a host uses to gate and warn
# --------------------------------------------------------------------------
#
# docs/api/mcp-tool-guide.md section 7 requires the description to state a
# tool's side effects; ToolAnnotations is the machine-readable form of the same
# fact, which is what lets a host decide without parsing English. These assert
# the semantics rather than a field's presence: a read that claimed
# `read_only_hint` while writing would be worse than no annotation at all.


@pytest.mark.asyncio
async def test_read_tools_are_annotated_read_only(mcp_url: str) -> None:
    """The seven reads declare read_only_hint and open_world_hint=False.

    Enumerated rather than derived from the surface, so a tool that quietly
    changes category fails here instead of being grandfathered in.
    """
    reads = {
        "list_sources",
        "search_items",
        "get_item_body",
        "list_jobs",
        "list_runs",
        "list_enrichments",
        "list_plugins",
        "get_system_snapshot",
    }
    async with mcp_session(mcp_url) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}

    for name in sorted(reads):
        annotations = tools[name].annotations
        assert annotations is not None, f"{name} carries no annotations"
        assert annotations.read_only_hint is True, name
        assert annotations.destructive_hint is False, name
        assert annotations.open_world_hint is False, name


@pytest.mark.asyncio
async def test_the_deletes_are_annotated_destructive(mcp_url: str) -> None:
    """The three irreversible tools say so in a field, not only in prose.

    A host uses `destructive_hint` to add its own confirmation affordance; if
    it were left unset on a delete, the machine-readable contract would say
    "safe" while the description said "permanent".
    """
    deletes = {"remove_source", "delete_items", "delete_source_items"}
    async with mcp_session(mcp_url) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}

    for name in sorted(deletes):
        annotations = tools[name].annotations
        assert annotations is not None, f"{name} carries no annotations"
        assert annotations.read_only_hint is False, name
        assert annotations.destructive_hint is True, name


@pytest.mark.asyncio
async def test_reversible_writes_are_not_annotated_destructive(mcp_url: str) -> None:
    """A pause or a folder move is not destructive, and must not claim to be.

    Over-marking is its own failure: a host that warns on every write trains
    the user to click through, which is exactly what makes the real delete
    warning worthless.
    """
    reversible = {
        "update_schedule_policy",
        "set_source_status",
        "set_source_folder",
        "set_plugin_status",
    }
    async with mcp_session(mcp_url) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}

    for name in sorted(reversible):
        annotations = tools[name].annotations
        assert annotations is not None, f"{name} carries no annotations"
        assert annotations.read_only_hint is False, name
        assert annotations.destructive_hint is False, name


@pytest.mark.asyncio
async def test_tools_that_reach_outward_are_annotated_open_world(mcp_url: str) -> None:
    """Collection and provider-spending tools declare open_world_hint.

    A local mutation is bounded by this database; `trigger_job` makes a real
    request to a third party and `retry_enrichments` spends provider budget.
    A host that wants to treat those differently has only this field to go on.
    """
    open_world = {
        "add_source",
        "trigger_job",
        "backfill_source",
        "retry_enrichments",
        "submit_items",
        "set_plugin_status",
    }
    async with mcp_session(mcp_url) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}

    for name in sorted(open_world):
        annotations = tools[name].annotations
        assert annotations is not None, f"{name} carries no annotations"
        assert annotations.open_world_hint is True, name


@pytest.mark.asyncio
async def test_every_tool_declares_annotations(mcp_url: str) -> None:
    """No tool may ship without the hint set.

    The per-category tests above name the tools they know about; this one
    catches a tool added later that nobody annotated, which is the way this
    drifts in practice.
    """
    async with mcp_session(mcp_url) as client:
        tools = (await client.list_tools()).tools

    missing = [t.name for t in tools if t.annotations is None]
    assert not missing, f"tools without annotations: {sorted(missing)}"
