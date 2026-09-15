"""Public-exposure hardening tests (ADR 0016).

The rate limiter's clock is injected, so window arithmetic — the part that
decides whether a brute-force campaign is throttled — is tested
deterministically instead of by sleeping.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, MutableMapping
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


def _receive_stub() -> Receive:
    async def receive() -> dict[str, Any]:
        return {"type": "http.request"}

    return receive


def _send_stub() -> Send:
    async def send(message: MutableMapping[str, Any]) -> None:
        return None

    return send


# --------------------------------------------------------------------------
# The limiter, driven through __call__
#
# These tests used to reach into _record_failure / _block_remaining, which made
# them assertions about the private shape of the class: rename either method
# and every test here broke with the behaviour unchanged. They now drive the
# middleware the way Starlette does and assert what a client sees -- whether
# the request reached the app, and with what status -- which is the contract
# the class actually has.
#
# "Reached the app" is the observable that matters for a limiter: a blocked
# address must not consume application work, and a passing address must get
# its real response. A 401 from the inner app is simulated with a responder
# that answers 401, which is what turns a request into a "failure" for the
# limiter to count.
# --------------------------------------------------------------------------


def _auth_fail_app(
    hits: list[Scope],
) -> Callable[[Scope, Receive, Send], Awaitable[None]]:
    """An inner app that records it was reached and answers 401."""

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        hits.append(scope)
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    return app


def _failing_limiter(
    clock: _FakeClock, hits: list[Scope], max_failures: int = 3
) -> AuthFailureRateLimitMiddleware:
    return AuthFailureRateLimitMiddleware(
        _auth_fail_app(hits),
        max_failures=max_failures,
        window_seconds=300.0,
        block_seconds=900.0,
        monotonic=clock,
    )


def _scope(ip: str = "1.2.3.4") -> Scope:
    return {
        "type": "http",
        "method": "GET",
        "client": (ip, 5),
        "headers": [],
    }


async def _responses(
    limiter: AuthFailureRateLimitMiddleware, scope: Scope
) -> list[MutableMapping[str, Any]]:
    """Send one request through the limiter, collecting what it sent."""
    messages: list[MutableMapping[str, Any]] = []

    async def send(message: MutableMapping[str, Any]) -> None:
        messages.append(message)

    await limiter(scope, _receive_stub(), send)
    return messages


def _status(messages: list[MutableMapping[str, Any]]) -> int:
    return int(messages[0]["status"])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failures_below_the_budget_never_block() -> None:
    clock = _FakeClock()
    hits: list[Scope] = []
    limiter = _failing_limiter(clock, hits)

    # max_failures=3: the first two requests pass through to the app, each
    # counted as a failure on the way out, and no block exists yet.
    for _ in range(2):
        assert _status(await _responses(limiter, _scope())) == 401
    assert len(hits) == 2, "a passing request must reach the app"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_budget_exhaustion_blocks_for_the_block_window() -> None:
    clock = _FakeClock()
    hits: list[Scope] = []
    limiter = _failing_limiter(clock, hits)

    for _ in range(3):
        await _responses(limiter, _scope())
    reached = len(hits)
    assert reached == 3, "the failure that trips the block was itself a real request"

    # The next request is refused without reaching the app, with a Retry-After
    # naming the block window.
    messages = await _responses(limiter, _scope())
    assert len(hits) == reached, "a blocked request must not consume app work"
    assert _status(messages) == 429, messages
    # Retry-After is the remaining block rounded up to a whole second the
    # client can act on (the code's `int(remaining) + 1`): at exactly 900.0s
    # remaining the header says 901, because 900 would have the client retry
    # on the boundary second.
    retry_after = dict(messages[0]["headers"])[b"retry-after"]
    assert int(retry_after) == 901, retry_after


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_block_expires_and_the_budget_resets() -> None:
    clock = _FakeClock()
    hits: list[Scope] = []
    limiter = _failing_limiter(clock, hits)

    for _ in range(3):
        await _responses(limiter, _scope())
    assert _status(await _responses(limiter, _scope())) == 429

    clock.advance(901.0)
    # Past the block: the request passes again. The old failures were outside
    # the new window too, so a fresh campaign starts with full headroom and
    # trips again only on the third new failure.
    assert _status(await _responses(limiter, _scope())) == 401
    assert _status(await _responses(limiter, _scope())) == 401
    assert _status(await _responses(limiter, _scope())) == 401
    assert _status(await _responses(limiter, _scope())) == 429


@pytest.mark.unit
@pytest.mark.asyncio
async def test_addresses_are_isolated() -> None:
    clock = _FakeClock()
    hits: list[Scope] = []
    limiter = _failing_limiter(clock, hits)

    for _ in range(3):
        await _responses(limiter, _scope("1.2.3.4"))
    # The blocked address is blocked; another address is untouched.
    assert _status(await _responses(limiter, _scope("1.2.3.4"))) == 429
    assert _status(await _responses(limiter, _scope("5.6.7.8"))) == 401


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
