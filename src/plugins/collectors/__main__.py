"""Child-process entry point: serve one collector over the RPC protocol.

Run as `python -m plugins.collectors <collector_id>`. The host (plugin
runtime) spawns this with the collector id, speaks line-delimited JSON-RPC
2.0 on stdio, and the child answers using the *same collector modules* the
in-process registry used — the only difference is the transport.

Trust boundary (ADR 0017): the child gets the collector's declared config
via the environment (injected by the host at spawn), never the host's full
environment. It performs no filesystem or database access; everything it
knows arrived in the request params.

Errors are values: a classified collection failure (HTTP 403 from the
origin, a parse error) is a *result* carrying an error object — the process
stays healthy and serves the next request. Only protocol violations or a
crash inside the child end the loop.
"""

from __future__ import annotations

import sys
from typing import Any, cast

import httpx

from core.plugins import protocol
from core.plugins.hooks import CollectorError

# Collector modules register lazily to keep startup cheap; the id decides
# which one this child serves.
_COLLECTORS: dict[str, Any] = {}
_CHILD_CLIENT: httpx.Client | None = None


def child_client() -> httpx.Client:
    """The child's own HTTP client, shared across calls in this process."""
    global _CHILD_CLIENT
    if _CHILD_CLIENT is None:
        _CHILD_CLIENT = httpx.Client(follow_redirects=True)
    return _CHILD_CLIENT


def _collector_for(collector_id: str) -> Any:
    """The collector module serving this child, imported on first use."""
    if not _COLLECTORS:
        from plugins.collectors.rss import collector as rss
        from plugins.collectors.telegram import collector as telegram
        from plugins.collectors.twitter import collector as twitter
        from plugins.collectors.zhihu import collector as zhihu
        from plugins.collectors.zsxq import collector as zsxq

        for collector_module in (rss, telegram, twitter, zhihu, zsxq):
            _COLLECTORS[collector_module.collector_id()] = collector_module
    module = _COLLECTORS.get(collector_id)
    if module is None:
        raise protocol.ProtocolError(f"no collector with id {collector_id!r}")
    return cast(Any, module)


def handle(collector_id: str, method: str, params: dict[str, Any]) -> Any:
    """Dispatch one RPC call against the collector's plain functions.

    Classified failures become error objects (values); the exceptions that
    escape are genuine protocol/child faults the host treats as a crash.
    """
    module = _collector_for(collector_id)

    if method == "ping":
        return {"collector_id": collector_id, "alive": True}

    if method == "match_url":
        return module.match_url(str(params["url"]))

    if method == "parse":
        # The raw payload arrives as its field dict; rebuild the value.
        from adapters.raw_payload import RawPayload

        raw = RawPayload(**params["raw"])
        return [draft.as_rpc_dict() for draft in module.parse(raw)]

    if method == "fetch":
        from adapters.http import FetchError
        from adapters.raw_payload import RawPayload

        try:
            payload = module.fetch(
                str(params["url"]),
                client=child_client(),
                source_id=str(params.get("source_id", "")),
                conditional=None,  # conditional state rides in params if sent
            )
        except FetchError as exc:
            return protocol.error_result(exc.error_class.value, exc.detail)
        except CollectorError as exc:
            return protocol.error_result(exc.error_class.value, exc.detail)
        return {"raw": payload.as_rpc_dict()}

    if method == "collect_pages":
        from adapters.repository import PagingState
        from plugins.collectors.twitter import walk

        result = walk.collect_pages(
            source_id=str(params["source_id"]),
            source_url=str(params["source_url"]),
            client=child_client(),
            state=PagingState(**params["state"]),
            max_pages=int(params.get("max_pages", 20)),
        )
        return {
            "drafts": [d.as_rpc_dict() for d in result.drafts],
            "newest_id": result.newest_id,
            "resume_cursor": result.resume_cursor,
            "error_class": (result.error_class.value if result.error_class else None),
            "detail": result.detail,
        }

    raise protocol.ProtocolError(f"unhandled method {method!r}")


def serve(collector_id: str) -> None:
    """The read-eval loop: one request line from stdin, one response to stdout."""
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = protocol.decode_request(line.encode("utf-8"))
        except protocol.ProtocolError as exc:
            # A malformed message can't be correlated to an id; fail the
            # stream — the host's restart policy owns recovery.
            print(
                protocol.Response(
                    id=0, error=protocol.error_result("protocol", str(exc))
                )
                .encode()
                .decode(),
                file=out,
                flush=True,
            )
            return

        try:
            result = handle(collector_id, request.method, request.params)
            response = protocol.Response(id=request.id, result=result)
        except CollectorError as exc:
            response = protocol.Response(
                id=request.id,
                error=protocol.error_result(exc.error_class.value, exc.detail),
            )
        except Exception as exc:  # child fault, not a classified failure
            response = protocol.Response(
                id=request.id,
                error=protocol.error_result(
                    "child_error", f"{type(exc).__name__}: {exc}"
                ),
            )
        print(response.encode().decode(), file=out, flush=True)


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: python -m plugins.collectors <collector_id>", file=sys.stderr)
        raise SystemExit(2)
    collector_id = sys.argv[1]
    # Fail fast if the id is unknown: a child that cannot serve its collector
    # must die at spawn, not answer every request with a protocol error.
    _collector_for(collector_id)
    serve(collector_id)


if __name__ == "__main__":
    main()
