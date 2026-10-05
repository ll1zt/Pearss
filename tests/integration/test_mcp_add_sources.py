"""A batch of subscriptions is one job.

Scenario register (normative source: SCENARIOS.md; issue #159; AC-161..166).

`add_source` subscribes one source per call, so a list through MCP was a stack
of calls. `add_sources` is the batch tool ADR 0044 asks for: one call for the
whole list, one work handle back, and each URL's outcome in the work's ledger.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import sqlalchemy as sa

from app.main import create_app, create_engine_for
from tests.integration.conftest import ServerInThread, free_port, run_work_to_completion
from tests.migrated_schema import migrated_database

pytestmark = pytest.mark.integration

MCP_EDGE_TOKEN = "add-sources-mcp-token"
API_TOKEN = "add-sources-api-token"

_POLICY: dict[str, object] = {"type": "fixed", "interval": "30m"}


def feed(name: str) -> str:
    return f"https://feeds.example/{name}.xml"


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    root = tmp_path_factory.mktemp("addsources")
    url = migrated_database(root, "addsources.db")
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


def _subscribed_urls(database_url: str) -> set[str]:
    engine = create_engine_for(database_url)
    try:
        with engine.connect() as conn:
            return {
                str(url)
                for (url,) in conn.execute(sa.text("select url from source")).fetchall()
            }
    finally:
        engine.dispose()


async def _rpc(
    client: httpx.AsyncClient, base: str, params: dict[str, Any], *, rid: int
) -> dict[str, Any]:
    """One raw `tools/call`."""
    body = {
        "jsonrpc": "2.0",
        "id": rid,
        "method": "tools/call",
        "params": {
            **params,
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                # No elicitation capability: a write needs none (ADR 0058).
                "io.modelcontextprotocol/clientCapabilities": {},
            },
        },
    }
    response = await client.post(
        f"{base}/mcp",
        json=body,
        headers={
            "authorization": f"Bearer {MCP_EDGE_TOKEN}",
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
            "mcp-protocol-version": "2026-07-28",
            "mcp-method": "tools/call",
            "mcp-name": str(params.get("name", "")),
        },
    )
    if "text/event-stream" in response.headers.get("content-type", ""):
        for line in response.text.splitlines():
            if line.startswith("data:"):
                decoded: dict[str, Any] = json.loads(line[len("data:") :].strip())
                return decoded
        return {"raw": response.text}
    try:
        decoded = response.json()
    except ValueError:
        decoded = {"raw": response.text}
    return decoded


def _payload(reply: dict[str, Any]) -> dict[str, Any]:
    """The JSON body of a completed `tools/call` reply."""
    for block in reply.get("result", {}).get("content", []):
        text_value = block.get("text")
        if text_value:
            parsed: dict[str, Any] = json.loads(text_value)
            return parsed
    return {}


async def _call(base: str, arguments: dict[str, Any], *, rid: int) -> dict[str, Any]:
    """Call `add_sources` once and return its payload."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        reply = await _rpc(
            client, base, {"name": "add_sources", "arguments": arguments}, rid=rid
        )
    return _payload(reply)


@pytest.mark.asyncio
async def test_the_whole_batch_becomes_one_job_that_subscribes_every_url(
    base_url: str, env: dict[str, str]
) -> None:
    """AC-161 / AC-211: one call queues one work for the whole list.

    Since #199 the answer is a handle: the subscriptions land when the worker
    runs, and `work_units` is where each URL's outcome goes. Both halves are
    asserted here, because "queued" and "subscribed" are now two different
    moments and a test that checked only one of them would pass while the other
    was broken.
    """
    urls = [feed("a"), feed("b"), feed("c")]

    payload = await _call(base_url, {"urls": urls, "schedule_policy": _POLICY}, rid=1)

    assert "error" not in payload, payload
    assert payload["counts"]["total"] == 3, payload
    # Nothing is subscribed yet: the response is a handle, not a deed.
    assert _subscribed_urls(env["PEARSS_DATABASE_URL"]) == set()

    await run_work_to_completion(env, payload["work_id"])

    assert _subscribed_urls(env["PEARSS_DATABASE_URL"]) == set(urls)
