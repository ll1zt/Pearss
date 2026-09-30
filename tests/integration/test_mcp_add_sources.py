"""One confirmation covers a batch of subscriptions.

Scenario register (normative source: SCENARIOS.md; issue #159; AC-161..166).

`add_source` elicits once per source, so subscribing a list through MCP is a
stack of dialogs. `add_sources` is the batch tool ADR 0044 asks for: the user is
asked once, the dialog names the batch (count + URL preview + a fingerprint),
and a client that edits the list cannot get the edited list executed.
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
from tests.integration.conftest import ServerInThread, free_port
from tests.migrated_schema import migrated_database

pytestmark = pytest.mark.integration

MCP_EDGE_TOKEN = "add-sources-mcp-token"
API_TOKEN = "add-sources-api-token"

_POLICY: dict[str, object] = {"type": "fixed", "interval": "30m"}

# Without an elicitation-capable client the server refuses the write with
# MissingRequiredClientCapability and never asks the question these scenarios
# are about.
_ELICITATION_CAPABILITY: dict[str, dict[str, dict[str, object]]] = {
    "elicitation": {"form": {}}
}


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
    """One raw `tools/call`, so the MRTR rounds are observable."""
    body = {
        "jsonrpc": "2.0",
        "id": rid,
        "method": "tools/call",
        "params": {
            **params,
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": _ELICITATION_CAPABILITY,
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


async def _ask(base: str, asked: list[str], *, replayed: list[str]) -> dict[str, Any]:
    """Round 1 asks about `asked`; round 2 replays the answers with `replayed`."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        first = await _rpc(
            client,
            base,
            {
                "name": "add_sources",
                "arguments": {"urls": asked, "schedule_policy": _POLICY},
            },
            rid=1,
        )
        result = first["result"]
        state = result["requestState"]
        key = next(iter(result["inputRequests"]))
        properties = result["inputRequests"][key]["params"]["requestedSchema"][
            "properties"
        ]
        answers = {name: spec.get("default") for name, spec in properties.items()}
        return await _rpc(
            client,
            base,
            {
                "name": "add_sources",
                "arguments": {"urls": replayed, "schedule_policy": _POLICY},
                "inputResponses": {key: {"action": "accept", "content": answers}},
                "requestState": state,
            },
            rid=2,
        )


@pytest.mark.asyncio
async def test_the_dialog_asks_about_the_batch_once(base_url: str) -> None:
    """AC-161: one elicitation names the batch; there is no per-URL dialog.

    The observable is the number of `tools/call` rounds the framework asks for:
    one round carrying the question, whatever the batch size.
    """
    urls = [feed("x"), feed("y"), feed("z")]

    async with httpx.AsyncClient(timeout=30.0) as client:
        reply = await _rpc(
            client,
            base_url,
            {
                "name": "add_sources",
                "arguments": {"urls": urls, "schedule_policy": _POLICY},
            },
            rid=1,
        )

    result = reply["result"]
    assert len(result["inputRequests"]) == 1, result
    only = next(iter(result["inputRequests"].values()))
    assert "3" in only["params"]["message"], only["params"]["message"]
    properties = only["params"]["requestedSchema"]["properties"]
    assert "urls_fingerprint" in properties, properties


@pytest.mark.asyncio
async def test_the_whole_batch_is_created_after_one_confirmation(
    base_url: str, env: dict[str, str]
) -> None:
    """AC-161: accepting the one dialog subscribes every URL."""
    urls = [feed("a"), feed("b"), feed("c")]

    reply = await _ask(base_url, urls, replayed=urls)

    assert "error" not in reply, reply
    assert _subscribed_urls(env["PEARSS_DATABASE_URL"]) == set(urls)


@pytest.mark.asyncio
async def test_an_edited_batch_is_refused_and_creates_nothing(
    base_url: str, env: dict[str, str]
) -> None:
    """AC-162: a list the user never saw cannot be executed.

    The question showed A and B; the retry submits A and C. Same count, same
    first URL -- so this is exactly the case a count-only check would accept.
    """
    asked = [feed("keep-a"), feed("keep-b")]
    edited = [feed("keep-a"), feed("swapped-in")]

    reply = await _ask(base_url, asked, replayed=edited)

    error = reply.get("error")
    assert error is not None, reply
    assert error["code"] == -32602, error

    # Neither the list the user saw nor the edited one landed. Asserted per URL
    # rather than "the table is empty": this module shares one database, so the
    # honest claim is about this batch.
    subscribed = _subscribed_urls(env["PEARSS_DATABASE_URL"])
    assert feed("keep-a") not in subscribed, subscribed
    assert feed("keep-b") not in subscribed, subscribed
    assert feed("swapped-in") not in subscribed, subscribed
