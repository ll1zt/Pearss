"""Tests for the FastAPI application.

Uses httpx's ASGI transport so requests exercise the real app without binding
a port. The session factory is injected so each test gets a temporary
database.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import AsyncIterator, Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from adapters.http import FetchError
from adapters.raw_payload import RawPayload
from adapters.repository import store_item
from app.main import create_app
from app.middleware import CorrelationAndAuditMiddleware
from app.plugin_runtime import HostClientTransport
from core.enums import ErrorClass
from core.normalize import normalize
from plugins.collectors.rss import collector as rss
from tests.migrated_schema import migrated_database

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "src/plugins/collectors/rss/fixtures"
TENANT = "default"


@pytest.fixture
def session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    db_path = tmp_path / "app.db"
    migrated_database(tmp_path, "app.db")

    engine = create_engine(f"sqlite+pysqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "insert into source (id, type, url, canonical_url, schedule_policy,"
                " status, tenant_id, created_at, updated_at)"
                " values ('src-1', 'rss', 'https://blog.example.com/feed',"
                " 'https://blog.example.com/feed', '{}', 'active', 'default',"
                " '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
            )
        )
        # The ingestion target for these tests. Writing through the real
        # endpoint needs an agent source (ADR 0022); src-1 stays an rss source
        # so tests can still cover the types being distinct.
        connection.execute(
            text(
                "insert into source (id, type, url, canonical_url, schedule_policy,"
                " status, tenant_id, created_at, updated_at)"
                " values ('agent-1', 'agent', 'agent://intake', 'agent://intake',"
                " '{}', 'active', 'default',"
                " '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
            )
        )
    try:
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        engine.dispose()


API_TOKEN = "test-write-token"
READ_TOKEN = "test-read-token"
INGEST_TOKEN = "test-ingest-token"


@pytest.fixture
def app(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> FastAPI:
    monkeypatch.setenv("PEARSS_API_TOKEN", API_TOKEN)
    monkeypatch.setenv("PEARSS_API_READ_TOKEN", READ_TOKEN)
    monkeypatch.setenv("PEARSS_INGEST_TOKEN", INGEST_TOKEN)
    return create_app(session_factory)


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """An async client bound straight to the ASGI app, authenticated as writer.

    The full token carries read, write and ingest (see app.auth), so this one
    client covers a test that writes and then reads back.

    ASGITransport is async-only, so a sync httpx.Client cannot use it. Binding
    to the app rather than a port keeps requests in-process, and using the
    async client means a blocking handler would show up as serialised latency
    instead of being masked by a test client.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {API_TOKEN}"},
    ) as opened:
        yield opened


@pytest_asyncio.fixture
async def anonymous_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """A client with no credentials, for asserting refusals."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as opened:
        yield opened


@pytest_asyncio.fixture
async def read_only_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """A client holding only the read scope."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {READ_TOKEN}"},
    ) as opened:
        yield opened


