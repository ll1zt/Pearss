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
    """
    import json

    body = json.dumps(
        {
            "error": {
                "code": code,
                "message": message,
                "suggestion": (
                    f"Set {MCP_TOKEN_ENV} and send it as "
                    "`Authorization: Bearer <token>`."
                    if status == 401
                    else f"Set {MCP_TOKEN_ENV} to enable the MCP endpoint."
                ),
            }
        }
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"www-authenticate", b'Bearer realm="pearss-mcp"'),
            ],
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
    "MCP_ALLOWED_HOSTS_ENV",
    "MCP_TOKEN_ENV",
    "MCPAuthMiddleware",
    "allowed_hosts",
    "mcp_auth_configured",
    "mcp_token",
    "warn_if_unconfigured",
]
