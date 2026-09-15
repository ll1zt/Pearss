"""MCP server: the agent-facing control plane.

Every tool is a thin wrapper over the Core API, called in-process through
httpx's ASGITransport. That routing is what satisfies AGENTS.md's rule that all
API callers go through the Core API audit middleware — a tool calling the
repository directly would be an unaudited, unauthenticated write path that
happens to look convenient. See docs/adr/0010.

Presentation rules come from docs/api/mcp-tool-guide.md: compact returns,
truncated strings, no bodies in tool results (those are Resources), and
elicitation on every write with the confirmed arguments identical to the ones
executed.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.caching import CacheHint
from mcp.server.mcpserver import MCPServer

from app.mcp.core_client import CoreAPIClient

LOGGER = logging.getLogger(__name__)

SERVER_NAME = "pearss"


def build_mcp_server(app: Any) -> MCPServer:
    """Construct the server with its tools and resources bound to `app`."""
    core = CoreAPIClient(app)

    # Apps-bound tools and their resources must be registered before the
    # MCPServer is constructed: the extension is consumed in __init__, and
    # a binding added after that never reaches tools/list.
    from app.mcp.tools.resources import build_apps_extension, register_item_resource

    apps = build_apps_extension(core)

    server = MCPServer(
        name=SERVER_NAME,
        instructions=(
            "Pearss is a personal information feed system. Use these tools to "
            "manage sources, inspect collection runs, and search collected "
            "items. Article bodies are Resources, not tool results, so request "
            "them only when needed. Writes ask the user to confirm first."
        ),
        # The spec requires a freshness hint on every cacheable result
        # (tools/list, prompts/list, resources/list, resources/read,
        # resources/templates/list, server/discover). The tool surface changes
        # only when a release does, so five minutes saves a re-fetch every turn
        # while staying wrong for at most five minutes after a deploy.
        #
        # `private` for both: the surface is identical for every caller today,
        # but it is served from an authenticated endpoint and there is no
        # second caller to share a cache with. Choosing `public` would buy
        # nothing and would have to be re-justified if that ever changed.
        cache_hints={
            "tools/list": CacheHint(ttl_ms=300_000, scope="private"),
            "resources/list": CacheHint(ttl_ms=300_000, scope="private"),
            "resources/templates/list": CacheHint(ttl_ms=300_000, scope="private"),
            # A body is immutable once published (the gate freezes it), but a
            # pending item can become readable at any moment -- so the hint is
            # zero and the client re-checks rather than caching a 404-shaped
            # answer for a resource that is about to exist.
            "resources/read": CacheHint(ttl_ms=0, scope="private"),
        },
        extensions=[apps],
    )

    # ---------------------------------------------------------------- reads
    from app.mcp.tools.reads import register_reads

    register_reads(server, core)

    # ---------------------------------------------------------------- writes
    from app.mcp.tools.writes import register_writes

    register_writes(server, core)

    register_item_resource(server, core)

    return server


def build_mcp_app(app: Any) -> Any:
    """The MCP Starlette app, on the current protocol, with Host pinning.

    The SDK rejects any Host it does not recognise, which by default means only
    loopback reaches the endpoint. `allowed_hosts` comes from the environment so
    a deployment that serves a Tailscale address says so explicitly rather than
    getting remote access by accident.

    `stateless_http=True` selects the 2026-07-28 protocol rather than the
    initialize-handshake one. That protocol removed protocol-level sessions, so
    every request carries its version and capabilities in `_meta` and is
    answerable on its own. Two consequences matter here:

    - `server/discover` (a MUST on that protocol) is only reachable this way:
      the legacy transport gates every method but `ping` behind a completed
      handshake, so on the legacy path the method simply does not exist.
    - the cache hints configured in `build_mcp_server` are only emitted on
      this path; the legacy wire has no place to put `ttlMs`/`cacheScope`.

    We keep no cross-request state on the MCP surface, so nothing is lost by
    dropping the session: the tools are stateless by construction and every
    write is confirmed by elicitation.
    """
    from mcp.server.transport_security import TransportSecuritySettings

    from app.mcp_guard import allowed_hosts

    server = build_mcp_server(app)
    # The SDK's own path stays `/mcp`; the *mount* is what decides the public
    # path. Mounting a `/mcp`-routing app under `/mcp` nested the two into
    # `/mcp/mcp`, so create_app mounts this at the root and guards it by prefix
    # instead -- see MCPPathGuard.

    return server.streamable_http_app(
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts(),
        ),
    )
