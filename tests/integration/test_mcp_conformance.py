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
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult, TextResourceContents
from sqlalchemy import text
from uvicorn.config import Config as UvicornConfig
from uvicorn.server import Server

from app.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]
MCP_EDGE_TOKEN = "conformance-edge-token"
API_TOKEN = "conformance-api-token"
MCP_PATH = "/mcp/mcp"

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def mcp_env(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    root = tmp_path_factory.mktemp("conformance")
    url = str(sa.URL.create("sqlite+pysqlite", database=str(root / "conf.db")))
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")

    engine = sa.create_engine(url)
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


class _ServerInThread:
    def __init__(self, app: Any, port: int) -> None:
        self._server = Server(
            UvicornConfig(app, host="127.0.0.1", port=port, log_level="critical")
        )
        self._port = port

    def __enter__(self) -> _ServerInThread:
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
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
def base_url(mcp_env: dict[str, str]) -> Iterator[str]:
    previous = {key: os.environ.get(key) for key in mcp_env}
    os.environ.update(mcp_env)
    try:
        app = create_app()
        with _ServerInThread(app, 8093):
            yield "http://127.0.0.1:8093"
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
    async with (
        http_client,
        streamable_http_client(mcp_url, http_client=http_client) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        with pytest.raises(MCPError) as excinfo:
            await session.read_resource(cast("Any", "item://does-not-exist"))

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
    engine = sa.create_engine(os.environ["PEARSS_DATABASE_URL"])
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
    async with (
        http_client,
        streamable_http_client(mcp_url, http_client=http_client) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        result = await session.read_resource(cast("Any", "item://i-ready"))

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
