"""The host <-> plugin-child RPC protocol (P2b, ADR 0017).

Line-delimited JSON-RPC 2.0 over stdio. One request line in, one response
line out — no streaming, no concurrency inside the child: a collector call
is synchronous by nature, and the host serialises access per plugin.

The message shapes live here (core, import-free of adapters) so both sides
speak the same frozen protocol: the host proxy in `app/plugin_runtime` and
the child entry point in `plugins/collectors/__main__`. Payloads ride as
plain JSON — `RawPayload` and `ItemDraft` are encoded via their field dicts,
because the child never imports host persistence machinery either.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final

# Methods the child serves. Frozen set: the protocol is versioned as a whole,
# and an unknown method is a protocol violation, not a feature probe.
METHODS: Final[frozenset[str]] = frozenset(
    {
        "match_url",
        "fetch",
        "parse",
        "collect_pages",
        "source_config",
        "backfill",
        "before_url",
        "oldest_cursor",
        "http",
        "ping",
        # The install-time contract check for an installed plugin (ADR 0023):
        # the host asks the child what its code actually implements and
        # compares that against the manifest. It belongs in the frozen set
        # rather than being probed, because a method the child might not know
        # is precisely what this set exists to rule out.
        "verify",
    }
)

PROTOCOL_VERSION: Final = 1
MAX_MESSAGE_BYTES: Final = 64 * 1024 * 1024  # 64 MB: parsed feed bodies ride here


@dataclass(frozen=True, slots=True, kw_only=True)
class Request:
    """One host -> child call."""

    id: int
    method: str
    params: dict[str, Any]

    def encode(self) -> bytes:
        line = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": self.id,
                "method": self.method,
                "params": self.params,
            },
            ensure_ascii=False,
        )
        return line.encode("utf-8")


@dataclass(frozen=True, slots=True, kw_only=True)
class Response:
    """One child -> host reply: either `result` or `error`, never both."""

    id: int
    result: Any = None
    # {"code": int, "message": str, "data": {...}} on failure.
    error: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def encode(self) -> bytes:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": self.id}
        if self.error is not None:
            payload["error"] = self.error
        else:
            payload["result"] = self.result
        line = json.dumps(payload, ensure_ascii=False)
        return line.encode("utf-8")


class ProtocolError(Exception):
    """A malformed message, unknown method, or version mismatch."""


def decode_request(line: bytes) -> Request:
    """Parse one request line; ProtocolError for anything malformed."""
    try:
        payload = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProtocolError(f"undecodable request: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProtocolError("request is not a JSON object")
    if payload.get("jsonrpc") != "2.0":
        raise ProtocolError("request is not jsonrpc 2.0")
    method = payload.get("method")
    if method not in METHODS:
        raise ProtocolError(f"unknown method {method!r}")
    request_id = payload.get("id")
    if not isinstance(request_id, int):
        raise ProtocolError("request id must be an integer")
    params = payload.get("params")
    if not isinstance(params, dict):
        raise ProtocolError("params must be an object")
    return Request(id=request_id, method=str(method), params=params)


@dataclass(frozen=True, slots=True, kw_only=True)
class ReverseCall:
    """A child -> host reverse call (e.g. host-proxied HTTP)."""

    method: str
    params: dict[str, Any]


def decode_message(line: bytes) -> Response | ReverseCall:
    """Parse one line as either a response or a reverse call.

    A reverse call carries `method` + `params` (a Request shape) with id=0;
    a response carries `result` or `error`. The host routes on this.
    """
    try:
        payload = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProtocolError(f"undecodable message: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProtocolError("message is not a JSON object")
    if (
        payload.get("method") is not None
        and "result" not in payload
        and "error" not in payload
    ):
        method = payload["method"]
        if method not in METHODS:
            raise ProtocolError(f"unknown method {method!r}")
        params = payload.get("params")
        if not isinstance(params, dict):
            raise ProtocolError("params must be an object")
        return ReverseCall(method=str(method), params=params)
    response = decode_response(line)
    return response


def decode_response(line: bytes) -> Response:
    """Parse one response line; ProtocolError for anything malformed."""
    try:
        payload = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProtocolError(f"undecodable response: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProtocolError("response is not a JSON object")
    request_id = payload.get("id")
    if not isinstance(request_id, int):
        raise ProtocolError("response id must be an integer")
    error = payload.get("error")
    if error is not None and not isinstance(error, dict):
        raise ProtocolError("error must be an object")
    return Response(id=request_id, result=payload.get("result"), error=error)


def error_result(code: str, message: str) -> dict[str, Any]:
    """The standard error object a child returns for a classified failure.

    `code` is the ErrorClass value; `data` may carry the classified detail.
    Application errors (a fetch that got a 403) are *results* with an error
    object, not transport failures — the child process itself is healthy.
    """
    return {"code": code, "message": message}
