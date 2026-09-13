"""Fixtures for the end-to-end suite: the whole loop, in one process.

The E2E scenario (docs/guides/testing.md) was originally written against
Compose, which is why it stayed unwired. The loop does not actually need
containers: the app is served on a real uvicorn thread (not ASGITransport, so
lifespans, middleware and the MCP session manager all run), its collector
fetches go out over real TCP to a local fixture origin, and the agent surface
is driven with a real MCP client. The only concession to hermeticity is that
the origin is 127.0.0.1, which the SSRF guard (ADR 0016) would otherwise
refuse -- `PEARSS_ALLOW_PRIVATE_FETCH=1` is the documented escape hatch for
exactly this.

This conftest is self-contained on purpose: e2e must be able to run alone,
and sharing helpers with the integration suites would couple their fixtures.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from mcp import types
from mcp.client._transport import Transport
from mcp.client.client import Client
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy.orm import Session, sessionmaker
from uvicorn.config import Config as UvicornConfig
from uvicorn.server import Server

from app.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]
VALID_FEED = REPO_ROOT / "src/plugins/collectors/rss/fixtures/valid.xml"

API_TOKEN = "e2e-write-token"
MCP_EDGE_TOKEN = "e2e-mcp-token"
READER_USERNAME = "e2e-reader"
READER_PASSWORD = "e2e-reader-pw"


# --------------------------------------------------------------------------
# The controlled origin
# --------------------------------------------------------------------------


class _FixtureOrigin:
    """A real HTTP server serving one RSS document on the loopback."""

    def __init__(self, body: bytes) -> None:
        self._body = body
        self.requests = 0
        self._lock = threading.Lock()

        origin = self

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                with origin._lock:
                    origin.requests += 1
                self.send_response(200)
                self.send_header("content-type", "application/rss+xml")
                self.send_header("content-length", str(len(origin._body)))
                self.end_headers()
                self.wfile.write(origin._body)

            def log_message(self, *_args: object) -> None:
                pass  # silence the default stderr logging

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = int(self._httpd.server_address[1])

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/feed.xml"

    def __enter__(self) -> _FixtureOrigin:
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture
def origin_url() -> Iterator[str]:
    with _FixtureOrigin(VALID_FEED.read_bytes()) as origin:
        yield origin.url


# --------------------------------------------------------------------------
# The app under test
# --------------------------------------------------------------------------


def _migrated_database(tmp_path: Path) -> str:
    url = str(sa.URL.create("sqlite+pysqlite", database=str(tmp_path / "e2e.db")))
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    return url


@pytest.fixture
def session_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[sessionmaker[Session]]:
    url = _migrated_database(tmp_path)
    monkeypatch.setenv("PEARSS_DATABASE_URL", url)
    monkeypatch.setenv("PEARSS_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("PEARSS_API_TOKEN", API_TOKEN)
    monkeypatch.setenv("PEARSS_MCP_TOKEN", MCP_EDGE_TOKEN)
    monkeypatch.setenv("PEARSS_READER_USERNAME", READER_USERNAME)
    monkeypatch.setenv("PEARSS_READER_PASSWORD", READER_PASSWORD)
    monkeypatch.setenv("PEARSS_SCHEDULER_ENABLED", "0")
    # The origin is the loopback; the guard's documented escape hatch is what
    # makes a hermetic end-to-end run possible at all.
    monkeypatch.setenv("PEARSS_ALLOW_PRIVATE_FETCH", "1")

    engine = sa.create_engine(url)
    try:
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        engine.dispose()


@pytest.fixture
def app(session_factory: sessionmaker[Session]) -> FastAPI:
    return create_app(session_factory)


@pytest.fixture(autouse=True)
def _reap_sandboxed_children() -> Iterator[None]:
    yield
    from app import collector_registry

    collector_registry.for_each_sandboxed_collector(lambda name, child: child.stop())


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class _ServerInThread:
    def __init__(self, app: Any, port: int) -> None:
        self._server = Server(
            UvicornConfig(app, host="127.0.0.1", port=port, log_level="critical")
        )
        self._port = port

    def __enter__(self) -> _ServerInThread:
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        for _ in range(120):
            with socket.socket() as probe:
                probe.settimeout(0.2)
                if probe.connect_ex(("127.0.0.1", self._port)) == 0:
                    break
            time.sleep(0.05)
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=15)


@pytest_asyncio.fixture
async def served(app: FastAPI) -> AsyncIterator[str]:
    """The app on a real port; REST reads go here."""
    port = _free_port()
    with _ServerInThread(app, port):
        yield f"http://127.0.0.1:{port}"


@pytest_asyncio.fixture
async def mcp_url(app: FastAPI) -> AsyncIterator[str]:
    port = _free_port()
    with _ServerInThread(app, port):
        yield f"http://127.0.0.1:{port}/mcp"


@asynccontextmanager
async def mcp_session(
    url: str, *, elicitation_callback: Any = None
) -> AsyncIterator[Client]:
    """A modern-protocol client; see tests/integration/test_mcp_server.py for
    the reasoning (no handshake, MRTR retry loop, transport carries auth)."""
    kwargs = (
        {"elicitation_callback": elicitation_callback}
        if elicitation_callback is not None
        else {}
    )
    http_client = cast(
        "Any",
        httpx.AsyncClient(
            headers={"authorization": f"Bearer {MCP_EDGE_TOKEN}"},
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
        ),
    )
    transport = streamable_http_client(url, http_client=http_client)
    client = Client(cast("Transport", transport), mode="2026-07-28", **kwargs)
    try:
        async with client:
            yield client
    finally:
        await http_client.aclose()


@pytest.fixture
def open_mcp_session() -> Callable[..., Any]:
    return mcp_session


async def _auto_accept(_ctx: Any, params: Any) -> Any:
    """Confirm by echoing the server's prefilled defaults: 'press confirm'."""
    properties = (params.requested_schema or {}).get("properties", {})
    content = {
        name: spec.get("default", False if spec.get("type") == "boolean" else "")
        for name, spec in properties.items()
    }
    return types.ElicitResult(action="accept", content=content)
