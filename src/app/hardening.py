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
from collections.abc import MutableMapping
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
    ) -> None:
        self.app = app
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self.block_seconds = block_seconds
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._blocked_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def _monotonic(self) -> float:
        """The clock seam; tests freeze it instead of sleeping."""
        return time.monotonic()

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
