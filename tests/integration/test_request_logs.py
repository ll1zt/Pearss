"""Scenario register (normative source: SCENARIOS.md).

AC-284..285: every request emits exactly one application access line carrying
the correlation id and the matched route template; reads stay at DEBUG unless
the response is an error, so NNW's constant polling is not an INFO stream.

The line is read from a real handler installed by ``configure_logging`` (not
``caplog``) because the correlation id reaches the rendered text through the
handler's filter -- that is the whole point of AC-284.
"""

from __future__ import annotations

import io
import logging
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.orm import Session, sessionmaker

from app import log_setup
from app.main import create_app

pytestmark = pytest.mark.integration

WRITE_TOKEN = "integration-write-token"
READ_TOKEN = "integration-read-token"


@pytest.fixture
def restore_logging() -> Iterator[None]:
    """Undo configure_logging's process-global effects after the test."""
    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    access = logging.getLogger("uvicorn.access")
    access_level = access.level
    yield
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
    for handler in handlers:
        if handler not in root.handlers:
            root.addHandler(handler)
    root.setLevel(level)
    access.setLevel(access_level)


@pytest.fixture
def app(
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> FastAPI:
    monkeypatch.setenv("PEARSS_API_TOKEN", WRITE_TOKEN)
    monkeypatch.setenv("PEARSS_API_READ_TOKEN", READ_TOKEN)
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


def _access_lines(stream: io.StringIO, route: str) -> list[str]:
    return [line for line in stream.getvalue().splitlines() if f"{route} ->" in line]


@pytest.mark.asyncio
async def test_a_write_request_logs_correlation_route_status_and_duration(
    client: httpx.AsyncClient, restore_logging: None
) -> None:
    """AC-284: one line names the request, what it hit and how long it took."""
    stream = io.StringIO()
    log_setup.configure_logging(level="DEBUG", stream=stream)

    response = await client.post(
        "/v1/sources",
        json={
            "url": "https://example.com/feed",
            "schedule_policy": {"type": "fixed", "interval": "30m"},
        },
        headers={"X-Request-ID": "req-9", "Idempotency-Key": "req-logs-1"},
    )

    assert response.status_code in (200, 201), response.text
    lines = _access_lines(stream, "/v1/sources")
    assert len(lines) == 1, stream.getvalue()
    line = lines[0]
    assert "req-9" in line
    assert f"-> {response.status_code}" in line
    assert "ms" in line
    assert "actor=" in line
    # A write is worth the default level, unlike a read.
    assert "INFO" in line


@pytest.mark.asyncio
async def test_a_read_request_is_silent_at_info_and_visible_at_debug(
    client: httpx.AsyncClient, restore_logging: None
) -> None:
    """AC-285: a successful read does not flood INFO."""
    info_stream = io.StringIO()
    log_setup.configure_logging(level="INFO", stream=info_stream)
    response = await client.get("/v1/sources")
    assert response.status_code == 200, response.text
    assert _access_lines(info_stream, "/v1/sources") == [], info_stream.getvalue()

    debug_stream = io.StringIO()
    log_setup.configure_logging(level="DEBUG", stream=debug_stream)
    response = await client.get("/v1/sources")
    assert response.status_code == 200
    assert len(_access_lines(debug_stream, "/v1/sources")) == 1


@pytest.mark.asyncio
async def test_an_error_response_is_logged_at_info(
    app: FastAPI, client: httpx.AsyncClient, restore_logging: None
) -> None:
    """AC-285: a 5xx on a read is an incident, so it is not DEBUG-only."""

    class _DeadWorker:
        def liveness(self) -> tuple[bool, str]:
            return False, "scheduler"

    app.state.scheduler = _DeadWorker()

    stream = io.StringIO()
    log_setup.configure_logging(level="INFO", stream=stream)
    response = await client.get("/healthz")

    assert response.status_code == 503
    lines = _access_lines(stream, "/healthz")
    assert len(lines) == 1, stream.getvalue()
    assert "-> 503" in lines[0]
    assert "INFO" in lines[0]


def test_configure_logging_silences_the_uvicorn_access_log(
    restore_logging: None,
) -> None:
    """The application line replaces uvicorn's, so there is no second line."""
    log_setup.configure_logging(level="INFO")
    assert not logging.getLogger("uvicorn.access").isEnabledFor(logging.INFO), (
        "uvicorn.access still emits INFO, so every request would be logged twice"
    )
