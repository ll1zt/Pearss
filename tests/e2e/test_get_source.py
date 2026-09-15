"""`get_source`: one source in full, for diagnosis.

`list_sources` answers "which sources exist"; this answers "what is this one
configured to do, and how far has it reached". The distinction matters when
items look missing: the paging state (`seen_frontier`, `floor_cursor`) is what
bounds what collection has walked, and a truncated list row does not carry it.

The scenario is driven through the real MCP surface against a real database, so
what is asserted is what an agent would actually receive -- including that the
fields survive the tool layer's shrinker, which is the failure mode a
hand-written payload would hide.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from tests.e2e.conftest import _auto_accept

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]


def _payload(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    import json

    for block in getattr(result, "content", []):
        text = getattr(block, "text", None)
        if text:
            loaded = json.loads(text)
            if isinstance(loaded, dict):
                return loaded
    raise AssertionError(f"no structured payload: {result!r}")


async def _add_source(session: Any, url: str, **extra: Any) -> str:
    """Add a source through the tool and return the id it reports."""
    result = await session.call_tool(
        "add_source",
        {
            "url": url,
            "schedule_policy": {"type": "fixed", "interval": "30m"},
            **extra,
        },
    )
    payload = _payload(result)
    assert not payload.get("declined"), payload
    assert payload.get("id"), payload
    return str(payload["id"])


async def test_get_source_returns_the_configuration_of_one_source(
    mcp_url: str, open_mcp_session: Callable[..., Any]
) -> None:
    """The full record for a known id, including fields the list omits.

    Asserted on specific fields rather than "the call succeeded": the tool is
    a projection of the Core API, so the useful property is that the values
    that identify the source survived the trip.
    """
    async with open_mcp_session(mcp_url, elicitation_callback=_auto_accept) as session:
        source_id = await _add_source(
            session, "https://get-source.example/feed.xml", folder="阅读"
        )
        result = await session.call_tool("get_source", {"source_id": source_id})
        payload = _payload(result)

    assert payload.get("id") == source_id, payload
    assert payload.get("url") == "https://get-source.example/feed.xml", payload
    assert payload.get("folder") == "阅读", payload
    assert payload.get("schedule_policy") == {
        "type": "fixed",
        "interval": "30m",
    }, payload
    assert "status" in payload, payload
    # The paging fields are what this tool exists to expose. Null for a source
    # that has never been walked is correct and must be present, not absent:
    # a missing key cannot be distinguished from "not applicable".
    assert "seen_frontier" in payload, payload
    assert "floor_cursor" in payload, payload


async def test_get_source_reports_an_unknown_id_as_a_diagnosable_error(
    mcp_url: str, open_mcp_session: Callable[..., Any]
) -> None:
    """A missing source yields the shared error shape, not a silent empty body.

    The code must be the Core API's own (`source_not_found`), because that is
    what lets an agent branch without parsing prose; and the suggestion has to
    name the safe next step rather than leaving the caller to guess.
    """
    async with open_mcp_session(mcp_url) as session:
        result = await session.call_tool("get_source", {"source_id": "does-not-exist"})
        payload = _payload(result)

    assert payload.get("error") == "source_not_found", payload
    assert payload.get("suggestion"), payload


async def test_get_source_distinguishes_two_sources(
    mcp_url: str, open_mcp_session: Callable[..., Any]
) -> None:
    """Two ids return their own records, so the id argument is load-bearing.

    A tool that ignored its argument and returned the first source would pass a
    single-source test; this is the check that it does not.
    """
    async with open_mcp_session(mcp_url, elicitation_callback=_auto_accept) as session:
        first = await _add_source(session, "https://get-source.example/one.xml")
        second = await _add_source(session, "https://get-source.example/two.xml")

        one = _payload(await session.call_tool("get_source", {"source_id": first}))
        two = _payload(await session.call_tool("get_source", {"source_id": second}))

    assert one["id"] == first and two["id"] == second
    assert one["url"] != two["url"]
