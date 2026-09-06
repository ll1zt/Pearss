"""Core API middleware: correlation and audit.

AGENTS.md requires every API caller to pass through the Core API audit
middleware rather than auditing only at the MCP layer, and requires an actor
plus a correlation id on every write. Doing it here means a future endpoint
cannot forget to audit, which is the reason the rule names the middleware as
the place.

Written as pure ASGI rather than BaseHTTPMiddleware: the latter runs the
downstream app in a separate task, and a contextvar set by the middleware is
then not reliably visible to the endpoint that reads it. Correlation ids are
the whole point of this middleware, so the propagation guarantee matters.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import MutableMapping
from typing import Any

from fastapi import Request
from sqlalchemy.orm import sessionmaker
from starlette.types import Receive, Scope, Send

from adapters import correlation
from adapters.audit import headers_from_scope, record, redact_headers
from app.auth import principal_from_scope
from core.enums import AuditOutcome

LOGGER = logging.getLogger(__name__)

SessionFactory = sessionmaker[Any]

# Paths that carry no information worth retaining forever. AuditLog is a fact
# source and is backed up, so recording every health probe and every read would
# grow the database for no investigative benefit.
UNAUDITED_PATHS = frozenset({"/healthz", "/docs", "/openapi.json", "/redoc"})

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Auth failures are audited even though they are reads: a burst of 401s is how
# a guessed token looks in the data, and that is worth keeping.
AUTH_FAILURE_STATUSES = frozenset({401, 403})

ScopeState = MutableMapping[str, Any]


def _should_audit(scope: Scope, status: int) -> bool:
    path = scope.get("path", "")
    if path in UNAUDITED_PATHS or path.startswith("/docs"):
        return False
    method = scope.get("method", "")
    if method in WRITE_METHODS:
        return True
    return status in AUTH_FAILURE_STATUSES


def _actor(scope: ScopeState) -> str:
    """Name the caller.

    Falls back to `anonymous` rather than leaving the field empty: an
    unattributable row is worse than an honest one. A refused request has no
    principal by definition, so `anonymous` on a 401 is accurate rather than a
    gap in the record.
    """
    return principal_from_scope(scope) or "anonymous"


def _operation(scope: ScopeState) -> str:
    route = scope.get("route")
    if route is not None and getattr(route, "path", None):
        return f"{scope.get('method', '?')} {route.path}"
    return f"{scope.get('method', '?')} {scope.get('path', '?')}"


class CorrelationAndAuditMiddleware:
    """Assign a correlation id, echo it, and audit meaningful operations."""

    def __init__(self, app: Any, *, session_factory: SessionFactory) -> None:
        self.app = app
        self.session_factory = session_factory

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        inbound = correlation.sanitize_inbound(request.headers.get("x-request-id"))
        correlation_id = inbound or correlation.new_correlation_id()
        token = correlation.set_correlation_id(correlation_id)

        status = 500
        sent = False

        async def send_wrapper(message: MutableMapping[str, Any]) -> None:
            nonlocal status, sent
            if message["type"] == "http.response.start":
                status = int(message["status"])
                # Echo the id the caller will use to ask about this request.
                message["headers"] = _with_header(
                    message["headers"], b"x-request-id", correlation_id
                )
                sent = True
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            # Reset before the task ends so a reused context cannot leak one
            # request's id into the next.
            correlation.reset(token)

        if not sent:
            return
        if _should_audit(scope, status):
            self._record(scope, status, correlation_id)

    def _record(self, scope: ScopeState, status: int, correlation_id: str) -> None:
        try:
            with self.session_factory() as session:
                record(
                    session,
                    # Random per record, not derived from the correlation id:
                    # that id may echo a client-supplied X-Request-ID, and a
                    # client that reuses one would collide on this primary
                    # key, silently dropping every audit row after the first.
                    audit_id=f"audit-{secrets.token_hex(8)}",
                    actor=_actor(scope),
                    operation=_operation(scope),
                    # <400 succeeded; any 4xx was refused by us on the caller's
                    # behalf; only 5xx means we failed. Collapsing 422 into
                    # "failed" would make a typo in a request body look like a
                    # server incident in the audit trail.
                    outcome=(
                        AuditOutcome.SUCCEEDED
                        if status < 400
                        else AuditOutcome.REJECTED
                        if status < 500
                        else AuditOutcome.FAILED
                    ),
                    target=scope.get("path"),
                    args={
                        "headers": redact_headers(
                            headers_from_scope(list(scope.get("headers", [])))
                        )
                    },
                    correlation_id=correlation_id,
                )
                session.commit()
        except Exception:
            # An audit write must not mask the response the caller asked for,
            # but losing it silently would be worse: log at error level so the
            # gap is visible in the journal.
            LOGGER.exception("failed to write audit record for %s", correlation_id)


def _with_header(
    headers: list[tuple[bytes, bytes]], name: bytes, value: str
) -> list[tuple[bytes, bytes]]:
    """Replace `name` in a raw ASGI header list, keeping everything else."""
    kept = [pair for pair in headers if pair[0].lower() != name.lower()]
    kept.append((name, value.encode("latin-1")))
    return kept
