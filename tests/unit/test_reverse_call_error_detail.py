"""Reverse-call egress errors must name the step and the target host.

Issue #3: a 15-hour all-sources outage surfaced as
``{"error_class": "network", "detail": "ConnectTimeout: "}`` -- the exception
type with an empty ``str()`` appended, and no hint of which host was
unreachable. httpx's ``RequestError`` has an empty ``__str__`` when constructed
without a message, so the ``f"{type}: {exc}"`` formatting hollowed the detail
out, and the URL (in hand the whole time) was never written in. Three hosts
share one such message and the operator cannot tell which egress died.

The child's stdin is captured rather than spawned: what is under test is the
message the host writes back over the RPC stream, not the framing (pinned in
the protocol tests) nor the transport (pinned in the HTTP boundary tests).
"""

from __future__ import annotations

import json
from typing import Any, cast

import httpx
import pytest

from app.plugin_runtime import PluginChild
from core.plugins import protocol


class _StdinCapture:
    """Stand-in for the child's stdin pipe: captures the payload lines."""

    def __init__(self) -> None:
        self.chunks: list[bytes] = []

    def write(self, data: bytes) -> int:
        self.chunks.append(data)
        return len(data)

    def flush(self) -> None:
        return None


class _FakeProc:
    def __init__(self) -> None:
        self.stdin = _StdinCapture()


def _reverse_call_error(child: PluginChild, url: str) -> dict[str, Any]:
    child._service_reverse_call(
        protocol.ReverseCall(method="http", params={"method": "GET", "url": url})
    )
    proc = cast(_FakeProc, child._proc)
    line = proc.stdin.chunks[0]
    payload = json.loads(line)
    assert "error" in payload, payload
    error: dict[str, Any] = payload["error"]
    return error


@pytest.mark.unit
def test_an_empty_message_exception_still_names_the_type_and_host() -> None:
    """``ConnectTimeout: `` must become "ConnectTimeout (egress to host)".

    httpx built without a message renders as an empty string; the production
    outage showed that bare ``f"{type}: {exc}"`` produces a detail that looks
    truncated but never was. The host must also be named -- a bare type name
    still cannot tell CookieCloud egress from zhihu egress.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("")  # the production shape

    child = PluginChild("cc", http_transport=httpx.MockTransport(handler))
    child._proc = cast(Any, _FakeProc())
    error = _reverse_call_error(child, "http://cookiecloud.internal:4050/get/uuid?x=1")
    assert error["code"] == "network"
    detail = str(error["message"])
    assert "ConnectTimeout" in detail, detail
    assert "cookiecloud.internal:4050" in detail, detail
    assert not detail.rstrip().endswith(":"), detail
    # Host:port only -- the path carries the credential uuid, the query is
    # worse; neither may ride along with the detail.
    assert "/get/uuid" not in detail, detail
    assert "?x=1" not in detail, detail


@pytest.mark.unit
def test_a_nonempty_exception_message_is_kept_beside_the_host() -> None:
    """A real message survives, and the host joins it."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("peer stopped answering")

    child = PluginChild("zhihu", http_transport=httpx.MockTransport(handler))
    child._proc = cast(Any, _FakeProc())
    error = _reverse_call_error(child, "https://www.zhihu.com/api")
    detail = str(error["message"])
    assert "ReadTimeout" in detail, detail
    assert "peer stopped answering" in detail, detail
    assert "www.zhihu.com" in detail, detail
