"""Fixtures for contract tests: an app served for real, nothing else.

Contract tests pin what an external client depends on -- the MCP tool surface
and the OpenAPI document -- so they need a real app speaking real HTTP, but no
database state beyond an empty migrated schema (create_app requires one). This
conftest is deliberately self-contained rather than sharing helpers with
tests/integration: contract tests must run without the integration suites and
must not inherit their fixtures.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy.orm import Session, sessionmaker
from uvicorn.config import Config as UvicornConfig
from uvicorn.server import Server

from app.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]

API_TOKEN = "contract-write-token"
MCP_EDGE_TOKEN = "contract-mcp-token"

API_HEADERS = {"Authorization": f"Bearer {API_TOKEN}"}


def _migrated_database(tmp_path: Path) -> str:
    """A real migrated schema: the app under test is the production one."""
    url = str(sa.URL.create("sqlite+pysqlite", database=str(tmp_path / "contract.db")))
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    return url


@pytest.fixture
def session_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[sessionmaker[Session]]:
    """A migrated, empty database -- and the env a real deployment reads."""
    url = _migrated_database(tmp_path)
    monkeypatch.setenv("PEARSS_DATABASE_URL", url)
    monkeypatch.setenv("PEARSS_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("PEARSS_API_TOKEN", API_TOKEN)
    monkeypatch.setenv("PEARSS_MCP_TOKEN", MCP_EDGE_TOKEN)
    monkeypatch.setenv("PEARSS_SCHEDULER_ENABLED", "0")

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
    """The registry spawns sandbox children lazily; stop them after the test."""
    yield
    from app import collector_registry

    collector_registry.for_each_sandboxed_collector(lambda name, child: child.stop())


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class _ServerInThread:
    """The app served on its own thread and event loop.

    Same shape as the integration helper: keeping uvicorn off the test loop
    means the server's anyio cancel scopes live and die on one thread.
    """

    def __init__(self, app: Any, port: int) -> None:
        self._server = Server(
            UvicornConfig(app, host="127.0.0.1", port=port, log_level="critical")
        )
        self._port = port

    def __enter__(self) -> _ServerInThread:
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        for _ in range(120):
            if self._listening():
                break
            time.sleep(0.05)
        return self

    def _listening(self) -> bool:
        with socket.socket() as probe:
            probe.settimeout(0.2)
            return probe.connect_ex(("127.0.0.1", self._port)) == 0

    def __exit__(self, *exc: object) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=15)


@pytest_asyncio.fixture
async def mcp_url(app: FastAPI) -> AsyncIterator[str]:
    """Serve the app and yield its MCP endpoint URL."""
    port = _free_port()
    with _ServerInThread(app, port):
        yield f"http://127.0.0.1:{port}/mcp"


@asynccontextmanager
async def mcp_session(
    url: str, *, elicitation_callback: Any = None
) -> AsyncIterator[ClientSession]:
    """A connected MCP client session, opened and closed in the caller's task.

    The transport enters an anyio cancel scope, so the same task must exit
    it -- a helper rather than a fixture for the same reason the integration
    suite gives (async-generator teardown runs in a different task).
    """
    # The SDK vendors its own renamed httpx (`httpx2`); the cast records that
    # the structural interface is what matters (see the integration suite).
    http_client = cast(
        "Any",
        httpx.AsyncClient(
            headers={"authorization": f"Bearer {MCP_EDGE_TOKEN}"},
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
        ),
    )
    kwargs = (
        {"elicitation_callback": elicitation_callback}
        if elicitation_callback is not None
        else {}
    )
    async with (
        http_client,
        streamable_http_client(url, http_client=http_client) as (read, write),
        ClientSession(read, write, **kwargs) as session,
    ):
        await session.initialize()
        yield session


@pytest.fixture
def open_mcp_session() -> Callable[..., Any]:
    """Expose `mcp_session` to tests as a fixture-provided helper."""
    return mcp_session
