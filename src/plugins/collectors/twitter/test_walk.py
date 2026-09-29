"""Direct tests for the paged-walk entry point.

`collect_pages` normally runs in the sandboxed child process, whose code the
parent's coverage cannot see. These call it in-process -- with the origin
stubbed by a MockTransport, the same tool the client tests use -- so its own
branching (success, partial failure, classification) is exercised and measured
here as well as through the child.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from core.enums import ErrorClass
from core.paging import WalkState
from plugins.collectors.twitter import client as x_client
from plugins.collectors.twitter import walk

pytestmark = pytest.mark.unit

Route = Callable[[httpx.Request], httpx.Response]


def _timeline(ids: list[str], next_cursor: str | None) -> bytes:
    entries: list[dict[str, object]] = [
        {
            "entryId": f"tweet-{rid}",
            "sortIndex": rid,
            "content": {
                "itemContent": {
                    "tweet_results": {
                        "result": {
                            "__typename": "Tweet",
                            "rest_id": rid,
                            "legacy": {
                                "full_text": f"body of {rid}",
                                "created_at": "Sun Aug 09 16:00:00 +0000 2026",
                                "entities": {"urls": [], "media": []},
                            },
                        }
                    }
                }
            },
        }
        for rid in ids
    ]
    if next_cursor:
        entries.append(
            {
                "entryId": f"cursor-bottom-{next_cursor}",
                "content": {"value": next_cursor},
            }
        )
    return json.dumps(
        {
            "data": {
                "home": {
                    "home_timeline_urt": {
                        "instructions": [
                            {"type": "TimelineAddEntries", "entries": entries}
                        ]
                    }
                }
            }
        }
    ).encode()


def _cursor(request: httpx.Request) -> str | None:
    value = json.loads(request.content)["variables"].get("cursor")
    return str(value) if value is not None else None


def _run(route: Route, monkeypatch: pytest.MonkeyPatch) -> walk.PagedWalkResult:
    monkeypatch.setenv(x_client.AUTH_TOKEN_ENV, "synthetic-auth")
    monkeypatch.setenv(x_client.CT0_ENV, "synthetic-ct0")
    with httpx.Client(transport=httpx.MockTransport(route)) as client:
        return walk.collect_pages(
            source_id="src-x",
            source_url="https://x.com/home",
            client=client,
            state=WalkState(seen_frontier=100),
        )


def _first_page_then(failure: Exception) -> Route:
    def route(request: httpx.Request) -> httpx.Response:
        if _cursor(request) is None:
            return httpx.Response(200, content=_timeline(["300", "200"], "C1"))
        raise failure

    return route


class _Unnamed(Exception):
    """A failure whose class is not a member of the taxonomy."""

    error_class = "not-a-class"


@pytest.mark.unit
def test_a_clean_walk_reports_no_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def route(request: httpx.Request) -> httpx.Response:
        if _cursor(request) is None:
            return httpx.Response(200, content=_timeline(["300", "200"], "C1"))
        return httpx.Response(200, content=_timeline(["150", "50"], None))

    result = _run(route, monkeypatch)

    assert result.error_class is None
    assert result.newest_id == 300


@pytest.mark.unit
def test_a_partial_network_failure_keeps_its_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The class the failing fetch carried survives the walk, not `blocked`."""
    result = _run(_first_page_then(httpx.ConnectError("dropped")), monkeypatch)

    assert result.error_class is ErrorClass.NETWORK


@pytest.mark.unit
def test_a_failure_with_no_class_falls_back_to_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _run(_first_page_then(RuntimeError("boom")), monkeypatch)

    assert result.error_class is ErrorClass.NETWORK


@pytest.mark.unit
def test_a_failure_naming_an_unknown_class_falls_back_to_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _run(_first_page_then(_Unnamed("boom")), monkeypatch)

    assert result.error_class is ErrorClass.NETWORK