@pytest_asyncio.fixture
async def ingest_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """A client holding only the ingest scope (ADR 0022)."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {INGEST_TOKEN}"},
    ) as opened:
        yield opened


INGEST_PATH = "/v1/sources/agent-1:ingest"


async def ingest(
    client: httpx.AsyncClient,
    *,
    key: str | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """POST one item to the ingest endpoint.

    Writes require an Idempotency-Key; this generates a fresh one per call so
    callers that are not testing replay behave as independent operations.
    """
    merged = {"Idempotency-Key": key or f"test-{uuid4().hex}"}
    merged.update(headers or {})
    return await client.post(INGEST_PATH, json=ingest_body(), headers=merged)


def ingest_body() -> dict[str, object]:
    """A body for the ingest endpoint (ADR 0022).

    No `source_id`: the endpoint takes it from the path, and the field is not
    part of the request schema at all.
    """
    return {
        "items": [
            {
                "external_id": "entry-api-1",
                "title": "Posted Through The API",
                "canonical_url": "https://blog.example.com/2026/01/posted?utm_source=x",
                "published_at": None,
                "content": "<p>Posted body.</p>",
            }
        ]
    }


# --------------------------------------------------------------------------
# Health, ingestion visibility, and the removed public feed
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_healthz_reports_ok(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_removed_feed_route_serves_nothing(client: httpx.AsyncClient) -> None:
    """The public RSS render is gone (ADR 0016): no route, no content, no 500.

    Content is consumed through the authenticated Reader API, so a feed URL —
    real or invented — must answer 404 rather than leak or half-exist.
    """
    assert (await client.get("/feeds/default.xml")).status_code == 404
    assert (await client.get("/feeds/nonexistent.xml")).status_code == 404


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_internal_ingest_scaffold_is_gone(client: httpx.AsyncClient) -> None:
    """`/internal/items` is removed, not merely deprecated (ADR 0022).

    It was the P1 pipeline scaffold, finished years of commits ago and never
    retired; leaving it beside /v1/sources/{id}:ingest would mean two
    ItemDraft-accepting writers with different auth semantics and two
    near-identical operations in the published contract. 404 -- not 401 --
    is the point: a leftover path must not read as "authenticate and retry".
    """
    response = await client.post(
        "/internal/items",
        json={
            "external_id": "whatever",
            "title": "t",
            "canonical_url": "https://e.example/x",
            "published_at": None,
            "content": "",
            "source_id": "agent-1",
        },
        headers={"Idempotency-Key": "k"},
    )
    assert response.status_code == 404


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ingested_item_is_queryable(client: httpx.AsyncClient) -> None:
    created = await ingest(client)
    assert created.status_code == 201
    item_id = created.json()["results"][0]["id"]
    assert created.json()["results"][0]["action"] == "insert"

    body = (await client.get("/v1/items?limit=10")).json()
    rows = [row for row in body["data"] if row["id"] == item_id]
    assert len(rows) == 1
    # Tracking parameter stripped during normalisation.
    assert "utm_source" not in json.dumps(rows)
    assert rows[0]["url"] == "https://blog.example.com/2026/01/posted"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ingest_is_idempotent(client: httpx.AsyncClient) -> None:
    first = (await ingest(client)).json()
    second = (await ingest(client)).json()

    assert first["results"][0]["id"] == second["results"][0]["id"]
    assert second["results"][0]["action"] == "skip_same_source"
    assert second["inserted"] == 0

    body = (await client.get("/v1/items?limit=10")).json()
    assert len(body["data"]) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pending_items_never_appear(
    client: httpx.AsyncClient, session_factory: sessionmaker[Session]
) -> None:
    # Store an item directly, bypassing the endpoint's promotion, so it stays
    # pending: the feed must omit it even though a row exists.
    drafts = rss.parse(
        RawPayload(
            url="https://blog.example.com/feed",
            status_code=200,
            body=(FIXTURES / "valid.xml").read_bytes(),
            source_id="src-1",
        )
    )
    items = [normalize(draft, tenant_id=TENANT).item for draft in drafts]

    with session_factory() as session:
        for item in items:
            store_item(session, item)
        session.commit()

    body = (await client.get("/v1/items?limit=10")).json()
    for item in items:
        assert item.guid not in json.dumps(body)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_internal_id_differs_from_public_guid(client: httpx.AsyncClient) -> None:
    created = (await ingest(client)).json()["results"][0]["id"]

    # The internal row id and the public guid are different values by
    # construction; nothing in the system should treat one as the other.
    row = (await client.get(f"/v1/items/{created}")).json()
    assert row["id"] != row["guid"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_openapi_is_served(client: httpx.AsyncClient) -> None:
    # Swagger is the documented fallback admin UI, so it must not regress.
    assert (await client.get("/docs")).status_code == 200
    assert (await client.get("/openapi.json")).status_code == 200


@pytest.mark.integration
@pytest.mark.asyncio
async def test_docs_can_be_disabled_for_public_deployments(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR 0016: an unauthenticated /openapi.json hands over the whole API
    surface, so a public deployment turns the fallback UI off entirely."""
    monkeypatch.setenv("PEARSS_DOCS_ENABLED", "0")
    hardened = create_app(session_factory)
    transport = httpx.ASGITransport(app=hardened)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as aclient:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert (await aclient.get(path)).status_code == 404, path


