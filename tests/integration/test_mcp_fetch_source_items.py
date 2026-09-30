"""One confirmation covers a list of addresses, and only that list.

Scenario register (normative source: `SCENARIOS.md`; issue #171; AC-176).

The MCP face of `POST /v1/sources/{id}:fetch-items`. An agent is the caller
that discovers addresses, so the tool has to exist -- and it has to confirm:
the deployment would make real outbound requests to someone else's site, once
per address.

The dialog cannot render a list (an elicitation form carries primitives), so
the confirmation is count + preview + a fingerprint of the list. The scenario
that matters is the edited one: a client that accepted a dialog about [a, b]
must not be able to execute [a, c] with that acceptance.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.main import create_app, create_engine_for
from tests.integration.conftest import ServerInThread, free_port
from tests.migrated_schema import migrated_database

pytestmark = pytest.mark.integration

MCP_EDGE_TOKEN = "fetch-items-mcp-token"
API_TOKEN = "fetch-items-api-token"

SOURCE_URL = "https://items.example/feed.xml"
ASKED = [
    "https://items.example/blog/archives/1",
    "https://items.example/blog/archives/2",
]
EDITED = [
    "https://items.example/blog/archives/1",
    "https://items.example/blog/archives/9",
]
# Off the source's host: refused before any fetch, which is what makes an
# executed call observable here without reaching anyone's server.
OFF_HOST = [
    "https://elsewhere.example/blog/archives/1",
    "https://elsewhere.example/blog/archives/2",
]

# Without an elicitation-capable client the server refuses the write with
# MissingRequiredClientCapability and never asks the question these scenarios
# are about.
_ELICITATION_CAPABILITY: dict[str, dict[str, dict[str, object]]] = {
    "elicitation": {"form": {}}
}


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    root = tmp_path_factory.mktemp("fetchitems")
    url = migrated_database(root, "fetchitems.db")
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


def _seed_source(database_url: str) -> str:
    """Store an rss source through the repository and return its id.

    Through the repository rather than a raw INSERT: the identity is derived
    there, and a hand-written row would have to restate every NOT NULL column
    (and break on the next one the schema gains).
    """
    from datetime import UTC, datetime

    from adapters.repository import create_source
    from adapters.timestamps import fixed_width_utc as timestamp

    engine = create_engine_for(database_url)
    try:
        with sessionmaker(engine, expire_on_commit=False)() as db:
            row = create_source(
                db,
                source_type="rss",
                url=SOURCE_URL,
                canonical_url=SOURCE_URL,
                schedule_policy='{"type": "fixed", "interval": "30m"}',
                tenant_id="default",
                at=timestamp(datetime(2026, 9, 30, 12, 0, tzinfo=UTC)),
            )
            db.commit()
            return str(row.id)
    finally:
        engine.dispose()


def _item_count(database_url: str) -> int:
    engine = create_engine_for(database_url)
    try:
        with engine.connect() as conn:
            return int(conn.execute(sa.text("select count(*) from item")).scalar_one())
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


def _arguments(source_id: str, urls: list[str]) -> dict[str, Any]:
    return {"source_id": source_id, "urls": urls}


async def _ask(
    base: str, source_id: str, asked: list[str], *, replayed: list[str]
) -> dict[str, Any]:
    """Round 1 asks about `asked`; round 2 replays the answers with `replayed`."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        first = await _rpc(
            client,
            base,
            {"name": "fetch_source_items", "arguments": _arguments(source_id, asked)},
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
                "name": "fetch_source_items",
                "arguments": _arguments(source_id, replayed),
                "inputResponses": {key: {"action": "accept", "content": answers}},
                "requestState": state,
            },
            rid=2,
        )


@pytest.mark.asyncio
async def test_the_dialog_names_the_list_once(
    base_url: str, env: dict[str, str]
) -> None:
    """AC-176: one elicitation for the list, carrying its fingerprint."""
    source_id = _seed_source(env["PEARSS_DATABASE_URL"])

    async with httpx.AsyncClient(timeout=30.0) as client:
        reply = await _rpc(
            client,
            base_url,
            {"name": "fetch_source_items", "arguments": _arguments(source_id, ASKED)},
            rid=1,
        )

    result = reply["result"]
    assert len(result["inputRequests"]) == 1, result
    only = next(iter(result["inputRequests"].values()))
    assert "2" in only["params"]["message"], only["params"]["message"]
    properties = only["params"]["requestedSchema"]["properties"]
    # The list itself cannot ride a form, so the fingerprint is what makes the
    # dialog a confirmation *of* that list rather than of its length.
    assert "urls_fingerprint" in properties, properties
    assert properties["urls_fingerprint"]["default"] != properties["count"]["default"]


@pytest.mark.asyncio
async def test_the_fingerprint_changes_when_any_address_changes(
    base_url: str, env: dict[str, str]
) -> None:
    """AC-176: the dialog's fingerprint covers the list, not its length.

    Both lists have the same count and the same first address, so anything
    that pinned only those would show identical dialogs for two different
    requests. The fingerprint is asserted to differ -- the falsifiable half of
    "the user was shown *this* list".
    """
    source_id = _seed_source(env["PEARSS_DATABASE_URL"])

    async def asked_fingerprint(urls: list[str]) -> str:
        async with httpx.AsyncClient(timeout=30.0) as client:
            reply = await _rpc(
                client,
                base_url,
                {
                    "name": "fetch_source_items",
                    "arguments": _arguments(source_id, urls),
                },
                rid=1,
            )
        only = next(iter(reply["result"]["inputRequests"].values()))
        properties = only["params"]["requestedSchema"]["properties"]
        return str(properties["urls_fingerprint"]["default"])

    original = await asked_fingerprint(ASKED)
    edited = await asked_fingerprint(EDITED)

    assert len(ASKED) == len(EDITED) and ASKED[0] == EDITED[0]
    assert original != edited


@pytest.mark.asyncio
async def test_an_edited_list_is_refused_and_stores_nothing(
    base_url: str, env: dict[str, str]
) -> None:
    """AC-176: accepting a dialog about one list cannot execute another.

    Same count, same first address -- exactly the case a count-only check
    would accept -- and the list nobody was shown does not execute: the
    accepted state is bound to the arguments it was created for.
    """
    source_id = _seed_source(env["PEARSS_DATABASE_URL"])

    reply = await _ask(base_url, source_id, ASKED, replayed=EDITED)

    error = reply.get("error")
    assert error is not None, reply
    assert error["code"] == -32602, error
    assert _item_count(env["PEARSS_DATABASE_URL"]) == 0


@pytest.mark.asyncio
async def test_one_confirmation_covers_the_whole_list(
    base_url: str, env: dict[str, str]
) -> None:
    """AC-176: the accepted list is executed as one call, per address.

    The addresses are off the source's host, so the call is answered with one
    result per address and nothing is fetched -- which is exactly what makes
    the assertion usable here: the *execution* is observable (both addresses
    come back, attributed) without reaching anyone's server.
    """
    source_id = _seed_source(env["PEARSS_DATABASE_URL"])

    reply = await _ask(base_url, source_id, OFF_HOST, replayed=OFF_HOST)

    rendered = json.dumps(reply)
    assert rendered.count("elsewhere.example/blog/archives") >= 2, rendered
    assert "rejected" in rendered, rendered
    assert _item_count(env["PEARSS_DATABASE_URL"]) == 0
