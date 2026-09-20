"""Public-exposure hardening middleware (ADR 0016).

Pearss was built for localhost/Tailscale, where every caller is the operator.
Once a tunnel puts the Reader API on the public internet the server must
defend itself, and the three defenses here are deliberately dumb and cheap:

- **BodySizeLimitMiddleware** rejects oversized request bodies before a byte
  reaches the application. Without it an unauthenticated client can stream
  gigabytes into uvicorn's buffers.
- **AuthFailureRateLimitMiddleware** turns credential brute force into a
  waiting game: every 401/403 counts against the client address, and a
  client that exceeds the budget gets 429s instead of more guesses. The
  counters live in memory — restart forgets them, which trades a few
  requests of persistence for zero database machinery.
- **SecurityHeadersMiddleware** stamps every response with
  `X-Content-Type-Options: nosniff`.

Client IP comes from `scope["client"]`, which uvicorn fills from
X-Forwarded-For when started with `--proxy-headers` — the tunnel proxies run
on the same host, so their reported address is the real client's.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable, MutableMapping
from typing import Any, Final

from starlette.types import Receive, Scope, Send

LOGGER = logging.getLogger(__name__)

DEFAULT_MAX_BODY_BYTES: Final = 1024 * 1024
DEFAULT_FAILURE_MAX: Final = 50
DEFAULT_FAILURE_WINDOW_SECONDS: Final = 300.0
DEFAULT_FAILURE_BLOCK_SECONDS: Final = 900.0

_SUCCESS = 200
_TOO_LARGE = 413
_TOO_MANY = 429
_AUTH_FAILURES = frozenset({401, 403})

# Paths exempt from the failure limiter (issue #6). A liveness probe carries
# no credentials, so there is nothing to brute force on it -- but the limiter
# is the outermost middleware, so without an exemption a blocked address got
# 429 here too, replacing /healthz's liveness answer with the limiter's and
# blinding the monitor for the length of the block (15 min by default).
DEFAULT_EXEMPT_PATHS: Final = frozenset({"/healthz"})


def env_int(name: str, default: int) -> int:
    """Read an integer knob from the environment, falling back on garbage."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class BodySizeLimitMiddleware:
    """Reject request bodies over the configured size with 413.

    Content-Length based: a chunked body with no declared length cannot be
    priced in advance and is passed through — uvicorn still bounds it by the
    connection's own read timeout, and the tunnels that front a public
    deployment buffer the request themselves.
    """

    def __init__(self, app: Any, *, max_bytes: int = DEFAULT_MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and self.max_bytes > 0:
            for name, value in scope.get("headers", []):
                if name == b"content-length" and int(value) > self.max_bytes:
                    LOGGER.warning(
                        "rejected %d byte body from %s (limit %d)",
                        int(value),
                        scope.get("client"),
                        self.max_bytes,
                    )
                    response: dict[str, Any] = {
                        "type": "http.response.start",
                        "status": _TOO_LARGE,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"x-content-type-options", b"nosniff"),
                        ],
                    }
                    body = json.dumps(
                        {
                            "error": {
                                "code": "payload_too_large",
                                "message": (
                                    f"The request body exceeds {self.max_bytes} bytes."
                                ),
                                "suggestion": "Send a smaller request.",
                                "details": [],
                            }
                        }
                    ).encode()
                    response["headers"].append(
                        (b"content-length", str(len(body)).encode())
                    )
                    await send(response)
                    await send({"type": "http.response.body", "body": body})
                    return
        await self.app(scope, receive, send)


