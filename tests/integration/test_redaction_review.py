"""Authenticated writes retain attributable audit evidence without credentials."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy.orm import Session, sessionmaker

from adapters.db_models import AuditLog
from app.main import create_app
from app.middleware import CorrelationAndAuditMiddleware

WRITE_TOKEN = "pearss-live-token-9f3a2b7c"
FEED_URL = "https://planetpython.org/rss20.xml"


@pytest.fixture
def app(
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> FastAPI:
    monkeypatch.setenv("PEARSS_API_TOKEN", WRITE_TOKEN)
    monkeypatch.setenv("PEARSS_STORAGE_DIR", str(tmp_path / "storage"))
    return create_app(session_factory)


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {WRITE_TOKEN}"},
    ) as opened:
        yield opened


def _audit_for(
    session_factory: sessionmaker[Session], response: httpx.Response
) -> AuditLog:
    # The audit write is asynchronous (A15): wait for the worker queue to
    # drain before demanding the row. scalar_one still fails for absent
    # evidence, not merely leaked credentials.
    CorrelationAndAuditMiddleware.drain_audit()
    with session_factory() as session:
        return session.scalars(
            sa.select(AuditLog).where(AuditLog.id == response.headers["x-audit-id"])
        ).one()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bearer_token_never_reaches_audit_log(
    client: httpx.AsyncClient, session_factory: sessionmaker[Session]
) -> None:
    """User writes with a credential; the matching successful audit is redacted."""
    response = await client.post(
        "/v1/sources",
        headers={"Idempotency-Key": "audit-redaction-1", "X-Request-ID": "redaction-1"},
        json={
            "url": FEED_URL,
            "type": "rss",
            "schedule_policy": {"type": "fixed", "interval": "30m"},
        },
    )
    assert response.status_code == 201
    row = _audit_for(session_factory, response)
    assert (row.operation, row.outcome, row.correlation_id) == (
        "POST /v1/sources",
        "succeeded",
        "redaction-1",
    )
    assert '"authorization": "<redacted>"' in json.loads(row.args_redacted)["headers"]
    assert WRITE_TOKEN not in row.args_redacted


@pytest.mark.integration
@pytest.mark.asyncio
async def test_credential_header_is_marked_not_dropped(
    client: httpx.AsyncClient, session_factory: sessionmaker[Session]
) -> None:
    """User can distinguish a redacted credential from an absent credential."""
    response = await client.post(
        "/v1/sources",
        headers={"Idempotency-Key": "audit-redaction-2", "X-Request-ID": "redaction-2"},
        json={
            "url": FEED_URL,
            "type": "rss",
            "schedule_policy": {"type": "fixed", "interval": "30m"},
        },
    )
    assert response.status_code == 201
    row = _audit_for(session_factory, response)
    assert (row.operation, row.outcome, row.correlation_id) == (
        "POST /v1/sources",
        "succeeded",
        "redaction-2",
    )
    assert '"authorization": "<redacted>"' in json.loads(row.args_redacted)["headers"]
    assert WRITE_TOKEN not in row.args_redacted


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rejected_write_is_still_audited(
    client: httpx.AsyncClient, session_factory: sessionmaker[Session]
) -> None:
    """User submits invalid configuration with valid authentication.

    Then the validation rejection has a matching redacted audit. This is not
    an unauthenticated perimeter refusal, deliberately unaudited by ADR 0016.
    """
    response = await client.post(
        "/v1/sources",
        headers={"Idempotency-Key": "audit-redaction-3", "X-Request-ID": "redaction-3"},
        json={"url": FEED_URL, "type": "rss", "schedule_policy": {"type": "nope"}},
    )
    assert response.status_code == 422
    row = _audit_for(session_factory, response)
    assert (row.operation, row.outcome, row.correlation_id) == (
        "POST /v1/sources",
        "rejected",
        "redaction-3",
    )
    assert response.headers["x-request-id"] == "redaction-3"
    assert '"authorization": "<redacted>"' in json.loads(row.args_redacted)["headers"]
    assert WRITE_TOKEN not in row.args_redacted
