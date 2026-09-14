"""Authentication and Host pinning for the MCP endpoint.

The MCP server is mounted as a sub-app, and a Starlette mount cannot take a
FastAPI dependency, so the check is an ASGI middleware wrapped around it rather
than a route guard.

Two separate opt-ins, deliberately:

- **`PEARSS_MCP_TOKEN`** gates the endpoint. It is its own credential, not
  `PEARSS_API_TOKEN`: revoking a laptop's MCP client should not log out the
  Core API, and the two are used by different software. The MCP server's own
  outbound calls to `/v1` still use the API token -- this token authenticates
  the edge, not the actor, which is why audit rows name `api:full` regardless
  of who connected (ADR 0010).
- **`PEARSS_MCP_ALLOWED_HOSTS`** widens the MCP SDK's DNS-rebinding protection.
  Unset means loopback only, which is the SDK default and is safe but makes the
  endpoint unreachable from any other device.

Fail-closed on the token: with none configured every MCP request is refused and
startup logs a warning. An unauthenticated control plane that can add, remove
and trigger sources is not a state to arrive at by omission.

Elicitation is not a substitute for any of this. It asks a *cooperative* client
to show the user a confirmation; a client that wants the write simply answers
yes. It bounds accidents, not adversaries.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
from collections.abc import Mapping
from typing import Any, Final

from starlette.types import Receive, Scope, Send

LOGGER = logging.getLogger(__name__)

MCP_TOKEN_ENV: Final = "PEARSS_MCP_TOKEN"  # noqa: S105
MCP_ALLOWED_HOSTS_ENV: Final = "PEARSS_MCP_ALLOWED_HOSTS"

# Always acceptable, so widening the list for a remote client never locks the
# machine out of its own endpoint. The `:*` suffix is required rather than
# cosmetic: the SDK matches the Host header exactly or via a `host:*` wildcard,
# and a browser sends the port, so a bare "127.0.0.1" rejects 127.0.0.1:8000.
_LOOPBACK_HOSTS: Final = (
    "127.0.0.1:*",
    "localhost:*",
    "[::1]:*",
    "127.0.0.1",
    "localhost",
)


def mcp_token() -> str:
    """The configured edge token, or "" when there is none."""
    return os.environ.get(MCP_TOKEN_ENV, "").strip()


def mcp_auth_configured() -> bool:
    """Whether the MCP endpoint has a credential to check against."""
    return bool(mcp_token())


def allowed_hosts() -> list[str]:
    """Host patterns the MCP transport should accept.

    Loopback is prepended unconditionally: without it, setting a single remote
    host would silently break the local client that everything else here is
    tested with.

    Entries are matched against the `Host` header, so a remote deployment needs
    whichever form its clients send: `host` (no port), `host:8000`, or
    `host:*` for any port.
    """
    raw = os.environ.get(MCP_ALLOWED_HOSTS_ENV, "").strip()
    configured = [part.strip() for part in raw.split(",") if part.strip()]
    return [*_LOOPBACK_HOSTS, *configured]


def _bearer_present(scope: Mapping[str, Any]) -> str:
    for name, value in scope.get("headers", []):
        if name.lower() != b"authorization":
            continue
        decoded = value.decode("latin-1", errors="replace").strip()
        scheme, _, token = decoded.partition(" ")
        if scheme.lower() == "bearer":
            return str(token.strip())
    return ""


class MCPPathGuard:
    """Let only MCP paths through; hand everything else back as 404.

    The MCP sub-app is mounted at the root so that the SDK's own route is the
    public path, and a root mount matches whatever no earlier route claimed.
    Without this, those leftovers land in the MCP layer and inherit its
    behaviour -- an unconfigured deployment answered 503 for every unknown URL,
    and the health probe and disabled docs paths were swallowed, which is both
    wrong (a missing route is 404) and informative (it advertises that an MCP
    endpoint exists).

    `prefix` is the public path; the sub-app serves there and nowhere else.
    """

    def __init__(self, app: Any, *, prefix: str = "/mcp") -> None:
        self._app = app
        self.app = app  # Starlette's mount typing reads `.app`
        self._prefix = prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self._app(scope, receive, send)
            return
        path = str(scope.get("path", ""))
        # `startswith` rather than equality: the transport and any protocol
        # metadata live under the same prefix, and this guard decides only
        # whether the MCP layer owns the path at all.
        if path == self._prefix or path.startswith(self._prefix + "/"):
            await self._app(scope, receive, send)
            return
        await _reject(
            scope,
            receive,
            send,
            status=404,
            code="not_found",
            message="No such endpoint.",
        )


class MCPAuthMiddleware:
    """Refuse any MCP request that does not carry the edge token.

    Runs before the MCP application, so an unauthenticated peer never reaches a
    tool -- it cannot enumerate tools, and cannot learn whether a source id
    exists by watching a tool's error shape.
    """

    def __init__(self, app: Any) -> None:
        self._app = app
        self.app = app  # Starlette's mount typing reads `.app`

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self._app(scope, receive, send)
            return

        expected = mcp_token()
        if not expected:
            await _reject(
                scope,
                receive,
                send,
                status=503,
                code="mcp_unconfigured",
                message=f"{MCP_TOKEN_ENV} is not set; the MCP endpoint is disabled.",
            )
            return

        presented = _bearer_present(scope)
        # Constant-time: the value is a secret, and a short-circuit compare on
        # one costs an attacker nothing but a little patience.
        if not presented or not hmac.compare_digest(
            presented.encode("utf-8"), expected.encode("utf-8")
        ):
            await _reject(
                scope,
                receive,
                send,
                status=401,
                code="unauthorized",
                message="A bearer token is required to use the MCP endpoint.",
            )
            return

        await self._app(scope, receive, send)


# Headers the Streamable HTTP transport mirrors from the JSON-RPC body so an
# intermediary can route and reject without parsing it.
METHOD_HEADER: Final = "mcp-method"
NAME_HEADER: Final = "mcp-name"
PROTOCOL_VERSION_HEADER: Final = "mcp-protocol-version"

# The JSON-RPC error code the spec allocates for a header/body disagreement.
HEADER_MISMATCH_CODE: Final = -32020

# Methods whose `params` carry a name the transport mirrors; the spec names
# exactly these three.
_NAME_BEARING_METHODS: Final = frozenset(
    {"tools/call", "resources/read", "prompts/get"}
)


def _header_value(scope: Mapping[str, Any], wanted: str) -> str | None:
    """One request header, case-insensitively; None when absent."""
    for name, value in scope.get("headers", []):
        if name.decode("latin-1").lower() == wanted:
            return str(value.decode("latin-1").strip())
    return None


def _decode_header_value(raw: str) -> str:
    """Undo the Base64 sentinel the spec defines for unsafe header values.

    `=?base64?...?=` marks a value that cannot ride as plain ASCII (non-ASCII,
    control characters, leading/trailing space). Servers must decode before
    comparing to the body, or a legitimate non-ASCII name would look like a
    mismatch.
    """
    if not (raw.startswith("=?base64?") and raw.endswith("?=")):
        return raw
    import base64

    encoded = raw[len("=?base64?") : -len("?=")]
    try:
        return base64.b64decode(encoded).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        # Leave it alone: the comparison below reports a mismatch, which is
        # the honest answer for a claim that cannot be decoded.
        return raw


class MCPHeaderValidationMiddleware:
    """Refuse a request whose mirrored headers disagree with its body.

    Spec, Streamable HTTP / Server Validation:

        Servers that process the request body MUST reject requests where the
        values specified in the headers do not match the corresponding values
        in the request body. This prevents potential security vulnerabilities
        when different components in the network rely on different sources of
        truth (e.g., a load balancer routing on the header value while the MCP
        server executes based on the body value).

    That divergence is the whole attack: a proxy that admits `tools/list`
    while the body says `tools/call` has authorized a different request than
    the one executed. The SDK streams the body without checking, so the check
    lives here, in front of the MCP application.

    Enforced only when the request carries a mirrored header at all: a legacy
    client sends none, and refusing those would break the backward
    compatibility the transport's own rules require us to keep. A modern
    client sends them, and then a wrong value is a failure rather than an
    omission.
    """

    def __init__(self, app: Any) -> None:
        self._app = app
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self._app(scope, receive, send)
            return

        declared_method = _header_value(scope, METHOD_HEADER)
        declared_version = _header_value(scope, PROTOCOL_VERSION_HEADER)
        if declared_method is None and declared_version is None:
            # A legacy client: no mirrored headers to disagree with.
            await self._app(scope, receive, send)
            return

        body = await _read_body(receive)
        try:
            payload = json.loads(body) if body else None
        except ValueError:
            # Malformed JSON is the MCP layer's error to report, not ours.
            await _replay(scope, receive, send, body, self._app)
            return

        problem = _header_problem(scope, payload, declared_method, declared_version)
        if problem is not None:
            await _reject_header_mismatch(send, problem)
            return

        await _replay(scope, receive, send, body, self._app)


def _header_problem(
    scope: Mapping[str, Any],
    payload: Any,
    declared_method: str | None,
    declared_version: str | None,
) -> str | None:
    """The first mismatch to report, or None when the headers agree."""
    if not isinstance(payload, dict):
        return None

    method = payload.get("method")
    # A header that is absent is not a disagreement: the spec's remedy for a
    # missing required header is the client's business, and the installed SDK
    # emits only the headers a given call needs. Comparing `None` against the
    # body would refuse every request that omitted one, which is a worse
    # failure than the mismatch this guards.
    if (
        isinstance(method, str)
        and declared_method is not None
        and declared_method != method
    ):
        return (
            f"{METHOD_HEADER} header value {declared_method!r} does not match "
            f"body value {method!r}"
        )

    params = payload.get("params")
    if isinstance(method, str) and method in _NAME_BEARING_METHODS:
        declared_name = _header_value(scope, NAME_HEADER)
        body_name: Any = None
        if isinstance(params, dict):
            body_name = params.get("uri" if method == "resources/read" else "name")
        # Same rule as the method header: absent means "not declared", which
        # this layer does not enforce; present-but-different is the attack.
        if isinstance(body_name, str) and declared_name is not None:
            decoded = _decode_header_value(declared_name)
            if decoded != body_name:
                return (
                    f"{NAME_HEADER} header value {declared_name!r} does not "
                    f"match body value {body_name!r}"
                )

    if declared_version is not None:
        meta = params.get("_meta") if isinstance(params, dict) else None
        body_version = (
            meta.get("io.modelcontextprotocol/protocolVersion")
            if isinstance(meta, dict)
            else None
        )
        if isinstance(body_version, str) and declared_version != body_version:
            return (
                f"{PROTOCOL_VERSION_HEADER} header value {declared_version!r} "
                f"does not match body value {body_version!r}"
            )
    return None


async def _read_body(receive: Receive) -> bytes:
    """Buffer the request body so it can be inspected and then replayed."""
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            break
        chunks.append(message.get("body", b""))
        if not message.get("more_body"):
            break
    return b"".join(chunks)


async def _replay(
    scope: Scope, receive: Receive, send: Send, body: bytes, app: Any
) -> None:
    """Hand the buffered body to the wrapped app as a fresh receive stream."""
    sent = False

    async def replay_receive() -> Any:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    await app(scope, replay_receive, send)


async def _reject_header_mismatch(send: Send, detail: str) -> None:
    """400 with `-32020`: the status and code the spec requires here."""
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": None,
            "error": {
                "code": HEADER_MISMATCH_CODE,
                "message": f"Header mismatch: {detail}",
            },
        }
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 400,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _reject(
    scope: Scope,
    receive: Receive,
    send: Send,
    *,
    status: int,
    code: str,
    message: str,
) -> None:
    """Answer in the same envelope the Core API uses.

    A client that speaks one API's error format should not have to learn a
    second one for a different port of the same process.

    Status-specific fields are deliberate. A 404 is a path this server does
    not serve, and saying anything about the MCP token there is both wrong
    (the token is not why the path is missing) and a disclosure (every
    mistyped URL would advertise that an MCP endpoint exists and name the
    variable that unlocks it). Only the 401/503 auth paths carry the
    challenge header for the same reason.
    """
    if status == 401:
        suggestion = (
            f"Set {MCP_TOKEN_ENV} and send it as `Authorization: Bearer <token>`."
        )
    elif status == 503:
        suggestion = f"Set {MCP_TOKEN_ENV} to enable the MCP endpoint."
    else:
        suggestion = "Check the path; this server serves /mcp."
    error: dict[str, object] = {
        "code": code,
        "message": message,
        "suggestion": suggestion,
    }
    headers = [
        (b"content-type", b"application/json"),
    ]
    if status in {401, 503}:
        headers.append((b"www-authenticate", b'Bearer realm="pearss-mcp"'))
    body = json.dumps({"error": error}).encode()
    headers.append((b"content-length", str(len(body)).encode()))
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": headers,
        }
    )
    await send({"type": "http.response.body", "body": body})


def warn_if_unconfigured() -> None:
    """Say so at startup if the endpoint is going to refuse everything."""
    if not mcp_auth_configured():
        LOGGER.warning(
            "%s unset; the MCP endpoint will reject every request", MCP_TOKEN_ENV
        )


__all__ = [
    "HEADER_MISMATCH_CODE",
    "MCP_ALLOWED_HOSTS_ENV",
    "MCP_TOKEN_ENV",
    "MCPAuthMiddleware",
    "MCPHeaderValidationMiddleware",
    "allowed_hosts",
    "mcp_auth_configured",
    "mcp_token",
    "warn_if_unconfigured",
]