class EarlyAuthMiddleware:
    """Authenticate /v1 requests before any request-body parsing.

    FastAPI resolves dependencies (the auth dependency included) *after*
    the request body has been read and validated, so an unauthenticated
    caller could otherwise probe the validation behaviour of every write
    endpoint for free, and an oversized invalid body would spend the
    application's parse budget before anyone checked credentials. This
    middleware resolves the bearer token straight off the ASGI scope -
    no body access - and refuses unauthenticated or under-scoped requests
    up front.

    Method decides the required scope (reads for GET/HEAD/OPTIONS, write
    otherwise), mirroring the per-route dependencies. Those stay in place:
    they document the bearer scheme in the OpenAPI spec and publish the
    principal shape; against a pre-authenticated scope they simply pass.
    """

    # Only the Core API is bearer-token territory. The Reader API speaks
    # ClientLogin and the MCP endpoint has its own guard; neither should
    # see this middleware's error shape.
    _PROTECTED_PREFIXES: Final[tuple[str, ...]] = ("/v1",)
    _READ_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD", "OPTIONS"})

    # Actions whose scope is neither `read` nor `write`, keyed by the `:action`
    # suffix the API style guide already uses for non-CRUD operations.
    #
    # Method alone cannot decide here: `POST /v1/sources/{id}:ingest` is a
    # write in shape but must not accept the write token, and the ingest token
    # must not open the rest of the write surface (ADR 0022). Without this
    # table the middleware would demand `write` and refuse a valid ingest
    # token before the route's own dependency ever runs -- the narrow token
    # would be unusable, and the split would be nominal.
    #
    # Suffix matching is deliberately method-blind: the scope belongs to the
    # action, and an action does not change meaning with the verb. The
    # consequence to know about is that a future GET under an `:ingest` path
    # would require the ingest scope rather than `read` -- correct if it is
    # genuinely the same action, wrong if it is a read that merely shares the
    # path, in which case it needs a path of its own.
    #
    # Matching is on the whole suffix, so `/v1/sources/x:ingestX` and
    # `/v1/sources/x:ingest/extra` fall through to the method rule instead of
    # inheriting `ingest` from a prefix they happen to start with.
    _ACTION_SCOPES: Final[dict[str, str]] = {":ingest": "ingest"}

    def __init__(self, app: Any) -> None:
        self.app = app

    def _required_scope(self, path: str, method: str) -> str:
        for suffix, scope in self._ACTION_SCOPES.items():
            if path.endswith(suffix):
                return scope
        return "read" if method in self._READ_METHODS else "write"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if not path.startswith(self._PROTECTED_PREFIXES):
            await self.app(scope, receive, send)
            return

        from app.auth import (
            SCOPE_PRINCIPAL_KEY,
            authenticate,
        )

        method = scope.get("method", "GET")
        needed = self._required_scope(path, method)
        principal = authenticate(_ScopeHeaders(scope))
        if principal is None:
            LOGGER.info("early auth refused: unauthenticated, method=%s", method)
            await _send_auth_error(send, status=401, code="unauthenticated")
            return
        if not principal.may(needed):
            LOGGER.info(
                "early auth refused: scope=%s to principal=%s", needed, principal.name
            )
            await _send_auth_error(send, status=403, code="insufficient_scope")
            return
        # Published for the audit middleware and the route dependency's
        # short-circuit.
        scope[SCOPE_PRINCIPAL_KEY] = principal
        await self.app(scope, receive, send)


class _ScopeHeaders:
    """The header-only view of a request that `authenticate()` needs.

    auth.authenticate takes a Starlette Request; a full one cannot be built
    from a raw scope without touching the receive channel, which must be
    left untouched for the body the route will read. Header dict only.
    """

    def __init__(self, scope: Scope) -> None:
        self.headers = {
            name.decode("latin-1").lower(): value.decode("latin-1")
            for name, value in scope.get("headers", [])
        }


