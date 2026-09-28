"""A write's result names the audit record it caused.

docs/api/mcp-tool-guide.md section 3 requires a tool result to carry
`audit_id` and `correlation_id`, so an operation is attributable: an agent that
changed the corpus can say which audit entry it produced, and an operator can
look that entry up instead of guessing from timestamps.

The property under test is *correspondence*, not the presence of a field: the
id the tool returns must name a row that exists, whose actor is the API caller
and whose operation names the endpoint that was hit. A tool that returned a
plausible-looking constant, or an id from an unrelated request, would satisfy a
presence check and fail this one.

Reads are the control. They are not audited (AuditLog is a fact store kept
forever), so a read tool that reported an audit id would be claiming a record
that does not exist -- the same defect from the other side.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from app.middleware import CorrelationAndAuditMiddleware
from tests.e2e.conftest import _auto_accept

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]


def _audit_row(session_factory: sessionmaker[Session], audit_id: str) -> Any:
    """The audit row with this id, or None. Returned as a row for direct reads.

    Drains first, because the row is written *after* the response is on the
    wire (the middleware's fire-and-forget contract, issue #18): looking it up
    straight after a tool call is a race that wins on an idle machine.
    """
    assert CorrelationAndAuditMiddleware.drain_audit(), (
        "the audit worker did not finish, so a missing row would prove nothing"
    )
    with session_factory() as db:
        return db.execute(
            sa.text(
                "select actor, operation, outcome, correlation_id"
                " from audit_log where id = :id"
            ),
            {"id": audit_id},
        ).fetchone()


def _tool_payload(result: Any) -> dict[str, Any]:
    """The structured content of a tool result, whatever shape the SDK used."""
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    for block in getattr(result, "content", []):
        text = getattr(block, "text", None)
        if text:
            import json

            loaded = json.loads(text)
            if isinstance(loaded, dict):
                return loaded
    raise AssertionError(f"no structured payload in tool result: {result!r}")


async def test_an_added_source_names_the_audit_row_it_created(
    mcp_url: str,
    open_mcp_session: Callable[..., Any],
    session_factory: sessionmaker[Session],
) -> None:
    """`add_source` returns an audit_id that names the row for that write.

    The row's operation must name the sources endpoint, which is what makes
    this the audit entry *for this write* rather than merely an entry that
    exists at the same time.
    """
    async with open_mcp_session(mcp_url, elicitation_callback=_auto_accept) as session:
        result = await session.call_tool(
            "add_source",
            {
                "url": "https://attribution.example/feed.xml",
                "schedule_policy": {"type": "fixed", "interval": "30m"},
            },
        )

    payload = _tool_payload(result)
    assert not payload.get("declined"), payload
    assert payload.get("audit_id"), f"no audit_id in write result: {payload}"

    row = _audit_row(session_factory, payload["audit_id"])
    assert row is not None, "the returned audit_id names no audit row"
    actor, operation, outcome, correlation_id = row
    assert actor == "api:full", "the write was not attributed to the API caller"
    assert "sources" in operation, operation
    assert outcome == "succeeded", outcome
    assert correlation_id, "the audit row carries no correlation id"


async def test_a_read_tool_claims_no_audit_record(
    mcp_url: str,
    open_mcp_session: Callable[..., Any],
    session_factory: sessionmaker[Session],
) -> None:
    """Reads are unaudited, so a read result must not report an audit_id.

    This is the control for the test above: a helper that stamped a
    constant onto every result would pass the write test and fail here,
    because there is no row for the read to point at.
    """
    async with open_mcp_session(mcp_url) as session:
        result = await session.call_tool("list_sources", {"limit": 5})

    payload = _tool_payload(result)
    assert "audit_id" not in payload, (
        f"a read result claimed an audit record: {payload.get('audit_id')!r}"
    )


async def test_the_audit_id_is_per_write_not_shared(
    mcp_url: str,
    open_mcp_session: Callable[..., Any],
    session_factory: sessionmaker[Session],
) -> None:
    """Two writes get two distinct audit ids, each naming its own row.

    A constant would make the first test pass and this one fail; a shared id
    would also mean the trail could not distinguish the two operations, which
    is the entire point of attribution.
    """
    async with open_mcp_session(mcp_url, elicitation_callback=_auto_accept) as session:
        first = _tool_payload(
            await session.call_tool(
                "add_source",
                {
                    "url": "https://attribution.example/one.xml",
                    "schedule_policy": {"type": "fixed", "interval": "30m"},
                },
            )
        )
        second = _tool_payload(
            await session.call_tool(
                "add_source",
                {
                    "url": "https://attribution.example/two.xml",
                    "schedule_policy": {"type": "fixed", "interval": "30m"},
                },
            )
        )

    assert first.get("audit_id") and second.get("audit_id")
    assert first["audit_id"] != second["audit_id"], "two writes shared one audit id"
    assert _audit_row(session_factory, first["audit_id"]) is not None
    assert _audit_row(session_factory, second["audit_id"]) is not None
