"""Conventions every /v1 route module shares: errors, cursors, idempotency.

These lived inside `sources_routes` because that file was written first, which
put items/plugins/system in the odd position of importing their error envelope
from a sibling business module. They are not sources functionality: they are
the API-wide contract that docs/api/style-guide.md describes, and this module
is now their one home.

Three rules travel together:

- every failure answers in the same envelope, with a correlation id the
  client can quote (api_error)
- every list is page-addressed by an opaque cursor the client must not parse
  (encode_cursor / decode_cursor)
- every write carries an Idempotency-Key, the first response is stored, and a
  retry with the same key replays it -- with URLs query-redacted, because the
  stored body is an exposure surface of its own (idempotency_key /
  replay_or_none / remember)
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Header, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import sessionmaker

from adapters.correlation import current as current_correlation_id
from adapters.repository import find_idempotent, store_idempotent, timestamp

LOGGER = logging.getLogger(__name__)

# How long a stored response is replayed. Long enough to cover a client that
# retries after a timeout and then again a few seconds later; short enough that
# the table does not grow without bound.
IDEMPOTENCY_WINDOW = timedelta(days=7)


# --------------------------------------------------------------------------
# Error envelope
# --------------------------------------------------------------------------
# (moved verbatim from sources_routes; see the module docstring)


def api_error(
    *,
    status_code: int,
    code: str,
    message: str,
    suggestion: str,
    error_class: str | None = None,
    details: list[Any] | None = None,
    headers: dict[str, str] | None = None,
) -> HTTPException:
    """Build an HTTPException carrying the documented error envelope.

    The correlation id is embedded so a client can quote one value when
    reporting a problem, and `code` is stable while `message` is allowed to
    change — clients branch on the former.

    `headers` exists for the answers that carry a retry interval: HTTP has a
    header for that, and a response whose body says "try later" while its
    headers say nothing makes every caller invent its own backoff.
    """
    return HTTPException(
        status_code=status_code,
        headers=headers,
        detail={
            "error": {
                "code": code,
                "message": message,
                "error_class": error_class,
                "suggestion": suggestion,
                "details": details or [],
                "correlation_id": current_correlation_id(),
            }
        },
    )


# --------------------------------------------------------------------------
# Pagination
# --------------------------------------------------------------------------


def encode_cursor(offset: int) -> str:
    """An opaque cursor. Clients must not parse it, so its layout is free to
    change; only the value it decodes to is meaningful."""
    return base64.urlsafe_b64encode(f"v1:{offset}".encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode()).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise api_error(
            status_code=400,
            code="invalid_cursor",
            message="The cursor could not be decoded.",
            suggestion="Omit the cursor to start from the beginning.",
        ) from None
    prefix, _, digits = decoded.partition(":")
    if prefix != "v1" or not digits.isdigit():
        raise api_error(
            status_code=400,
            code="invalid_cursor",
            message="The cursor is not valid for this API version.",
            suggestion="Omit the cursor to start from the beginning.",
        )
    offset = int(digits)
    if offset > 10_000_000:
        raise api_error(
            status_code=400,
            code="invalid_cursor",
            message="The cursor is not valid for this API version.",
            suggestion="Page through the list instead of seeking.",
        )
    return offset


def encode_keyset_cursor(sort_value: str, last_id: str) -> str:
    """An opaque keyset cursor: the (sort value, id) of a page's last row.

    Keyset rather than offset for newest-first lists, because rows are
    appended at the front: an offset walks a shifting window and silently
    skips or repeats whatever arrived ahead of it. The id is carried as the
    tiebreaker within one sort value.
    """
    raw = f"k1:{sort_value}\x1f{last_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_keyset_cursor(cursor: str | None) -> tuple[str, str] | None:
    """Decode a keyset cursor, or None when there is no cursor to honour.

    A malformed cursor is a 400 rather than a silent restart from the top:
    silently re-serving page one would look like a successful page-two
    request and the caller would never learn their position was lost.
    """
    if not cursor:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode()).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise _bad_keyset_cursor() from None
    prefix, sep, rest = decoded.partition(":")
    sort_value, unit, last_id = rest.partition("\x1f")
    if prefix != "k1" or not sep or not unit or not last_id:
        raise _bad_keyset_cursor()
    return sort_value, last_id


def _bad_keyset_cursor() -> HTTPException:
    return api_error(
        status_code=400,
        code="invalid_cursor",
        message="The cursor could not be decoded.",
        suggestion="Omit the cursor to start from the beginning.",
    )


# --------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------


def idempotency_key(
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> str:
    """Require and validate the Idempotency-Key header on a write."""
    if not idempotency_key or not idempotency_key.strip():
        raise api_error(
            status_code=400,
            code="idempotency_key_required",
            message="This write requires an Idempotency-Key header.",
            suggestion=(
                "Generate a UUID per logical operation and reuse it when "
                "retrying the same operation."
            ),
        )
    key = idempotency_key.strip()
    if len(key) > 128:
        raise api_error(
            status_code=400,
            code="idempotency_key_too_long",
            message="Idempotency-Key must be at most 128 characters.",
            suggestion="Use a UUID.",
        )
    return key


def _now() -> datetime:
    return datetime.now(UTC)


def _redact_queries(value: Any) -> Any:
    """Strip query strings from URL-shaped strings in a replayed payload.

    The idempotency store keeps whole response bodies for a week, and source
    payloads carry `url`/`canonical_url` verbatim - feed URLs routinely
    embed access tokens in their query (AGENTS.md privacy rule, same reason
    the audit trail redacts headers and adapters.http redacts logged URLs).
    A replay is still an exposure surface: it goes to whoever holds the key,
    which after a leak is exactly the wrong party. Structured recursion over
    dicts and lists; strings that parse as URLs with a query get the query
    dropped, everything else passes through untouched.
    """
    if isinstance(value, dict):
        return {key_: _redact_queries(item) for key_, item in value.items()}
    if isinstance(value, list):
        return [_redact_queries(item) for item in value]
    if isinstance(value, str) and value.count("?") == 1:
        base, _, query = value.partition("?")
        # Only strip when the left side plausibly is a URL (scheme://...):
        # a schedule policy string or a title containing a lone "?" is data,
        # not an address.
        if "://" in base and query:
            return base
    return value


def replay_or_none(
    session_factory: sessionmaker[Any],
    *,
    actor: str,
    endpoint: str,
    key: str,
) -> JSONResponse | None:
    """Return a stored response for this write, if one exists.

    The replayed body is query-redacted: see `_redact_queries`. The original
    execution's response carried the full URLs; the replay deliberately does
    not, because a client that wants the URLs again has the resource itself
    (GET /v1/sources/{id}) under its own authenticated request.
    """
    now = timestamp(_now())
    with session_factory() as session:
        stored = find_idempotent(
            session, actor=actor, endpoint=endpoint, key=key, now=now
        )
        if stored is None:
            return None
        payload: dict[str, Any] = _redact_queries(json.loads(stored.response_body))
        response = JSONResponse(status_code=stored.status_code, content=payload)
    # Replaying must be visible to the caller: without this a retry looks
    # identical to a fresh execution, which is correct but unhelpful when
    # debugging why nothing happened.
    response.headers["Idempotent-Replay"] = "true"
    return response


def remember(
    session_factory: sessionmaker[Any],
    *,
    actor: str,
    endpoint: str,
    key: str,
    status_code: int,
    body: dict[str, Any],
) -> None:
    created = _now()
    with session_factory() as session:
        store_idempotent(
            session,
            actor=actor,
            endpoint=endpoint,
            key=key,
            status_code=status_code,
            response_body=json.dumps(body, sort_keys=True),
            created_at=timestamp(created),
            expires_at=timestamp(created + IDEMPOTENCY_WINDOW),
        )
        session.commit()
