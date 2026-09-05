"""Tests for the MCP edge guard: fail-closed auth and Host pinning.

A real uvicorn instance on its own thread is required rather than
`httpx.ASGITransport`: the `/mcp` mount is created inside the application
lifespan, so a transport that skips the lifespan has no `/mcp` route at all and
every request 404s before reaching the guard. A 404 would satisfy "not 401"
without the middleware ever having run, which for an authentication test is the
one outcome worse than failing.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.orm import sessionmaker
from uvicorn import Config as UvicornConfig
from uvicorn.server import Server

from app.main import create_app
from app.mcp_guard import (
    MCP_ALLOWED_HOSTS_ENV,
    MCP_TOKEN_ENV,
    allowed_hosts,
    mcp_auth_configured,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MCP_PATH = "/mcp/mcp"
MCP_TOKEN = "synthetic-mcp-edge-token"
API_TOKEN = "synthetic-api-token"

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    },
}
MCP_HEADERS = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
}


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    url = str(sa.URL.create("sqlite+pysqlite", database=str(tmp_path / "guard.db")))
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")

    engine = sa.create_engine(url)
    monkeypatch.setenv("PEARSS_API_TOKEN", API_TOKEN)
    monkeypatch.setenv("PEARSS_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv(MCP_TOKEN_ENV, MCP_TOKEN)
    monkeypatch.delenv(MCP_ALLOWED_HOSTS_ENV, raising=False)
    return create_app(sessionmaker(engine, expire_on_commit=False))


@pytest.fixture
def base_url(app: Any) -> Iterator[str]:
    server = Server(UvicornConfig(app, host="127.0.0.1", port=0, log_level="critical"))

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if server.started and server.servers:
                socket = server.servers[0].sockets[0]
                yield f"http://127.0.0.1:{socket.getsockname()[1]}"
                break
            time.sleep(0.05)
        else:
            raise RuntimeError("uvicorn did not bind a socket")
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def post_mcp(base: str, token: str | None = None) -> httpx.Response:
    headers = dict(MCP_HEADERS)
    if token:
        headers["authorization"] = f"Bearer {token}"
    return httpx.post(f"{base}{MCP_PATH}", json=INITIALIZE, headers=headers)


# --------------------------------------------------------------------------
# Fail-closed authentication
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_mcp_requires_the_edge_token(base_url: str) -> None:
    """No credential must not reach a tool, or even enumerate them."""
    response = post_mcp(base_url)
    assert response.status_code == 401
    body = response.json()["error"]
    assert body["code"] == "unauthorized"
    # Same envelope as /v1, so a client learns one error shape, not two.
    assert "suggestion" in body


@pytest.mark.integration
def test_a_wrong_token_is_refused(base_url: str) -> None:
    assert post_mcp(base_url, token="not-the-token").status_code == 401


@pytest.mark.integration
def test_the_api_token_does_not_work_on_mcp(base_url: str) -> None:
    """Separate credentials: revoking a laptop's MCP client must not log out
    the Core API, and vice versa."""
    assert post_mcp(base_url, token=API_TOKEN).status_code == 401


@pytest.mark.integration
def test_the_right_token_reaches_the_mcp_server(base_url: str) -> None:
    response = post_mcp(base_url, token=MCP_TOKEN)
    assert response.status_code in (200, 201), response.text[:200]


@pytest.mark.integration
def test_a_non_bearer_scheme_is_refused(base_url: str) -> None:
    response = httpx.post(
        f"{base_url}{MCP_PATH}",
        json=INITIALIZE,
        headers={**MCP_HEADERS, "authorization": f"Basic {MCP_TOKEN}"},
    )
    assert response.status_code == 401


@pytest.mark.integration
def test_unconfigured_token_disables_the_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No token configured is not "no auth required" -- it is "off"."""
    monkeypatch.delenv(MCP_TOKEN_ENV, raising=False)
    assert not mcp_auth_configured()

    url = str(sa.URL.create("sqlite+pysqlite", database=str(tmp_path / "off.db")))
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    monkeypatch.setenv("PEARSS_API_TOKEN", API_TOKEN)
    monkeypatch.setenv("PEARSS_STORAGE_DIR", str(tmp_path / "storage"))
    app = create_app(sessionmaker(sa.create_engine(url), expire_on_commit=False))

    server = Server(UvicornConfig(app, host="127.0.0.1", port=0, log_level="critical"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if server.started and server.servers:
                sock = server.servers[0].sockets[0]
                base = f"http://127.0.0.1:{sock.getsockname()[1]}"
                # Even the right token is refused: there is no expected value
                # to match, so the endpoint is disabled rather than open.
                response = post_mcp(base, token=MCP_TOKEN)
                assert response.status_code == 503
                assert response.json()["error"]["code"] == "mcp_unconfigured"
                return
            time.sleep(0.05)
        raise RuntimeError("uvicorn did not bind a socket")
    finally:
        server.should_exit = True
        thread.join(timeout=10)


# --------------------------------------------------------------------------
# Host pinning configuration
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_loopback_is_always_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Widening for a remote host must not lock out the local client."""
    monkeypatch.setenv(MCP_ALLOWED_HOSTS_ENV, "tailscale.host:8000")
    hosts = allowed_hosts()
    assert "tailscale.host:8000" in hosts
    assert "127.0.0.1:*" in hosts and "localhost:*" in hosts


@pytest.mark.unit
def test_unset_hosts_keep_the_sdk_default_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(MCP_ALLOWED_HOSTS_ENV, raising=False)
    hosts = allowed_hosts()
    # Both forms: the SDK matches the Host header exactly or via `host:*`, and
    # a browser always sends the port.
    assert "127.0.0.1:*" in hosts and "localhost:*" in hosts


@pytest.mark.unit
def test_blank_entries_are_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MCP_ALLOWED_HOSTS_ENV, " a.com ,, b.com, ")
    hosts = allowed_hosts()
    assert "a.com" in hosts and "b.com" in hosts
    assert "" not in hosts


# --------------------------------------------------------------------------
# The guard is scoped to /mcp
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_core_api_still_works_with_its_own_token(base_url: str) -> None:
    response = httpx.get(
        f"{base_url}/v1/sources", headers={"authorization": f"Bearer {API_TOKEN}"}
    )
    assert response.status_code == 200


@pytest.mark.integration
def test_healthz_stays_open(base_url: str) -> None:
    """A monitoring probe should not need a credential."""
    assert httpx.get(f"{base_url}/healthz").status_code == 200


@pytest.mark.unit
def test_bearer_extraction_ignores_other_schemes() -> None:
    from app.mcp_guard import _bearer_present

    assert _bearer_present({"headers": [(b"authorization", b"Bearer abc")]}) == "abc"
    assert _bearer_present({"headers": [(b"authorization", b"Basic abc")]}) == ""
    assert _bearer_present({"headers": [(b"authorization", b"bearer  spaced")]}) == (
        "spaced"
    )
    assert _bearer_present({"headers": []}) == ""
