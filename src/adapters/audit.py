"""Audit logging.

AGENTS.md requires every API caller to pass through the Core API audit
middleware, not just MCP, and requires an actor and correlation id on every
write. This is the storage side of that: AuditLog is a fact source, so what
gets written here outlives the request and is backed up.

The redaction rules are the substance of this module. An audit trail that
records credentials is a credential leak with better retention, so arguments
are reduced to a safe summary before they reach the database.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from adapters.correlation import current
from adapters.db_models import AuditLog
from adapters.repository import timestamp
from core.enums import AuditOutcome

LOGGER = logging.getLogger(__name__)

# Header names whose values must never be recorded, in whole or in part.
SENSITIVE_HEADERS = frozenset(
    {
        "authorization",
        "cookie",
        "set-cookie",
        "proxy-authorization",
        "x-api-key",
        "x-reader-token",
        # X's `ct0` value, which is a login-derived session token and is sent
        # as this header by the authenticated collector (ADR 0012).
        "x-csrf-token",
    }
)

# Query parameters that carry credentials or private feed locations.
SENSITIVE_QUERY_KEYS = frozenset({"token", "key", "secret", "password", "auth"})

# Argument values are truncated: a body or a large filter must not end up in an
# audit row, and the row is retained indefinitely.
MAX_VALUE_CHARS = 200
MAX_ARGS = 24

# Keys whose values are dropped entirely rather than truncated, because even a
# prefix is sensitive.
SENSITIVE_ARG_KEYS = frozenset(
    {
        "password",
        "passwd",
        "token",
        "secret",
        "authorization",
        "cookie",
        "api_key",
        "content",
        "body",
        "html",
        "xml",
    }
)


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    """Keep only headers that are safe to retain.

    Sensitive ones are replaced with a marker rather than removed, so a reader
    can tell "a credential header was present" from "there was no auth at all"
    — that distinction is exactly what an investigation needs.
    """
    return {
        name: ("<redacted>" if name.lower() in SENSITIVE_HEADERS else value)
        for name, value in headers.items()
    }


def headers_from_scope(raw: list[tuple[bytes, bytes]]) -> dict[str, str]:
    """Decode ASGI's byte header pairs into text.

    The scope stores names and values as bytes. Passing those through unchanged
    breaks redaction twice over: a bytes key never equals a str entry in
    SENSITIVE_HEADERS, so Authorization slips through, and the resulting dict
    cannot be serialised to JSON at all. Decoding here is what lets the
    allowlist actually match.
    """
    return {
        name.decode("latin-1", errors="replace"): value.decode(
            "latin-1", errors="replace"
        )
        for name, value in raw
    }


def redact_args(args: dict[str, Any]) -> dict[str, str]:
    """Reduce request arguments to a safe, bounded summary.

    Values are stringified and truncated rather than nested: the point is to
    record what was asked for, not to keep a copy of the payload.
    """
    summary: dict[str, str] = {}
    for index, (key, value) in enumerate(args.items()):
        if index >= MAX_ARGS:
            summary["..."] = "truncated"
            break
        if key.lower() in SENSITIVE_ARG_KEYS:
            summary[key] = "<redacted>"
            continue
        summary[key] = _summarise(value)
    return summary


def _summarise(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    if len(text) > MAX_VALUE_CHARS:
        return f"{text[:MAX_VALUE_CHARS]}…({len(text)} chars)"
    return text


def redact_url(url: str) -> str:
    """Strip credentials and private query parameters from a URL.

    Private feed URLs carry tokens in their query string, and AGENTS.md forbids
    recording private URLs; the host and path are what identify the resource.
    """
    base, sep, query = url.partition("?")
    if not sep:
        return base
    kept = [
        pair
        for pair in query.split("&")
        if pair.split("=", 1)[0].lower() not in SENSITIVE_QUERY_KEYS
    ]
    return f"{base}?{'&'.join(kept)}" if kept else base


def record(
    session: Session,
    *,
    audit_id: str,
    actor: str,
    operation: str,
    outcome: AuditOutcome,
    args: dict[str, Any] | None = None,
    target: str | None = None,
    session_id: str | None = None,
    correlation_id: str | None = None,
    occurred_at: datetime | None = None,
) -> str:
    """Write one immutable audit record and return its id.

    `actor` is mandatory and non-empty by design: an anonymous audit row cannot
    be attributed, which defeats the purpose of the table. Callers that cannot
    name an actor should pass an explicit synthetic one (for example
    `system:scheduler`) rather than leaving it blank.
    """
    if not actor:
        raise ValueError("audit records require a non-empty actor")
    if not operation:
        raise ValueError("audit records require a non-empty operation")

    stamp = occurred_at or datetime.now(UTC)
    session.add(
        AuditLog(
            id=audit_id,
            # Fixed-width, like every other stored instant: audit rows are
            # ordered and read as text, and isoformat()'s omitted fractional
            # part at microsecond zero would mix widths in a fact-source table.
            occurred_at=timestamp(stamp),
            actor=actor,
            session_id=session_id,
            operation=operation,
            target=redact_url(target) if target else None,
            args_redacted=json.dumps(
                redact_args(args or {}), ensure_ascii=False, sort_keys=True
            ),
            outcome=outcome.value,
            correlation_id=correlation_id or current(),
        )
    )
    return audit_id
