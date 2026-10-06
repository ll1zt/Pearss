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

Audit writes run on a dedicated worker thread, never on the event loop: the
audit table lives in the same SQLite database as everything else, so under
writer contention a synchronous audit commit would block the *shared* loop
for up to the full busy timeout, stalling unrelated requests and worker
scheduling behind one bookkeeping row. The session is created and closed
inside the thread that uses it.
"""

from __future__ import annotations

import logging
import secrets
import time
from collections.abc import MutableMapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi import Request
from sqlalchemy.orm import sessionmaker
from starlette.types import Receive, Scope, Send

from adapters import correlation
from adapters.audit import headers_from_scope, record, redact_headers
from adapters.log_safety import escape_control_characters
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
# a guessed token looks in the data, and that is worth keeping. But only when
# there was a guess: the auth layers mark requests that offered no credential
# at all (CREDENTIALLESS_AUTH_FAILURE below) -- a probe, not a guess (issue
# #18). The tunnel's only public surface (the Reader API) receives such probes
# in bulk, and auditing every one of them filled the fact-source table
# unboundedly from an unthrottled path. Only the auth layer itself can tell
# "no credential" from "a wrong credential": a form POST without T is
# wire-identical to one with a bad T as far as headers go.
#
# The rate limiter's exemption is *not* this same set, and it cannot be. The
# limiter sits outside the auth layer and sees only headers, so it counts every
# form POST -- an empty T included -- and exempts only requests that presented
# neither an Authorization header nor a form body (issue #349). The audit mark
# and the rate-limit exemption are two different predicates, each as precise as
# the layer that owns its evidence: the auth layer has the parsed form, the
# limiter does not.
AUTH_FAILURE_STATUSES = frozenset({401, 403})

# Scope key the auth layers set when they refused a request that presented no
# credential whatsoever. Shape-level detection (header/form sniffing) cannot
# do this: Reader write probes are form POSTs, so the content-type matches a
# real ClientLogin either way. Marking at the point of refusal keeps the
# definition next to the check that has the evidence.
CREDENTIALLESS_AUTH_FAILURE = "pearss.credentialless-auth-failure"

# The audit queue's bound. One worker commits rows at database speed; a flood
# of audited requests must not grow the queue without limit (each entry holds
# the whole request's header snapshot). When the bound is hit the row is
# dropped with an error-level journal entry -- overload semantics, not
# silent loss: an x-audit-id whose row was dropped still names the gap in
# the journal by correlation id.
AUDIT_QUEUE_BOUND = 1000

ScopeState = MutableMapping[str, Any]


def _should_audit(scope: Scope, status: int) -> bool:
    path = scope.get("path", "")
    if path in UNAUDITED_PATHS or path.startswith("/docs"):
        return False
    # Auth failures are decided by the auth layer's own evidence first,
    # whatever the method: a 401 means the operation never ran, so the only
    # question is whether a guess was attempted. A request that offered no
    # credential at all is a probe, not a refused write (issue #18).
    if status in AUTH_FAILURE_STATUSES:
        return not scope.get(CREDENTIALLESS_AUTH_FAILURE)
    method = scope.get("method", "")
    return method in WRITE_METHODS


def _actor(scope: ScopeState) -> str:
    """Name the caller.

    Falls back to `anonymous` rather than leaving the field empty: an
    unattributable row is worse than an honest one. A refused request has no
    principal by definition, so `anonymous` on a 401 is accurate rather than a
    gap in the record.
    """
    return principal_from_scope(scope) or "anonymous"


def _operation(scope: ScopeState) -> str:
    """Name what the request hit, safe to put in a log line and an audit row.

    An unmatched route has no template, so the fallback is the request's own
    path -- which the caller controls. ``scope["path"]`` is percent-decoded,
    so a ``%0A`` arrives as a real newline and would end the access line early
    (AC-296). Escaping it here fixes the log *and* the audit row, which is the
    other place this string lands.
    """
    route = scope.get("route")
    if route is not None and getattr(route, "path", None):
        return escape_control_characters(f"{scope.get('method', '?')} {route.path}")
    return escape_control_characters(
        f"{scope.get('method', '?')} {scope.get('path', '?')}"
    )


def _access_level(scope: ScopeState, status: int) -> int:
    """The level one request's access line is worth.

    Reads that succeeded are DEBUG, writes and errors are INFO (issue #294).
    The client polls the Reader API constantly; a line per poll at the default
    level is a journal flood that carries nothing an operator acts on, while a
    write or a response the caller did not succeed with is worth keeping.
    """
    if scope.get("method", "") in WRITE_METHODS:
        return logging.INFO
    return logging.INFO if status >= 400 else logging.DEBUG


class CorrelationAndAuditMiddleware:
    """Assign a correlation id, echo it, and audit meaningful operations."""

    # One thread is the bound: audit rows are bookkeeping, and a burst of
    # requests queues behind each other instead of multiplying database
    # contention. An overload therefore delays audit records (visible in
    # the journal on failure) rather than the responses callers wait for.
    _EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pearss-audit")

    def __init__(self, app: Any, *, session_factory: SessionFactory) -> None:
        self.app = app
        self.session_factory = session_factory

    def _submit(
        self, scope: ScopeState, status: int, correlation_id: str, audit_id: str
    ) -> None:
        """Hand the audit write to the worker thread and return immediately.

        Fire-and-forget is the deliberate contract: the caller's response is
        already on the wire, so waiting here would only couple its completion
        to database contention the request did not cause. Completion is
        observable through the audit table itself; failure through the
        error-level journal entry the worker emits.

        The queue is bounded (issue #18): under a flood of audited requests
        the single worker cannot keep up and an unbounded queue grows the
        process without limit -- each entry holds the request's header
        snapshot. At capacity the row is dropped and the gap is logged at
        error level with the correlation id, so the overload is observable
        rather than silent. Drops prefer the newest request (the flood's
        latest probe), never a reordering of already-queued rows.
        """
        if self._EXECUTOR._work_queue.qsize() >= AUDIT_QUEUE_BOUND:
            LOGGER.error(
                "audit queue full (%d); dropping the audit row for %s",
                AUDIT_QUEUE_BOUND,
                correlation_id,
            )
            return
        self._EXECUTOR.submit(self._record, scope, status, correlation_id, audit_id)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        started = time.perf_counter()
        inbound = correlation.sanitize_inbound(request.headers.get("x-request-id"))
        correlation_id = inbound or correlation.new_correlation_id()
        token = correlation.set_correlation_id(correlation_id)

        # Generated before the response starts rather than in `_record`, because
        # the id has to be echoed in a response header and the response is
        # already on the wire by then. Random per request, not derived from the
        # correlation id: that id may echo a client-supplied X-Request-ID, and a
        # client that reused one would collide on this primary key.
        audit_id = f"audit-{secrets.token_hex(8)}"

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
                # The audit id is promised exactly when the row will exist:
                # the status is known here (response.start), so the header
                # and the audit decision cannot disagree. A credential-free
                # probe gets no x-audit-id, because no row is coming (issue
                # #18) -- a promised id for a dropped row would send an
                # operator looking for evidence that was never written.
                if _should_audit(scope, status):
                    message["headers"] = _with_header(
                        message["headers"], b"x-audit-id", audit_id
                    )
                sent = True
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            # An unhandled exception is answered by Starlette's
            # ServerErrorMiddleware, which sits outside this one: `send_wrapper`
            # never sees the 500, so without this the request that crashed --
            # the one an operator most needs -- left no line at all (AC-304).
            # Only when nothing was sent: a failure *after* the response started
            # already has its line in the `finally` below.
            if not sent:
                self._log_access(scope, 500, started)
            raise
        finally:
            # Logged before the correlation reset: the id reaches the line
            # through the handler's filter reading the contextvar, so resetting
            # first would stamp the access log with a generated id instead of
            # the caller's (issue #294).
            if sent:
                self._log_access(scope, status, started)
            # Reset before the task ends so a reused context cannot leak one
            # request's id into the next.
            correlation.reset(token)

        if not sent:
            return
        if _should_audit(scope, status):
            self._submit(scope, status, correlation_id, audit_id)

    @classmethod
    def drain_audit(cls, timeout: float = 10.0) -> bool:
        """Wait until every submitted audit write has finished.

        Test support for the fire-and-forget contract: the middleware itself
        never waits (that is the point of A15), so a test that asserts on the
        audit table awaits this instead of sleeping a fixed interval. Returns
        False on timeout, so a test can fail on evidence rather than hang.

        A sentinel task, not an empty-queue check. The executor's queue holds
        only *pending* work -- the worker removes an item before it runs it -- so
        `qsize() == 0` is true while a write is still executing. Polling for
        that returned early, and because the executor is FIFO with one worker,
        waiting on a sentinel submitted now is exactly "everything submitted
        before this point has finished".

        That distinction cost a CI run: `test_backfill_chain_endpoint_queues_
        and_audits` failed with `assert 1 == 2` -- one audit row short, the
        second write still in flight -- while passing in isolation, where the
        write happens to win the race. Every `drain_audit(); assert count` in
        the suite carried the same latent flake.
        """
        try:
            cls._EXECUTOR.submit(lambda: None).result(timeout=timeout)
        except TimeoutError:
            return False
        return True

    def _log_access(self, scope: ScopeState, status: int, started: float) -> None:
        """One line per request: method, route template, status, actor, time.

        Route template rather than raw path: the path carries ids, so a line
        per item would be a line per id instead of one per endpoint. The
        correlation id is added by the handler's filter.

        At most one line: uvicorn's access log is silenced (see
        `configure_logging`) so this replaces it rather than doubling it.
        """
        duration_ms = (time.perf_counter() - started) * 1000.0
        LOGGER.log(
            _access_level(scope, status),
            "%s -> %d actor=%s in %.1fms",
            _operation(scope),
            status,
            _actor(scope),
            duration_ms,
        )

    def _record(
        self, scope: ScopeState, status: int, correlation_id: str, audit_id: str
    ) -> None:
        """Write one audit row, on the worker thread, in its own session.

        Runs outside the event loop, so a contended database wait costs this
        thread nothing but time -- the responses callers wait for are already
        gone. Failure is visible, never fatal: the row is lost, the journal
        says so.
        """
        try:
            with self.session_factory() as session:
                record(
                    session,
                    audit_id=audit_id,
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
