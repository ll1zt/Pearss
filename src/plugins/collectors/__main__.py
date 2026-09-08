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

import base64
import sys
from typing import Any, cast

import httpx

from adapters.http import FetchError
from core.enums import ErrorClass
from core.plugins import protocol
from core.plugins.hooks import CollectorError

# Collector modules register lazily to keep startup cheap; the id decides
# which one this child serves.
_COLLECTORS: dict[str, Any] = {}
_CHILD_CLIENT: httpx.Client | None = None


class EgressRefusal(Exception):
    """The host refused or could not answer a reverse http call.

    Carries the host's classification code so the transport handler can
    re-raise it as the same classified failure the in-process path would
    have produced, instead of flattening every refusal into a connect
    error.
    """

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class HostProxy:
    """The child's view of the host: synchronous reverse "http" calls.

    Reverse calls ride id=0 (the host routes them by the marker before
    matching the outer request id) and block until the host answers.
    """

    def __init__(self, stdin: Any, stdout: Any) -> None:
        self._stdin = stdin
        self._stdout = stdout

    def __call__(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request = protocol.Request(id=0, method=method, params=params)
        self._stdin.write(request.encode().decode() + "\n")
        self._stdin.flush()
        line = self._stdout.readline()
        if not line:
            raise RuntimeError("host closed the reverse-call channel")
        response = protocol.decode_response(line.encode())
        if response.error is not None:
            raise EgressRefusal(
                str(response.error.get("code", "error")),
                str(response.error.get("message", "")),
            )
        return dict(response.result)


def child_client(host_http: Any = None) -> httpx.Client:
    """The client the collector's HTTP layer runs through.

    Without a host callback this is a plain client (direct egress). With
    one, egress is *proxied back to the host* over the RPC stream: the host
    enforces the network whitelist (E4) and test fixtures inject
    MockTransports exactly as they do for the in-process path.
    """
    global _CHILD_CLIENT
    if host_http is None:
        if _CHILD_CLIENT is None:
            _CHILD_CLIENT = httpx.Client(follow_redirects=True)
        return _CHILD_CLIENT
    return _host_proxied_client(host_http)


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


def handle(
    collector_id: str,
    method: str,
    params: dict[str, Any],
    host_http: Any = None,
) -> Any:
    """Dispatch one RPC call against the collector's plain functions.

    Classified failures become error objects (values); the exceptions that
    escape are genuine protocol/child faults the host treats as a crash.
    """
    module = _collector_for(collector_id)

    if method == "ping":
        return {"collector_id": collector_id, "alive": True}

    if method == "source_config":
        return module.source_config().as_dict()

    if method == "backfill":
        hooks = module.backfill()
        return hooks is not None

    if method == "match_url":
        return module.match_url(str(params["url"]))

    if method == "parse":
        # The raw payload arrives as its field dict; rebuild the value.
        from adapters.raw_payload import RawPayload

        raw = RawPayload.from_rpc_dict(params["raw"])
        return [draft.as_rpc_dict() for draft in module.parse(raw)]

    if method == "fetch":
        # Classified failures raise CollectorError; serve() turns that into
        # a response-level error object the host proxy re-raises as
        # FetchError. Returning an error_result as a *result* would lose the
        # result/error distinction the protocol defines.
        try:
            payload = module.fetch(
                str(params["url"]),
                client=child_client(host_http),
                source_id=str(params.get("source_id", "")),
                conditional=None,  # conditional state rides in params if sent
            )
        except FetchError as exc:
            raise CollectorError(exc.error_class, exc.detail) from exc
        return {"raw": payload.as_rpc_dict()}

    if method == "collect_pages":
        from adapters.repository import PagingState
        from plugins.collectors.twitter import walk

        result = walk.collect_pages(
            source_id=str(params["source_id"]),
            source_url=str(params["source_url"]),
            client=child_client(host_http),
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


def _host_proxied_client(host_http: Any) -> httpx.Client:
    """A client whose every request round-trips to the host over the RPC."""

    def handler(request: httpx.Request) -> httpx.Response:
        params = {
            "method": request.method,
            "url": str(request.url),
            # .raw yields bytes pairs by httpx's type; decode directly.
            "headers": [[k.decode(), v.decode()] for k, v in request.headers.raw],
            "content_b64": base64.b64encode(request.read()).decode("ascii"),
        }
        try:
            result = host_http("http", params)
        except EgressRefusal as exc:
            if exc.code in {member.value for member in ErrorClass}:
                # The host classified the refusal (its SSRF guard, a blocked
                # origin); re-raise it as that same classified failure so the
                # collector's HTTP layer passes it through unchanged.
                raise FetchError(ErrorClass(exc.code), exc.detail) from exc
            # No classification (egress denied, channel trouble): a transport
            # error, which the collector's HTTP layer maps to a
            # network-classified FetchError rather than a child crash.
            raise httpx.ConnectError(f"host egress failed: {exc}") from exc
        except Exception as exc:  # pragma: no cover - HostProxy wraps all errors
            raise httpx.ConnectError(f"host egress failed: {exc}") from exc
        headers = [(str(k), str(v)) for k, v in (result.get("headers") or {}).items()]
        body = base64.b64decode(str(result.get("body_b64", "")))
        return httpx.Response(int(result["status"]), headers=headers, content=body)

    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def serve(collector_id: str) -> None:
    """The read-eval loop: one request line from stdin, one response to stdout.

    `handle` may make synchronous reverse calls (host-proxied HTTP) through
    the HostProxy while processing one request; the loop itself stays the
    only reader/writer outside those windows.
    """
    out = sys.stdout
    # Reverse-call direction: the child *writes* the reverse request to its
    # stdout (which the host reads) and reads the answer from its stdin
    # (which the host writes). Same two pipes as normal RPC, opposite use.
    host_proxy = HostProxy(sys.stdout, sys.stdin)
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
            result = handle(collector_id, request.method, request.params, host_proxy)
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
