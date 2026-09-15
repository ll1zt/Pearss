"""The MCP tool surface, pinned against a committed snapshot.

Moved out of the integration suite so the contract layer owns its own
contract: `just contract` runs without the integration fixtures, and a
surface change shows up as a diff to a file under tests/contract/, which is
where a reviewer looks for contract changes.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.contract

REPO_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_PATH = REPO_ROOT / "tests" / "contract" / "mcp_tools.json"


def _surface(tools: list[Any]) -> dict[str, Any]:
    """The parts of the surface a client depends on.

    Names, descriptions, input schemas, annotations and `_meta` (which carries
    the MCP Apps `ui` binding -- a host renders the bound panel because of it,
    so a drift there is a contract change). Annotations are included because
    they are machine-readable behaviour: a host gates its own confirmation on
    `destructiveHint`, so flipping one changes what a user sees even though no
    description changed. Deliberately excluding the server version and anything
    time-dependent, which would churn the snapshot on unrelated releases and
    train reviewers to ignore its diffs.
    """
    return {
        tool.name: {
            "description": tool.description,
            "input_schema": tool.input_schema,
            **(
                {"annotations": tool.annotations.model_dump(exclude_none=True)}
                if tool.annotations
                else {}
            ),
            **({"meta": tool.meta} if tool.meta else {}),
        }
        for tool in sorted(tools, key=lambda item: item.name)
    }


@pytest.mark.asyncio
async def test_tool_surface_matches_the_snapshot(
    mcp_url: str, open_mcp_session: Callable[..., Any]
) -> None:
    """docs/api/mcp-tool-guide.md section 6 and architecture section 7.1.

    An agent that has adapted to this surface sees behaviour drift when it
    changes quietly, so a change must be a visible diff in a committed file.
    Set MCP_SNAPSHOT_UPDATE=1 to rewrite the snapshot after reviewing the
    change, never to make the test pass.
    """
    async with open_mcp_session(mcp_url) as session:
        current = _surface((await session.list_tools()).tools)

    if os.environ.get("MCP_SNAPSHOT_UPDATE") == "1":
        SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT_PATH.write_text(
            json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        pytest.skip("snapshot rewritten; review the diff and commit it")

    assert SNAPSHOT_PATH.exists(), (
        "tests/contract/mcp_tools.json is missing; run "
        "MCP_SNAPSHOT_UPDATE=1 just contract to create it"
    )
    expected = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    assert current == expected, (
        "the MCP tool surface changed; review the difference, then run "
        "MCP_SNAPSHOT_UPDATE=1 just contract and commit the new snapshot"
    )