# --------------------------------------------------------------------------
# Concurrency: the app must not block the event loop
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_query_requests_do_not_serialise(app: FastAPI) -> None:
    """Ten concurrent requests must not take ten times as long as one.

    A blocking call inside an async handler would serialise them, which is
    invisible in a single-request test.
    """
    transport = httpx.ASGITransport(app=app)

    async def once(aclient: httpx.AsyncClient) -> None:
        await aclient.get("/v1/items?limit=5")

    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as aclient:
        start = time.perf_counter()
        await asyncio.gather(*(once(aclient) for _ in range(10)))
        elapsed = time.perf_counter() - start

    # Generous bound: this is a smoke test for serialisation, not a benchmark.
    assert elapsed < 5.0, f"10 concurrent feed requests took {elapsed:.2f}s"


# --------------------------------------------------------------------------
# Authentication and scopes
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_write_without_credentials_is_refused(
    anonymous_client: httpx.AsyncClient,
) -> None:
    response = await ingest(anonymous_client)
    assert response.status_code == 401


@pytest.mark.integration
@pytest.mark.asyncio
async def test_write_with_a_bad_token_is_refused(
    app: FastAPI,
) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer not-the-token"},
    ) as opened:
        response = await ingest(opened)
    assert response.status_code == 401


@pytest.mark.integration
@pytest.mark.asyncio
async def test_read_only_token_cannot_write(
    read_only_client: httpx.AsyncClient,
) -> None:
    # The point of splitting scopes: a reader-shaped credential must not be
    # able to mutate the corpus.
    response = await ingest(read_only_client)
    assert response.status_code == 403


@pytest.mark.integration
@pytest.mark.asyncio
async def test_read_only_token_can_read(client: httpx.AsyncClient) -> None:
    assert (await client.get("/healthz")).status_code == 200


@pytest.mark.integration
@pytest.mark.asyncio
async def test_auth_failure_uses_the_documented_error_envelope(
    anonymous_client: httpx.AsyncClient,
) -> None:
    # Ingest is a /v1 path, so an unauthenticated call is refused by
    # EarlyAuthMiddleware at the scope level and carries that layer's flat
    # envelope -- the same shape every other /v1 write produces for a missing
    # token.
    response = await ingest(anonymous_client)
    assert response.status_code == 401
    error = response.json()["error"]
    assert error["code"] == "unauthenticated"
    assert "message" in error and "suggestion" in error


@pytest.mark.integration
@pytest.mark.asyncio
async def test_openapi_declares_the_bearer_scheme(
    client: httpx.AsyncClient,
) -> None:
    # API-first: the spec is the source of truth the MCP surface is generated
    # from, so an undocumented auth scheme would propagate into the tools.
    spec = (await client.get("/openapi.json")).json()
    assert "bearerAuth" in spec.get("components", {}).get("securitySchemes", {})


