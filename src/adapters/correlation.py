"""Request correlation.

A correlation id ties one caller-visible request to the log lines, audit rows
and job runs it produced. docs/api/style-guide.md requires accepting an inbound
`X-Request-ID`, generating one when absent, echoing it back, and mapping it to
`correlation_id` internally.

A contextvar is used rather than threading the id through every signature: the
id is needed at the points that record things (audit, logs), which are many and
mostly unrelated to each other, and passing it as a parameter everywhere would
put an observability concern into domain signatures.

This lives in adapters, not core: it is infrastructure, not domain logic, and
keeping it out of core is what lets core stay free of ambient state.
"""

from __future__ import annotations

import secrets
import string
from contextvars import ContextVar, Token

# Set by the HTTP middleware; read by anything recording an event.
_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)

# Characters a caller could not use to break a log line or a header value.
# Built from string.ascii_letters rather than a literal containing "a-zA-Z":
# set() over such a string yields only the characters actually present, so the
# ranges are not expanded and most letters are silently rejected.
_ALLOWED = frozenset(string.ascii_letters + string.digits + "-_.:")
MAX_INBOUND_LENGTH = 64


def new_correlation_id() -> str:
    """Generate a fresh id. Unpredictable, so ids cannot collide across
    concurrent requests from the same client."""
    return secrets.token_hex(8)


def sanitize_inbound(value: str | None) -> str | None:
    """Validate a caller-supplied `X-Request-ID`, or reject it.

    The header is untrusted input: an attacker-controlled string that ends up in
    a log line or echoed back in a header could inject newlines (forging log
    entries) or break the response. Anything unsafe is dropped so the caller
    gets a generated id instead, which is also the correct behaviour for a
    malformed one — a bad inbound id must not fail an otherwise valid request.
    """
    if not value:
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > MAX_INBOUND_LENGTH:
        return None
    # Reject control characters and anything outside the allowlist outright
    # rather than stripping them: a mangled id is worse than a fresh one,
    # because it looks like it came from the client.
    if any(char not in _ALLOWED for char in candidate):
        return None
    return candidate


def current() -> str:
    """The active correlation id, generating one if none is set.

    Returning a generated id rather than None keeps every recorded event
    traceable even when it originates outside a request — a scheduler run, a
    CLI invocation, a test.
    """
    active = _correlation_id.get()
    if active is not None:
        return active
    return new_correlation_id()


def set_correlation_id(value: str) -> Token[str | None]:
    """Install `value` for the current context; returns a token to reset with."""
    return _correlation_id.set(value)


def reset(token: Token[str | None]) -> None:
    _correlation_id.reset(token)
