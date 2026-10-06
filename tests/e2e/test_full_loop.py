"""The end-to-end loop from docs/guides/testing.md, minus Compose.

The scenario the guide prescribed for P1:

1. (start services -- here: one process, real uvicorn thread + real MCP client
   + a local fixture origin)
2. discover_feed -- skipped: the tool does not exist yet (A5 backlog); the
   feed URL is known to the test
3. confirm and add_source            (MCP, with elicitation)
4. trigger_job, wait for the run     (MCP; trigger is synchronous)
5. assert the Item is stored ready/published
6. fetch the outputs, assert stability and no pending items
7. replay the same Idempotency-Key, assert no duplicate side effects

Everything here is real: the HTTP server, the auth middleware, the audit
trail, the MCP session manager, elicitation, the collector's fetch over TCP,
the Reader API. Only the origin is a fixture. The MCP surface is reached
through the same server as the REST surface -- it is mounted on the same app,
so driving both through one port is the honest topology, not a shortcut.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.middleware import CorrelationAndAuditMiddleware
from tests.e2e.conftest import API_TOKEN, READER_PASSWORD, READER_USERNAME

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]


def _payload(result: CallToolResult) -> dict[str, Any]:
    for block in result.content:
        text_value = getattr(block, "text", None)
        if text_value:
            return dict(json.loads(text_value))
    return {}


async def _call(session: Any, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    return _payload(await session.call_tool(tool, args))


def _rpc_error(excinfo: pytest.ExceptionInfo[BaseException]) -> MCPError:
    """The MCPError inside whatever the transport raised (task groups wrap)."""
    found: list[MCPError] = []

    def walk(exc: BaseException) -> None:
        if isinstance(exc, MCPError):
            found.append(exc)
        for sub in getattr(exc, "exceptions", []):
            walk(sub)

    walk(excinfo.value)
    assert found, f"no MCPError in {excinfo.value!r}"
    return found[0]


async def test_subscribe_collect_consume_replay(
    served: str,
    origin_url: str,
    session_factory: sessionmaker[Session],
    open_mcp_session: Callable[..., Any],
) -> None:
    mcp_endpoint = f"{served}/mcp"

    # -- 3. add_source (MCP) -------------------------------------------------
    # No elicitation capability is declared, and none is needed: a write does
    # not elicit (ADR 0058). This step used to assert the `-32021` refusal
    # first, then redo the call with a callback that accepted the dialog.
    async with open_mcp_session(mcp_endpoint) as session:
        added = await _call(
            session,
            "add_source",
            {
                "url": origin_url,
                "schedule_policy": {"type": "fixed", "interval": "30m"},
            },
        )
        source_id = added["id"]

        # -- 4. trigger and wait for the run --------------------------------
        triggered = await _call(session, "trigger_job", {"source_id": source_id})
        assert triggered.get("run_status") == "succeeded", triggered

    # -- 5. the item is stored, past the gate -------------------------------
    async with httpx.AsyncClient(
        base_url=served,
        headers={"Authorization": f"Bearer {API_TOKEN}"},
    ) as client:
        listed = await client.get("/v1/items")
        assert listed.status_code == 200, listed.text
        items = listed.json()["data"]
        assert items, "collection must have stored at least one item"
        assert all(row["publish_status"] in ("ready", "published") for row in items), (
            items
        )

        # -- 6a. the Reader API serves them --------------------------------
        token = await _reader_login(client)
        ids = await _reader_ids(client, token)
        assert ids, "the Reader API must expose the collected items"

        first = await _reader_contents(client, token, ids)
        again = await _reader_contents(client, token, ids)
        assert first == again, "serving content twice must be stable"

        # -- 6b. the RSS+ output layer renders the same items --------------
        _assert_rss_output(session_factory, {row["id"] for row in items})

        # -- 7. the same Idempotency-Key replays, not re-executes ----------
        body = {
            "url": origin_url,
            "schedule_policy": {"type": "fixed", "interval": "30m"},
        }
        first_add = await client.post(
            "/v1/sources", headers={"Idempotency-Key": "e2e-replay-1"}, json=body
        )
        assert first_add.status_code == 201
        assert first_add.json()["id"] == source_id, "identity is the canonical URL"

        replayed = await client.post(
            "/v1/sources", headers={"Idempotency-Key": "e2e-replay-1"}, json=body
        )
        assert replayed.status_code == 201
        assert replayed.headers.get("idempotent-replay") == "true"

    # Exactly one source exists, however many times it was added. The audit
    # row is written after the response, so wait for it before counting.
    CorrelationAndAuditMiddleware.drain_audit()
    with session_factory() as db:
        count = db.execute(text("select count(*) from origin")).scalar_one()
        assert count == 1, count
        audits = db.execute(
            text("select count(*) from audit_log where operation = :op"),
            {"op": "POST /v1/sources"},
        ).scalar_one()
        assert audits >= 2, "every accepted write that reached the Core API is audited"


# --------------------------------------------------------------------------
# Reader API helpers
# --------------------------------------------------------------------------


async def _reader_login(client: httpx.AsyncClient) -> str:
    response = await client.post(
        "/api/greader.php/accounts/ClientLogin",
        data={"Email": READER_USERNAME, "Passwd": READER_PASSWORD},
    )
    assert response.status_code == 200, response.text
    for line in response.text.splitlines():
        if line.startswith("Auth="):
            return line.removeprefix("Auth=").strip()
    raise AssertionError("no Auth= in the ClientLogin response")


async def _reader_ids(client: httpx.AsyncClient, token: str) -> list[str]:
    response = await client.get(
        "/api/greader.php/reader/api/0/stream/items/ids",
        headers={"Authorization": f"GoogleLogin auth={token}"},
        params={"output": "json", "n": 1000},
    )
    assert response.status_code == 200, response.text
    return [ref["id"] for ref in response.json().get("itemRefs", [])]


async def _reader_contents(
    client: httpx.AsyncClient, token: str, ids: list[str]
) -> list[dict[str, Any]]:
    response = await client.post(
        "/api/greader.php/reader/api/0/stream/items/contents",
        data={"T": token, "i": ids},
    )
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json().get("items", [])
    return items


# --------------------------------------------------------------------------
# The RSS+ output layer
# --------------------------------------------------------------------------


def _assert_rss_output(
    session_factory: sessionmaker[Session], api_item_ids: set[str]
) -> None:
    """The RSS+ output contract, at the library boundary it lives at.

    `render_feed` has no HTTP route yet (the Reader API is the mounted output
    surface), so the assertion runs against the repository state a route would
    read: the same visibility rule must select the same items, and the feed
    must carry each item's guid.
    """
    from adapters.repository import items_ready_for_feed
    from adapters.rss_out import FeedMeta, render_feed
    from app.pipeline import bodies_for

    with session_factory() as db:
        ready = items_ready_for_feed(db)
        rendered = render_feed(
            FeedMeta(title="E2E", link="https://e2e.example"),
            ready,
            bodies_for(db, ready),
        ).decode()

        assert "<rss" in rendered
        # The feed's items are exactly the ones the API showed: the visibility
        # rule agrees across both output surfaces.
        assert {item.id for item in ready} == api_item_ids
        for item in ready:
            assert item.guid in rendered, item.guid
