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
    ) -> tuple[int, dict[str, Any]]:
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
        return response.status_code, payload

    async def get(self, path: str, **params: Any) -> tuple[int, dict[str, Any]]:
        return await self.request("GET", path, params=params or None)


def _infer_source_type(url: str) -> str:
    """Suggest a source type from the URL, before the operator overrides it.

    A URL a sandboxed collector claims (zhihu, telegram, x.com/home) cannot
    be collected by the rss collector - routing it as `rss` sends the rss
    collector at an HTML page and the origin answers 403 (observed when
    subscribing a Zhihu profile). So: a collector match means `web`; anything
    else stays `rss`, the default for a feed URL. `parser_plugin_id` exists
    for finer targeting; this inference only picks between rss and web.
    """
    from app import collector_registry

    for name, matcher in collector_registry.matchers():
        if name != "rss" and _safe_match(matcher, url):
            return "web"
    return "rss"


def _safe_match(matcher: Any, url: str) -> bool:
    """A matcher that raises (a malformed candidate URL) simply does not match."""
    try:
        return bool(matcher(url))
    except Exception:
        return False
