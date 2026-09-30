"""The MCP server's caller of the Core API (ADR 0010).

Every tool is a thin wrapper over the Core API, called in-process through
httpx's ASGITransport. That routing is what satisfies AGENTS.md's rule that all
API callers go through the Core API audit middleware — a tool calling the
repository directly would be an unaudited, unauthenticated write path that
happens to look convenient.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

import httpx

# The token the server uses against its own Core API. Read scope is enough for
# the read tools; write scope is required for the ones that mutate, and each of
# those elicits first. Held here rather than passed per call because the server
# is one trusted internal client, not a proxy for arbitrary callers — see the
# attribution caveat in docs/adr/0010.
# Name of the environment variable that holds the Core API bearer credential.
# A variable NAME, never a value.
CORE_API_AUTH_ENV = "PEARSS_API_TOKEN"


def _fresh_key() -> str:
    """A new Idempotency-Key per tool invocation.

    Not derived from the arguments. A key stable per argument set would replay
    the first response forever, so a second deliberate "trigger this source"
    or "add this feed again" would silently return a stale answer instead of
    doing the thing asked. The Core API is already idempotent on source
    identity, so a duplicate add resolves correctly without a stable key.

    A per-call key still satisfies the contract AGENTS.md asks for — a retried
    *request* is deduped — while keeping distinct user intents distinct.
    """
    return f"mcp-{uuid.uuid4()}"


class CoreAPIClient:
    """In-process caller of the Core API.

    Uses ASGITransport against the same application object, so requests pass
    through the audit middleware, the bearer-token dependency and request
    validation exactly as an external client's would. Nothing is bypassed
    because the caller is local.
    """

    def __init__(self, app: Any) -> None:
        self._app = app
        # The application's own collector registry, so the URL-to-type
        # heuristic below asks the same collectors the rest of the application
        # uses. A caller with no application (the guard tests) leaves it None
        # and the heuristic falls back to the process default (issue #84).
        self.registry = getattr(getattr(app, "state", None), "collector_registry", None)

    def _token(self) -> str:
        return os.environ.get(CORE_API_AUTH_ENV, "")

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[int, dict[str, Any], str | None]:
        """Call the Core API, returning status, payload and this call's audit id.

        The audit id travels in the return value rather than in an attribute on
        this object. One client serves every request (`mcp_server.build_mcp_server`
        builds it once), the tool bodies are `async def`, and the server handles
        concurrent requests -- so an attribute here is shared mutable state that
        a second caller overwrites before the first reads it. The failure is
        silent and precise-looking: a caller reports an audit id that names
        somebody else's operation (issue #64).

        Returning it makes the correlation structural: the id cannot be
        separated from the response it describes, so no future `await` in a
        tool body can introduce the race again.
        """
        headers = {"Authorization": f"Bearer {self._token()}"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self._app),
            base_url="http://pearss.internal",
            timeout=60.0,
            headers=headers,
        ) as client:
            response = await client.request(method, path, json=json_body, params=params)
        try:
            payload: dict[str, Any] = response.json()
        except (json.JSONDecodeError, ValueError):
            payload = {"text": response.text}
        # Read from the response header rather than the body: the audit row is
        # written by the middleware after the response is sent, so the id can
        # only travel out-of-band. None on a read, which is not audited.
        return (
            response.status_code,
            payload,
            response.headers.get("x-audit-id"),
        )

    async def get(
        self, path: str, **params: Any
    ) -> tuple[int, dict[str, Any], str | None]:
        return await self.request("GET", path, params=params or None)