async def _send_auth_error(send: Send, *, status: int, code: str) -> None:
    """The documented error envelope, sent without entering the app."""
    body = json.dumps(
        {
            "error": {
                "code": code,
                "message": (
                    "A valid bearer token is required."
                    if status == 401
                    else "This endpoint requires a broader scope."
                ),
                "suggestion": (
                    "Send 'Authorization: Bearer <token>' with a token that "
                    "grants the required scope (PEARSS_API_TOKEN or "
                    "PEARSS_API_READ_TOKEN)."
                ),
                "details": [],
            }
        }
    ).encode()
    headers: list[tuple[bytes, bytes]] = [
        (b"content-type", b"application/json"),
        (b"x-content-type-options", b"nosniff"),
        (b"content-length", str(len(body)).encode()),
    ]
    if status == 401:
        headers.append((b"www-authenticate", b"Bearer"))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class SecurityHeadersMiddleware:
    """Stamp every response with `X-Content-Type-Options: nosniff`.

    One header, set globally rather than per endpoint, because the failure
    modes it prevents (an HTML body rendered as markup) exist wherever a
    response can be misread — including error paths that never reach a route.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                names = {name for name, _ in headers}
                if b"x-content-type-options" not in names:
                    headers.append((b"x-content-type-options", b"nosniff"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


class AuthFailureRateLimitMiddleware:
    """Rate limit credential brute force by client address.

    A 401 or 403 response is one failed guess. After `max_failures` failures
    inside `window_seconds`, the address is blocked for `block_seconds` and
    further requests get 429 with a Retry-After header. Counters are in
    memory and per-process — a restart or a second worker resets them, which
    is acceptable for a single-user deployment whose job is to make online
    guessing slow, not to build an intrusion-detection system.
    """

    def __init__(
        self,
        app: Any,
        *,
        max_failures: int = DEFAULT_FAILURE_MAX,
        window_seconds: float = DEFAULT_FAILURE_WINDOW_SECONDS,
        block_seconds: float = DEFAULT_FAILURE_BLOCK_SECONDS,
        monotonic: Callable[[], float] | None = None,
        exempt_paths: frozenset[str] | None = None,
    ) -> None:
        self.app = app
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self.block_seconds = block_seconds
        # Credential-free paths pass through untouched: never counted, never
        # blocked. Only liveness belongs here -- exempting an authenticated
        # path would hand brute force a budget-free route.
        self._exempt_paths = (
            DEFAULT_EXEMPT_PATHS if exempt_paths is None else exempt_paths
        )
        # Injected so the window arithmetic can be driven deterministically in
        # tests: what is under test is "N failures within the window block the
        # address", and observing that by sleeping out a real window is not a
        # test. Production passes None, meaning the real monotonic clock.
        self._monotonic = monotonic or time.monotonic
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._blocked_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def _client_ip(self, scope: Scope) -> str:
        client = scope.get("client")
        return str(client[0]) if client else ""

    def _prune_window(self, failures: deque[float], now: float) -> None:
        cutoff = now - self.window_seconds
        while failures and failures[0] <= cutoff:
            failures.popleft()

    def _record_failure(self, ip: str, now: float) -> bool:
        """Count one failure; return True when this failure trips the block."""
        with self._lock:
            failures = self._failures[ip]
            self._prune_window(failures, now)
            failures.append(now)
            if len(failures) >= self.max_failures:
                self._blocked_until[ip] = now + self.block_seconds
                failures.clear()
                return True
            return False

    def _block_remaining(self, ip: str, now: float) -> float:
        with self._lock:
            remaining = self._blocked_until.get(ip, 0.0) - now
            if remaining <= 0:
                self._blocked_until.pop(ip, None)
                return 0.0
            return remaining

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self.max_failures <= 0:
            await self.app(scope, receive, send)
            return

        # Exempt paths bypass the limiter entirely -- before the block check,
        # so a liveness probe answers while its source address is blocked,
        # and before the accounting, so it never spends the budget either.
        if str(scope.get("path", "")) in self._exempt_paths:
            await self.app(scope, receive, send)
            return

        ip = self._client_ip(scope)
        now = self._monotonic()
        remaining = self._block_remaining(ip, now)
        if remaining > 0:
            body = json.dumps(
                {
                    "error": {
                        "code": "rate_limited",
                        "message": "Too many failed attempts; try again later.",
                        "suggestion": (
                            f"Wait {int(remaining) + 1} seconds before retrying."
                        ),
                        "details": [],
                    }
                }
            ).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": _TOO_MANY,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                        (b"retry-after", str(int(remaining) + 1).encode()),
                        (b"x-content-type-options", b"nosniff"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return

        status_holder: dict[str, int] = {"status": _SUCCESS}

        async def send_with_status(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = int(message["status"])
            await send(message)

        await self.app(scope, receive, send_with_status)

        if status_holder["status"] in _AUTH_FAILURES and self._record_failure(ip, now):
            LOGGER.warning(
                "auth failures from %s exceeded %d in %ds; blocking for %ds",
                ip,
                self.max_failures,
                self.window_seconds,
                self.block_seconds,
            )
