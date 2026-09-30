"""Reader API authentication (issue #86).

The credential model, the deliberate asymmetry between the GET header token and
the POST form token, and the startup credential sync. The routes import the two
dependencies from here, so the rules about where a token may come from live in
one module rather than beside the endpoints that happen to use them.

Authentication asymmetry is load-bearing -- get this wrong and no client can
authenticate:

    GET  -> Authorization: GoogleLogin auth=<token>
    POST -> form field T=<token>

Clients do not mix them: a GET ignores a query token and a POST ignores the
header. Documented in ADR 0005.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

from fastapi import Form, Header, HTTPException, Request
from sqlalchemy.orm import Session, sessionmaker

from app.middleware import CREDENTIALLESS_AUTH_FAILURE
from core.reader_state import derive_auth_token, hash_password, verify_password

LOGGER = logging.getLogger(__name__)

DEFAULT_USERNAME = "reader"

_UNAUTHORIZED_HEADERS = {"X-Reader-Google-Bad-Token": "true"}


def _unauthorized() -> HTTPException:
    """The exact shape clients expect on an auth failure under /reader/api/0."""
    return HTTPException(
        status_code=401, detail="Unauthorized", headers=_UNAUTHORIZED_HEADERS
    )


class ReaderAuth:
    """Resolves and validates Reader API tokens."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def token_for(self, session: Session, username: str) -> str | None:
        from adapters.repository import reader_credential

        stored = reader_credential(session, username)
        if stored is None:
            return None
        return derive_auth_token(username=username, password_hash=stored)

    def valid(self, session: Session, token: str | None) -> bool:
        """Constant-time comparison against every configured credential."""
        from adapters.repository import reader_credential

        if not token:
            return False
        from secrets import compare_digest

        matched = False
        for username in _configured_usernames(session):
            stored = reader_credential(session, username)
            if stored is None:
                continue
            expected = derive_auth_token(username=username, password_hash=stored)
            # No early return: comparing against every credential keeps the
            # timing from revealing which username was valid.
            if compare_digest(token, expected):
                matched = True
        return matched


def _configured_usernames(session: Session) -> list[str]:
    """Usernames with a stored credential, plus the environment default."""
    from adapters.db_models import ReaderCredential

    rows = session.execute(
        ReaderCredential.__table__.select().with_only_columns(
            ReaderCredential.__table__.c.username
        )
    ).scalars()
    names = {str(row) for row in rows}
    names.add(os.environ.get("PEARSS_READER_USERNAME", DEFAULT_USERNAME))
    return sorted(names)


# --------------------------------------------------------------------------
# Dependencies
# --------------------------------------------------------------------------


def _auth_from_header(
    request: Request, authorization: str | None = Header(default=None)
) -> str:
    """Authenticate a GET: token comes from the Authorization header only."""
    auth: ReaderAuth = request.app.state.reader_auth
    token = _parse_google_login(authorization)
    with auth._session_factory() as session:
        if not auth.valid(session, token):
            raise _unauthorized()
    return token or ""


def _parse_google_login(header: str | None) -> str | None:
    """Extract the token from `GoogleLogin auth=<token>`."""
    if not header:
        return None
    scheme, _, remainder = header.partition(" ")
    if scheme != "GoogleLogin":
        return None
    field, _, value = remainder.partition("=")
    if field != "auth":
        return None
    return value.strip() or None


def _auth_from_form(
    request: Request, token: str | None = Form(default=None, alias="T")
) -> str:
    """Authenticate a write POST: token comes from the form field T only."""
    if not token or not token.strip():
        # Issue #18: nothing was offered. The auth layer is the only place
        # that can tell this from a wrong token (the wire shape is
        # identical), so it marks the scope; the audit middleware uses the
        # mark to keep probes out of the fact-source table.
        request.scope[CREDENTIALLESS_AUTH_FAILURE] = True
    auth: ReaderAuth = request.app.state.reader_auth
    with auth._session_factory() as session:
        if not auth.valid(session, token):
            raise _unauthorized()
    return token or ""


async def authenticate_any(request: Request) -> str:
    """Accept the token from the Authorization header or the form body.

    Used by the read endpoints. NetNewsWire sends them as POST with the token
    in the Authorization header and every parameter in the query string, not the
    form-body `T` the Google Reader convention describes. Serving those as
    GET-only produced a wall of 405s and no articles, so the asymmetry is
    enforced where clients depend on it (writes) and relaxed where the real
    client disagrees with the documentation (reads).

    The query string is deliberately not consulted. A token in a URL is
    captured by access logs, proxies and browser history, so accepting it there
    would trade a credential for a convenience no client in fact needs.
    """
    auth: ReaderAuth = request.app.state.reader_auth
    token = _parse_google_login(request.headers.get("authorization"))
    if not token and request.method in {"POST", "PUT", "PATCH"}:
        # Only parse a body when one was actually sent, and never consume it on
        # a GET, where there is nothing to read.
        form = await request.form()
        supplied = form.get("T")
        if isinstance(supplied, str) and supplied.strip():
            token = supplied.strip()
    if not token:
        # Issue #18: neither the header nor the form carried a token -- a
        # probe, not a guess. Marked for the audit middleware, which cannot
        # distinguish this from a wrong token by shape.
        request.scope[CREDENTIALLESS_AUTH_FAILURE] = True
    with auth._session_factory() as session:
        if not auth.valid(session, token):
            raise _unauthorized()
    return token or ""


# --------------------------------------------------------------------------
# Credential sync
# --------------------------------------------------------------------------


def configure_reader_credentials(
    session_factory: sessionmaker[Session], *, at: datetime | None = None
) -> bool:
    """Sync the environment credential into the database.

    Returns True if a credential was stored. The plaintext password is read
    from the environment and never persisted.
    """
    from sqlalchemy import inspect
    from sqlalchemy.exc import OperationalError, ProgrammingError

    from adapters.repository import upsert_reader_credential

    username = os.environ.get("PEARSS_READER_USERNAME", DEFAULT_USERNAME)
    password = os.environ.get("PEARSS_READER_PASSWORD", "")
    if not password:
        LOGGER.warning("PEARSS_READER_PASSWORD unset; Reader API login disabled")
        return False

    try:
        from adapters.db_models import ReaderCredential

        with session_factory() as session:
            # Starting up must not depend on the schema being current: a fresh
            # clone has credentials set but an unmigrated database, and failing
            # the boot there is unrecoverable without manual intervention.
            bind = session.get_bind()
            if "reader_credential" not in inspect(bind).get_table_names():
                LOGGER.warning(
                    "reader_credential table missing; run 'just migrate' "
                    "before using the Reader API"
                )
                return False
            # Rehash only when the password actually changed. The auth token
            # is derived from the stored hash, and scrypt salts are random,
            # so an unconditional rehash would mint a fresh token on every
            # login - each ClientLogin invalidating the token it had just
            # handed the same client's concurrent requests (observed as
            # alternating 200/401 in NNW's refresh). verify_password keeps
            # the check honest: a changed password rehashes, an unchanged
            # one keeps its hash and every outstanding token stays valid.
            row = session.get(ReaderCredential, username)
            if row is None or not verify_password(password, row.password_hash):
                upsert_reader_credential(
                    session,
                    username=username,
                    password_hash=hash_password(password),
                    at=at or datetime.now(UTC),
                )
                session.commit()
    except (OperationalError, ProgrammingError) as exc:
        LOGGER.warning("could not sync reader credential at startup: %s", exc)
        return False
    return True
