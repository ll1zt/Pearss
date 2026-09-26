"""A timeout for a half-open request header (issue #9).

A connection that sends a partial request header and then stops — the slowloris
shape — held a worker slot indefinitely. Measured on the deployed host
(2026-09-26): the uvicorn socket and the tailscale serve hop both kept such a
connection alive past **120 seconds**, and uvicorn exposes no setting for it
(``Config`` has keep-alive, notify and graceful-shutdown timeouts, and nothing
for "bytes stopped arriving mid-header").

Why this is a protocol class rather than middleware: ASGI middleware runs once
a request object exists, and a half-open header never produces one — the bytes
are still being collected by h11. The only place with both the transport and
the "no complete request yet" state is the protocol, so uvicorn's documented
``--http`` seam is used to install one:

    uvicorn app.main:app --http app.http_timeout:HeaderTimeoutProtocol

Semantics (deliberate, and each pinned by a test):

- The timer starts when the connection is made and is **reset by every byte
  received** until a complete request line/headers arrive. It targets *no
  progress*, not total duration, so a slow but dribbling client on a bad link
  is served rather than severed.
- Once the request is complete the timer never runs again for that connection.
  Otherwise a pooled keep-alive connection would be closed while idle and
  healthy, turning a security bound into flaky client failures.
- Expiry closes the transport. No response is owed: the client never finished
  asking anything, and writing a 408 to a peer that is not reading is its own
  resource cost.

The default is on, and a malformed setting falls back to it rather than
disabling it: a protection that only exists when an operator read the release
note is not a protection.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
from typing import Final

import h11
from uvicorn.protocols.http.h11_impl import H11Protocol
from uvicorn.protocols.utils import get_local_addr, get_remote_addr

LOGGER = logging.getLogger(__name__)

TIMEOUT_ENV: Final = "PEARSS_HTTP_HEADER_TIMEOUT_SECONDS"

# Generous enough that a slow link is not severed mid-header (each byte resets
# the window) and tight enough to bound the resource. nginx's own default for
# the equivalent directive is 60s; the point here is that a bound exists at all,
# and 15s is already far below the >120s measured with none.
DEFAULT_TIMEOUT_SECONDS: Final = 15.0
MAX_TIMEOUT_SECONDS: Final = 300.0


def header_timeout_seconds() -> float:
    """The configured header timeout, or a safe default.

    Every unusable value (absent, non-numeric, zero, negative, NaN, infinity)
    falls back to the default and is clamped to a maximum. Reading a bad value
    as 0 or inf would silently remove the protection, which is the failure this
    exists to prevent, so there is no way to configure "off" here.
    """
    raw = os.environ.get(TIMEOUT_ENV, "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        LOGGER.warning(
            "%s=%r is not a number; using %s", TIMEOUT_ENV, raw, DEFAULT_TIMEOUT_SECONDS
        )
        return DEFAULT_TIMEOUT_SECONDS
    if not math.isfinite(value) or value <= 0:
        LOGGER.warning(
            "%s=%r is not a positive finite number; using %s",
            TIMEOUT_ENV,
            raw,
            DEFAULT_TIMEOUT_SECONDS,
        )
        return DEFAULT_TIMEOUT_SECONDS
    if value > MAX_TIMEOUT_SECONDS:
        LOGGER.warning(
            "%s=%r exceeds the %s second maximum; clamping",
            TIMEOUT_ENV,
            raw,
            MAX_TIMEOUT_SECONDS,
        )
        return MAX_TIMEOUT_SECONDS
    return value


class HeaderTimeoutProtocol(H11Protocol):
    """h11 with a header-progress deadline (issue #9).

    Subclassing uvicorn's own h11 protocol rather than writing a protocol from
    scratch keeps every other behavior — request parsing, keep-alive, pipelining,
    WebSocket upgrade, the 400 responses — exactly as uvicorn ships it, so this
    file's only subject is the deadline.
    """

    # Set by `connection_made`; cancelled once a request has completed (see
    # `_update_deadline`) and rescheduled on every burst of header bytes until
    # then.
    _header_deadline: asyncio.TimerHandle | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        """Arm the deadline as soon as the connection exists.

        Arming here rather than on first byte is deliberate: a connection that
        sends nothing at all is the cheapest attack (it costs the client
        nothing) and would otherwise never be timed out.
        """
        # uvicorn annotates this parameter `Transport` while asyncio passes a
        # `BaseTransport`; upstream suppresses the same asymmetry on its own
        # definition. Narrowing the parameter here instead would break the
        # override for asyncio's call site, so the mismatch is silenced at the
        # call rather than papered over in the signature.
        super().connection_made(transport)  # type: ignore[arg-type]
        self._arm_header_deadline()

    def data_received(self, data: bytes) -> None:
        """Handle bytes, then rearm or disarm based on what h11 now holds.

        The decision is made *after* delegating, because only then has h11
        parsed the bytes: a write that completes a request must disarm, and one
        that leaves the request partial must rearm. Doing this before delegating
        was the first implementation, and it was wrong in a way only a real
        socket showed -- see `_has_progressed`.

        Note what is *not* asked here: whether h11 is currently mid-header.
        After a response completes h11 returns to IDLE, which is the same state
        a fresh connection waits in, so "is a header incomplete right now?"
        cannot tell a keep-alive socket apart from an attacker -- asking it is
        how the deadline got re-armed on an idle pooled connection and closed
        it mid-use.
        """
        super().data_received(data)
        self._update_deadline()

    def _update_deadline(self) -> None:
        """Arm while a request is incomplete; disarm once one has arrived.

        The distinction is `their_state is DONE`, and the two ways to get this
        wrong are both instructive because a real socket was needed to find
        them:

        * Treating `IDLE` as "no request" (the first attempt) never closed
          anything: h11 reports IDLE both for a connection that has sent
          nothing and for one whose request was already dispatched.
        * Tracking "has ever served a request" *stickily* (the second attempt,
          which fixed the keep-alive case) opened a hole instead: a client that
          sends a complete request and then a *partial* second one leaves h11
          in IDLE again, and refused to arm -- so slowloris survived over
          keep-alive. Measured: the partial second request stayed open
          indefinitely.

        So the state is re-derived from h11 on every byte: DONE means a request
        arrived (disarm -- the socket is legitimately idle between requests),
        and anything else means the parser is mid-connection, which covers both
        a fresh connection and the second half of a reused one.
        """
        transport = getattr(self, "transport", None)
        if transport is None or transport.is_closing():
            self._cancel_header_deadline()
            return
        if self._request_arrived():
            self._cancel_header_deadline()
            return
        self._arm_header_deadline()

    def _request_arrived(self) -> bool:
        """Whether h11 has a complete request in hand right now.

        `DONE` is the only state that means it: h11 sets it when the request is
        complete and reaches IDLE again once the response has been sent, which
        is why this is read rather than latched.
        """
        conn = getattr(self, "conn", None)
        return conn is not None and conn.their_state is h11.DONE

    def connection_lost(self, exc: Exception | None) -> None:
        """Cancel the deadline: a closed connection has nothing to protect."""
        self._cancel_header_deadline()
        super().connection_lost(exc)

    # -- the deadline itself -------------------------------------------------

    def _arm_header_deadline(self) -> None:
        self._cancel_header_deadline()
        loop = asyncio.get_event_loop()
        self._header_deadline = loop.call_later(
            header_timeout_seconds(), self._on_header_timeout
        )

    def _cancel_header_deadline(self) -> None:
        if self._header_deadline is not None:
            self._header_deadline.cancel()
            self._header_deadline = None

    def _on_header_timeout(self) -> None:
        """Close a connection whose request header never completed."""
        self._header_deadline = None
        if self._request_arrived():
            return  # the request arrived; the deadline was already disarmed
        transport = getattr(self, "transport", None)
        if transport is None or transport.is_closing():
            return
        client = get_remote_addr(transport)
        server = get_local_addr(transport)
        LOGGER.warning(
            "closing a connection from %s to %s: request header incomplete after %ss",
            client,
            server,
            header_timeout_seconds(),
        )
        transport.close()
