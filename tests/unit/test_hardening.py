"""Public-exposure hardening tests (ADR 0016).

The rate limiter's clock is injected, so window arithmetic — the part that
decides whether a brute-force campaign is throttled — is tested
deterministically instead of by sleeping.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from starlette.types import Receive, Scope, Send

from app.hardening import (
    AuthFailureRateLimitMiddleware,
    BodySizeLimitMiddleware,
    SecurityHeadersMiddleware,
)


class _FakeClock:
    """A controllable monotonic clock."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _ok_app() -> Callable[[Scope, Receive, Send], Awaitable[None]]:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        return None

    return app


def _limiter(
    clock: _FakeClock, max_failures: int = 3
) -> AuthFailureRateLimitMiddleware:
    return AuthFailureRateLimitMiddleware(
        _ok_app(),
        max_failures=max_failures,
        window_seconds=300.0,
        block_seconds=900.0,
    )


def _receive_stub() -> Receive:
    async def receive() -> dict[str, Any]:
        return {"type": "http.request"}

    return receive


def _send_stub() -> Send:
    async def send(message: Any) -> None:
        return None

    return send


@pytest.mark.unit
def test_failures_below_the_budget_never_block() -> None:
    clock = _FakeClock()
    limiter = _limiter(clock)
    # max_failures=3: the first two leave one failure of headroom.
    assert limiter._record_failure("1.2.3.4", clock.now) is False
    assert limiter._record_failure("1.2.3.4", clock.now) is False
    assert limiter._block_remaining("1.2.3.4", clock.now) == 0.0


@pytest.mark.unit
def test_the_budget_exhaustion_blocks_for_the_block_window() -> None:
    clock = _FakeClock()
    limiter = _limiter(clock)
    assert limiter._record_failure("1.2.3.4", clock.now) is False
    assert limiter._record_failure("1.2.3.4", clock.now) is False
    # The third failure inside the window trips the block.
    assert limiter._record_failure("1.2.3.4", clock.now) is True
    assert limiter._block_remaining("1.2.3.4", clock.now) == 900.0


@pytest.mark.unit
def test_the_block_expires_and_the_budget_resets() -> None:
    clock = _FakeClock()
    limiter = _limiter(clock)
    for _ in range(3):
        limiter._record_failure("1.2.3.4", clock.now)

    clock.advance(901.0)
    assert limiter._block_remaining("1.2.3.4", clock.now) == 0.0
    # The old failures are outside the new window: a fresh campaign starts
    # with full headroom, and trips again only on the third new failure.
    assert limiter._record_failure("1.2.3.4", clock.now) is False
    assert limiter._record_failure("1.2.3.4", clock.now) is False
    assert limiter._record_failure("1.2.3.4", clock.now) is True


@pytest.mark.unit
def test_addresses_are_isolated() -> None:
    clock = _FakeClock()
    limiter = _limiter(clock)
    for _ in range(3):
        limiter._record_failure("1.2.3.4", clock.now)
    assert limiter._block_remaining("5.6.7.8", clock.now) == 0.0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_zero_budget_disables_the_limiter() -> None:
    calls: list[Scope] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        calls.append(scope)

    limiter = AuthFailureRateLimitMiddleware(
        app, max_failures=0, window_seconds=300.0, block_seconds=900.0
    )
    scope: Scope = {
        "type": "http",
        "method": "GET",
        "client": ("1.2.3.4", 5),
        "headers": [],
    }
    await limiter(scope, _receive_stub(), _send_stub())
    assert calls == [scope], "a disabled limiter must pass everything through"


# --------------------------------------------------------------------------
# ASGI behaviour
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_body_size_limit_rejects_early() -> None:
    reached: list[Scope] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        reached.append(scope)

    middleware = BodySizeLimitMiddleware(app, max_bytes=1024)
    scope: Scope = {
        "type": "http",
        "method": "POST",
        "client": ("1.2.3.4", 5),
        "headers": [(b"content-length", b"10485760")],
    }
    sent: list[Any] = []

    async def capture(message: Any) -> None:
        sent.append(message)

    await middleware(scope, _receive_stub(), capture)

    assert reached == [], "an oversized body must not reach the app"
    assert sent[0]["status"] == 413


@pytest.mark.unit
@pytest.mark.asyncio
async def test_undersized_bodies_pass_through() -> None:
    reached: list[Scope] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        reached.append(scope)

    middleware = BodySizeLimitMiddleware(app, max_bytes=1024)
    scope: Scope = {
        "type": "http",
        "method": "POST",
        "client": ("1.2.3.4", 5),
        "headers": [(b"content-length", b"64")],
    }
    await middleware(scope, _receive_stub(), _send_stub())
    assert reached == [scope]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_security_headers_are_stamped_on_every_response() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    sent: list[Any] = []

    async def capture(message: Any) -> None:
        sent.append(message)

    middleware = SecurityHeadersMiddleware(app)
    await middleware({"type": "http"}, _receive_stub(), capture)

    headers = dict((name, value) for name, value in sent[0]["headers"])
    assert headers[b"x-content-type-options"] == b"nosniff"
