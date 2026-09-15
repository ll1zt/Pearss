"""Core API authentication.

Three tokens, deliberately separate from the Reader API credential:

    PEARSS_API_TOKEN        read and write and ingest
    PEARSS_API_READ_TOKEN   read only; optional
    PEARSS_INGEST_TOKEN     push content into an agent source only; optional

Splitting them is what AGENTS.md means by "读写 scope 分离": a client that only
needs to read — a dashboard, an agent doing diagnosis — can be given a token
that cannot mutate anything. The Reader API keeps its own credential because
its clients speak Google Reader's ClientLogin and cannot present a bearer
token; the two surfaces are authenticated independently and neither accepts
the other's secret.

The ingest token is a third split, on the same principle one level down: the
agent that pushes content (ADR 0022) is an automated writer with a narrow job,
so what its credential can do is narrowed to match. Leaking it costs the
attacker the ability to add items to an agent source -- not the ability to
delete sources, rewrite schedules, or read anyone's read state.

Comparisons are constant-time. A timing difference on a token check is a real,
if slow, side channel, and `hmac.compare_digest` costs nothing.
"""

from __future__ import annotations

import hmac
import logging
import os
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass
from typing import Any, Protocol

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

LOGGER = logging.getLogger(__name__)

READ_SCOPE = "read"
WRITE_SCOPE = "write"
# A scope of its own rather than a use of WRITE_SCOPE, so that the ingest
# credential opens exactly one thing: a caller holding only INGEST_SCOPE
# cannot touch source CRUD, deletion or plugin toggles.
#
# Note which direction the separation runs. It restricts the *ingest* token,
# not the full one -- PEARSS_API_TOKEN is granted INGEST_SCOPE too (see
# expected_tokens), because it can already delete sources and rewrite
# schedules, so withholding content-push from it would cost the "full" token
# its name without buying any safety. ADR 0022 asks for the ingest credential
# to be isolated; it does not ask for write to be unable to ingest.
INGEST_SCOPE = "ingest"

# auto_error=False so a missing header becomes an unauthenticated request we
# decide about, rather than an immediate 403 from the security scheme itself.
#
# scheme_name is set so the OpenAPI document carries a stable, meaningful
# security scheme id. The spec is the source of truth the MCP surface is
# generated from, and a class-derived name like "HTTPBearer" would leak an
# implementation detail into a published contract.
_bearer = HTTPBearer(auto_error=False, scheme_name="bearerAuth")


@dataclass(frozen=True, slots=True, kw_only=True)
class Principal:
    """An authenticated caller, with the scopes it was granted."""

    name: str
    scopes: frozenset[str]

    def may(self, scope: str) -> bool:
        return scope in self.scopes


# Key under which the resolved caller is parked on the ASGI scope, so the audit
# middleware can name the actor after the response.
#
# A scope entry rather than a contextvar, deliberately: contextvars propagate
# *into* a downstream task and not back out, so a value set by the endpoint's
# dependency would be invisible to the middleware that wraps it. The scope dict
# is one mutable object shared by the whole chain, which is what makes it
# readable on the way out.
SCOPE_PRINCIPAL_KEY = "pearss_principal"


def publish_principal(request: Request, principal: Principal) -> None:
    """Record the resolved caller for the audit middleware."""
    request.scope[SCOPE_PRINCIPAL_KEY] = principal


def principal_from_scope(scope: MutableMapping[str, Any]) -> str | None:
    """The caller the endpoint resolved, or None if it resolved none."""
    principal = scope.get(SCOPE_PRINCIPAL_KEY)
    return principal.name if principal is not None else None


def _configured(name: str) -> str:
    return os.environ.get(name, "").strip()


def expected_tokens() -> dict[str, frozenset[str]]:
    """Map each configured token to the scopes it grants.

    Narrow tokens are listed first so a caller presenting one is matched
    before a broader token is considered. With distinct secrets the order
    cannot matter, but it keeps the intent legible: the table is meant to be
    read as "least privilege first".

    The full token carries `ingest` as well. ADR 0022's requirement is that
    the ingest credential is *isolated* -- leaking it must not grant source
    deletion, schedule edits or read-state access -- and that holds because
    PEARSS_INGEST_TOKEN grants nothing else. Whether the full token may also
    push content is a separate question, and the answer is yes: it can already
    delete sources and rewrite schedules, so withholding content-push from it
    would add no security while making the "full" token not full.
    """
    table: dict[str, frozenset[str]] = {}
    read_token = _configured("PEARSS_API_READ_TOKEN")
    if read_token:
        table[read_token] = frozenset({READ_SCOPE})
    ingest_token = _configured("PEARSS_INGEST_TOKEN")
    if ingest_token:
        table[ingest_token] = frozenset({INGEST_SCOPE})
    api_token = _configured("PEARSS_API_TOKEN")
    if api_token:
        table[api_token] = frozenset({READ_SCOPE, WRITE_SCOPE, INGEST_SCOPE})
    return table