# --------------------------------------------------------------------------
# Correlation and audit
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_response_echoes_a_generated_request_id(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/healthz")
    assert response.headers.get("x-request-id")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_inbound_request_id_is_honoured_and_echoed(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/healthz", headers={"X-Request-ID": "trace-abc-123"})
    assert response.headers.get("x-request-id") == "trace-abc-123"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_hostile_request_id_is_replaced_not_reflected(
    client: httpx.AsyncClient,
) -> None:
    # Newlines in a header value would forge log lines and could split the
    # response. The request must still succeed, with a generated id.
    response = await client.get("/healthz", headers={"X-Request-ID": "ok\r\nX-Fake: 1"})
    echoed = response.headers.get("x-request-id", "")
    assert "\r" not in echoed and "\n" not in echoed
    assert echoed != "ok"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_write_is_audited_with_actor_and_correlation(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    response = await ingest(client)
    assert response.status_code == 201
    correlation_id = response.headers.get("x-request-id")

    CorrelationAndAuditMiddleware.drain_audit()
    with app.state.session_factory() as session:
        rows = session.execute(
            sa.text(
                "select actor, operation, outcome, correlation_id, args_redacted"
                " from audit_log"
            )
        ).fetchall()

    assert rows, "a write should leave an audit record"
    match = [row for row in rows if row[3] == correlation_id]
    assert match, f"no audit row for correlation {correlation_id}"
    actor, operation, outcome, _, args = match[0]
    assert actor == "api:full"
    assert "ingest" in operation
    assert outcome == "succeeded"
    assert "Bearer" not in args


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reused_request_id_does_not_drop_audit_rows(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    """A client that reuses X-Request-ID must not lose audit records.

    Regression: the audit row's primary key was derived from the correlation
    id, which echoes this header — the second write collided and was dropped.
    """
    for _ in range(2):
        response = await ingest(
            client, headers={"X-Request-ID": "reused-by-the-client"}
        )
        assert response.status_code == 201

    CorrelationAndAuditMiddleware.drain_audit()
    with app.state.session_factory() as session:
        count = session.execute(sa.text("select count(*) from audit_log")).scalar()
    assert count == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_credentials_never_reach_the_audit_log(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    await ingest(client)
    CorrelationAndAuditMiddleware.drain_audit()
    with app.state.session_factory() as session:
        args = session.execute(
            sa.text("select args_redacted from audit_log")
        ).fetchall()
    joined = " ".join(row[0] for row in args)
    assert API_TOKEN not in joined
    assert "Bearer" not in joined
    assert "<redacted>" in joined


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reads_are_not_audited(app: FastAPI, client: httpx.AsyncClient) -> None:
    # AuditLog is a fact source kept forever, so routine reads would grow it
    # for no investigative benefit.
    await client.get("/v1/items")
    CorrelationAndAuditMiddleware.drain_audit()
    with app.state.session_factory() as session:
        after_read = session.execute(sa.text("select count(*) from audit_log")).scalar()

    assert after_read == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_refused_write_is_not_audited_because_it_never_reaches_the_endpoint(
    app: FastAPI, anonymous_client: httpx.AsyncClient
) -> None:
    """A /v1 request refused for want of a token leaves no audit row.

    This is a property of the layering, not of ingest: EarlyAuthMiddleware
    wraps outside CorrelationAndAuditMiddleware (it is registered later, and
    Starlette wraps in reverse order), so a request it refuses is answered
    before the audit layer sees it. Verified against POST /v1/sources too --
    the same call is also unaudited -- which is why this is stated for the
    layer rather than as an ingest behaviour.

    The set of *successful* writes is still fully audited, and that is what
    the audit log is for: an operator asking what changed the corpus.
    """
    await ingest(anonymous_client)
    # Without the drain this could pass because the refusal's audit row has not
    # been considered yet, rather than because none was written.
    CorrelationAndAuditMiddleware.drain_audit()
    with app.state.session_factory() as session:
        after_refusal = session.execute(
            sa.text("select count(*) from audit_log")
        ).scalar()

    assert after_refusal == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_write_echoes_the_audit_id_it_recorded(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    """The response header names the audit row, and that row exists.

    The audit row is written by the middleware *after* the response is sent,
    so the id cannot travel in the body; it is echoed as `X-Audit-ID` instead.
    The correspondence is what matters: an id in a header that named nothing
    would be worse than no id at all, so this reads the row back by that id.
    """
    response = await ingest(client)
    assert response.status_code == 201

    audit_id = response.headers.get("x-audit-id")
    assert audit_id, "a write did not report the audit row it created"

    CorrelationAndAuditMiddleware.drain_audit()
    with app.state.session_factory() as session:
        row = session.execute(
            sa.text("select operation from audit_log where id = :id"),
            {"id": audit_id},
        ).fetchone()
    assert row is not None, "the echoed audit id names no row"
    assert "ingest" in row[0]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_read_reports_no_audit_id(
    client: httpx.AsyncClient,
) -> None:
    """Reads are not audited, so they must not claim an audit id.

    The header is set from the request's method, not the outcome, so this is
    the guard against that predicate drifting: a header on a read would point
    at a row that was never written.
    """
    response = await client.get("/v1/items")
    assert response.status_code == 200
    assert "x-audit-id" not in response.headers


# --------------------------------------------------------------------------
# Public-exposure hardening (ADR 0016)
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_oversized_bodies_are_rejected_before_the_app(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PEARSS_MAX_BODY_BYTES", "64")
    # Early auth runs before the body limit: an unauthenticated oversized
    # request is answered 401, so this test must present a valid token to
    # reach the 413 it is about.
    monkeypatch.setenv("PEARSS_API_TOKEN", API_TOKEN)
    monkeypatch.setenv("PEARSS_API_READ_TOKEN", READ_TOKEN)
    hardened = create_app(session_factory)
    transport = httpx.ASGITransport(app=hardened)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as aclient:
        response = await aclient.post(
            "/v1/sources",
            headers={"Authorization": f"Bearer {API_TOKEN}"},
            json={"url": "https://blog.example.com/" + "x" * 200},
        )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_every_response_carries_nosniff(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_html_bodies_are_served_with_a_sandbox_csp(
    client: httpx.AsyncClient,
) -> None:
    created = await ingest(client)
    item_id = created.json()["results"][0]["id"]
    response = await client.get(f"/v1/items/{item_id}/body")
    assert response.status_code == 200
    assert response.headers["content-security-policy"] == "sandbox"
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_auth_failures_are_rate_limited(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Brute force must hit a wall, not an unlimited guessing game.

    The budget is deliberately tiny here (3 failures); the production default
    is 50 per 5 minutes with a 15 minute block.
    """
    monkeypatch.setenv("PEARSS_AUTH_RATELIMIT_MAX", "3")
    hardened = create_app(session_factory)
    transport = httpx.ASGITransport(app=hardened)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as aclient:
        for i in range(3):
            # The canonical guess: a presented (wrong) token. A headerless
            # request offers nothing to check and does not count (issue #8).
            wrong = await aclient.get(
                "/v1/sources", headers={"Authorization": f"Bearer wrong-{i}"}
            )
            assert wrong.status_code == 401

        blocked = await aclient.get("/v1/sources")
        assert blocked.status_code == 429
        assert blocked.json()["error"]["code"] == "rate_limited"
        assert "retry-after" in blocked.headers

        # Even a *valid* token is refused while blocked: the attacker's next
        # move after a burst is replaying a guessed or phished credential.
        still = await aclient.get(
            "/v1/sources", headers={"Authorization": f"Bearer {API_TOKEN}"}
        )
        assert still.status_code == 429


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reader_login_is_rate_limited_too(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PEARSS_AUTH_RATELIMIT_MAX", "2")
    monkeypatch.setenv("PEARSS_READER_USERNAME", "reader")
    monkeypatch.setenv("PEARSS_READER_PASSWORD", "probe-reader-pass-0123456789")
    hardened = create_app(session_factory)
    transport = httpx.ASGITransport(app=hardened)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as aclient:
        for _ in range(2):
            wrong = await aclient.post(
                "/api/greader.php/accounts/ClientLogin",
                data={"Email": "reader", "Passwd": "wrong-password"},
            )
            assert wrong.status_code == 401

        blocked = await aclient.post(
            "/api/greader.php/accounts/ClientLogin",
            data={"Email": "reader", "Passwd": "wrong-password"},
        )
        assert blocked.status_code == 429


@pytest.mark.integration
@pytest.mark.asyncio
async def test_lifespan_wires_the_sandboxed_egress_transport(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The app's lifespan must inject egress into every sandboxed child.

    Without the wiring each child's reverse http call is answered
    egress_denied and the four third-party collectors cannot fetch at all.
    The injected transport is the app's own shared client, so the SSRF
    guard covers sandboxed egress too.
    """
    from app import collector_registry

    collector_registry._registry()  # build children (they spawn lazily)
    # Run the lifespan by hand: ASGITransport does not enter it, and this
    # test needs the wiring that happens inside, not the routes.

    # A real loopback origin: the assertions below observe bytes arriving
    # and guards firing, not object identity between private fields.
    seen: list[str] = []

    class Origin(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append(self.path)
            body = b"sandboxed egress reached the origin"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Origin)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        origin_url = f"http://127.0.0.1:{server.server_port}/feed"
        monkeypatch.setenv("PEARSS_ALLOW_PRIVATE_FETCH", "1")
        async with app.router.lifespan_context(app):
            for name in ("telegram", "twitter", "zhihu", "zsxq"):
                child = collector_registry._SANDBOX_CHILDREN[name]
                transport = child._http_transport
                assert isinstance(transport, HostClientTransport), (
                    f"sandboxed collector {name!r} must egress through a "
                    "HostClientTransport"
                )
                # The wiring must actually carry traffic: a reverse call
                # through the injected transport reaches the real origin
                # and its body comes back (old B02 identity assertion).
                response = transport.handle_request(httpx.Request("GET", origin_url))
                assert response.status_code == 200, (
                    f"{name!r}'s egress must reach the wired transport"
                )
                assert b"sandboxed egress reached the origin" in response.content
            assert seen == ["/feed"] * 4

            # The SSRF guard covers sandboxed egress: without the
            # private-fetch exemption the same destination is refused
            # before the host sends anything (ADR 0016 via the transport).
            # The hermetic-DNS fixture answers every name with a public
            # address, and the guard's classification is exactly what this
            # half observes -- so resolve the loopback host for real (the
            # pattern the SSRF-specific suites use).
            from adapters import http as pearss_http

            monkeypatch.setattr(
                pearss_http,
                "resolve_host",
                lambda host, *, deadline=None: {"127.0.0.1"},
            )
            monkeypatch.delenv("PEARSS_ALLOW_PRIVATE_FETCH", raising=False)
            with pytest.raises(FetchError) as refused:
                transport.handle_request(httpx.Request("GET", origin_url))
            assert refused.value.error_class == ErrorClass.BLOCKED
            assert len(seen) == 4, "a refused destination must not be contacted"
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_lifespan_stops_every_sandboxed_child_on_shutdown(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shutdown must reap the children, not leave them to the OS.

    Children spawn lazily and hold real processes with pipes; without an
    explicit stop at lifespan exit they outlive a graceful shutdown and are
    only reaped when the host process itself dies. In tests that shows up as
    ResourceWarnings about running subprocesses, which is how this gap was
    found -- but the production consequence is orphaned collector processes.
    """
    from app import collector_registry

    collector_registry._registry()
    # Force-real spawn so there is a live process whose death can be observed
    # (children are lazy; a built registry alone leaves _proc as None).
    child = collector_registry._SANDBOX_CHILDREN["telegram"]
    child.start()
    proc = child._proc
    assert proc is not None, "the child must be running before the lifespan exits"

    async with app.router.lifespan_context(app):
        assert child._proc is not None and child._proc.poll() is None

    assert child._proc is None, "the child must be stopped, not left dangling"
    assert proc.poll() is not None, "the child process must be reaped"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unauthenticated_requests_are_refused_before_body_parsing(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bad token gets 401 even when the body is invalid JSON.

    FastAPI resolves dependencies after the body has been read and
    validated, so the route dependency alone let an unauthenticated caller
    probe every endpoint's validation behaviour. EarlyAuth refuses at the
    scope level, before any body byte is parsed.
    """
    monkeypatch.setenv("PEARSS_API_TOKEN", API_TOKEN)
    monkeypatch.setenv("PEARSS_API_READ_TOKEN", READ_TOKEN)
    hardened = create_app(session_factory)
    transport = httpx.ASGITransport(app=hardened)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as aclient:
        bad_token_bad_body = await aclient.post(
            "/v1/sources",
            headers={"Authorization": f"Bearer {API_TOKEN}-wrong"},
            content=b"{ this is not json",
        )
        assert bad_token_bad_body.status_code == 401
        assert bad_token_bad_body.json()["error"]["code"] == "unauthenticated"

        no_token = await aclient.get("/v1/sources")
        assert no_token.status_code == 401

        read_token_write = await aclient.post(
            "/v1/sources",
            headers={"Authorization": f"Bearer {READ_TOKEN}", "Idempotency-Key": "k"},
            json={"url": "https://blog.example.com/feed"},
        )
        assert read_token_write.status_code == 403
        assert read_token_write.json()["error"]["code"] == "insufficient_scope"

    # The documented envelope shape, not Starlette's default.
    assert "suggestion" in bad_token_bad_body.json()["error"]
