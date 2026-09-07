"""Integration tests for the /v1/plugins lifecycle surface (P2, ADR 0017).

The registry's startup verification and the manifest round-trip are
exercised through the real app: a migrated database, the lifespan sync, and
the authenticated endpoints.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from sqlalchemy.orm import Session, sessionmaker

from app.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]
WRITE_TOKEN = "test-write-token"
READ_TOKEN = "test-read-token"


@pytest.fixture
def session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    url = str(sa.URL.create("sqlite+pysqlite", database=str(tmp_path / "p.db")))
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    engine = sa.create_engine(url)
    try:
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        engine.dispose()


@pytest.fixture
def app(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> FastAPI:
    monkeypatch.setenv("PEARSS_API_TOKEN", WRITE_TOKEN)
    monkeypatch.setenv("PEARSS_API_READ_TOKEN", READ_TOKEN)
    return create_app(session_factory)


@pytest_asyncio.fixture
async def writer(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {WRITE_TOKEN}"},
    ) as opened:
        yield opened


@pytest.mark.integration
@pytest.mark.asyncio
async def test_startup_syncs_five_manifests(writer: httpx.AsyncClient) -> None:
    body = (await writer.get("/v1/plugins")).json()
    by_id = {row["id"]: row for row in body["data"]}
    assert set(by_id) == {"rss", "telegram", "twitter", "zhihu", "zsxq"}
    for row in body["data"]:
        assert row["status"] == "enabled"
        assert row["checksum"], "every manifest carries its checksum"
        assert row["version"] == "0.1.0"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_login_type_sources_declare_required_config(
    writer: httpx.AsyncClient,
) -> None:
    body = (await writer.get("/v1/plugins")).json()
    by_id = {row["id"]: row for row in body["data"]}

    assert by_id["rss"]["required_config"] == []
    zhihu = by_id["zhihu"]["required_config"]
    assert zhihu and zhihu[0]["env_name"] == "PEARSS_ZHIHU_COOKIES"
    assert zhihu[0]["required"] is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_disable_then_enable_round_trip(writer: httpx.AsyncClient) -> None:
    disabled = await writer.post("/v1/plugins/telegram:disable")
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "disabled"

    # Disable is durable: the listing shows it, not just the write response.
    listed = (await writer.get("/v1/plugins")).json()
    telegram = next(row for row in listed["data"] if row["id"] == "telegram")
    assert telegram["status"] == "disabled"

    enabled = await writer.post("/v1/plugins/telegram:enable")
    assert enabled.status_code == 200
    assert enabled.json()["status"] == "enabled"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_disabling_an_disabled_plugin_conflicts(
    writer: httpx.AsyncClient,
) -> None:
    first = await writer.post("/v1/plugins/rss:disable")
    assert first.status_code == 200
    second = await writer.post("/v1/plugins/rss:disable")
    assert second.status_code == 409
    assert second.json()["detail"]["error"]["code"] == "plugin_status_unchanged"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unknown_plugin_is_404(writer: httpx.AsyncClient) -> None:
    response = await writer.post("/v1/plugins/missing:disable")
    assert response.status_code == 404
    assert response.json()["detail"]["error"]["code"] == "plugin_not_found"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_p2b_operations_are_honest_placeholders(
    writer: httpx.AsyncClient,
) -> None:
    """Import/uninstall/reload need the loader; saying so beats faking it."""
    for method, path in (
        ("POST", "/v1/plugins/rss:import"),
        ("DELETE", "/v1/plugins/rss"),
        ("POST", "/v1/plugins/rss:reload"),
    ):
        response = await writer.request(method, path)
        assert response.status_code == 409, path
        assert (
            response.json()["detail"]["error"]["code"] == "plugin_operation_unavailable"
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_read_token_can_list_but_not_disable(
    app: FastAPI, writer: httpx.AsyncClient
) -> None:
    reader = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {READ_TOKEN}"},
    )
    listed = await reader.get("/v1/plugins")
    assert listed.status_code == 200

    refused = await reader.post("/v1/plugins/rss:disable")
    assert refused.status_code == 403