def auth_configured() -> bool:
    """Whether any API token is configured.

    Drives the startup warning: an unprotected Core API is not a state anyone
    should end up in by omission.
    """
    return bool(expected_tokens())


def _matches(presented: str, expected: str) -> bool:
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


class _HasHeaders(Protocol):
    """The slice of a request that `authenticate` actually reads.

    `headers` is spelled as a property so both Starlette's Request (where it
    is one) and the ASGI scope's plain dict-shaped view satisfy it.
    """

    @property
    def headers(self) -> Mapping[str, str]: ...


def authenticate(request: _HasHeaders) -> Principal | None:
    """Resolve the caller from `Authorization: Bearer <token>`, or None.

    Takes any headers-bearing view — a full Starlette Request at the route,
    or the raw scope's headers in ASGI middleware (EarlyAuth authenticates
    before a Request object exists, since building one would touch the
    receive channel that belongs to the body the route will read).
    """
    header = request.headers.get("authorization", "")
    scheme, _, presented = header.partition(" ")
    if scheme.lower() != "bearer" or not presented.strip():
        return None
    token = presented.strip()
    for expected, scopes in expected_tokens().items():
        if _matches(token, expected):
            return Principal(name=_label_for(scopes), scopes=scopes)
    return None


def _label_for(scopes: frozenset[str]) -> str:
    """The actor name the audit log records for a token.

    Keyed on the exact scope set, so a new token shape must be given a label
    here rather than silently reporting as the nearest existing one.
    """
    if scopes == frozenset({READ_SCOPE}):
        return "api:read-only"
    if scopes == frozenset({INGEST_SCOPE}):
        return "api:ingest"
    return "api:full"


def _dependency_for(scope: str) -> Callable[[Request], Principal]:
    """Build a dependency that requires `scope`.

    A factory rather than two near-identical functions because FastAPI inspects
    the signature of whatever it is given, and the request object is needed to
    read the header at call time rather than at import time.
    """

    def dependency(
        request: Request,
        # Declared so FastAPI documents the bearer security scheme in the
        # OpenAPI spec. The value itself is unused: authenticate() reads the
        # header off the request, because the same token must also be checked
        # outside dependency resolution. API-first matters here — the spec is
        # the source of truth the MCP surface is generated from, so an
        # undocumented auth scheme would propagate into the tools.
        credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    ) -> Principal:
        del credentials
        principal = authenticate(request)
        if principal is None:
            LOGGER.info("core api unauthenticated attempt, scope=%s", scope)
            raise HTTPException(
                status_code=401,
                detail={
                    "error": {
                        "code": "unauthenticated",
                        "message": "A valid bearer token is required.",
                        "suggestion": (
                            "Send 'Authorization: Bearer <token>'. Tokens are "
                            "configured via PEARSS_API_TOKEN or "
                            "PEARSS_API_READ_TOKEN."
                        ),
                        "details": [],
                    }
                },
                headers={"WWW-Authenticate": "Bearer"},
            )
        if not principal.may(scope):
            LOGGER.info(
                "core api refused scope=%s to principal=%s", scope, principal.name
            )
            raise HTTPException(
                status_code=403,
                detail={
                    "error": {
                        "code": "insufficient_scope",
                        "message": f"This endpoint requires the '{scope}' scope.",
                        "suggestion": (
                            "Use a token that grants write access, "
                            "PEARSS_API_TOKEN rather than PEARSS_API_READ_TOKEN."
                        ),
                        "details": [],
                    }
                },
            )
        # Published for the audit middleware, which runs after the response and
        # would otherwise have to report every caller as anonymous.
        publish_principal(request, principal)
        return principal

    return dependency


# The concrete dependencies FastAPI sees. Declared as module-level names so
# route signatures stay readable: `principal: Principal = Depends(read_required)`.
read_required = _dependency_for(READ_SCOPE)
write_required = _dependency_for(WRITE_SCOPE)
ingest_required = _dependency_for(INGEST_SCOPE)
