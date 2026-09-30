"""A03: installing plugins must preserve shipped collectors and their sources.

Real API sockets, migrated SQLite, plugin subprocesses and a TCP fixture origin.
Only Telegram's external origin is redirected to the recorded fixture; no
registry, pipeline, collector or repository implementation is substituted.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy.orm import Session, sessionmaker

from adapters.db_models import Plugin
from tests.e2e.conftest import REPO_ROOT, _FixtureOrigin
from tests.e2e.test_plugin_collection import (
    _FETCHING_COLLECTOR,
    _add_source,
    _client,
    _install_plugin,
    _key,
    _titles,
)

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]

SHIPPED = {"rss", "telegram", "twitter", "zhihu", "zsxq"}
TELEGRAM_URL = "https://t.me/registry_fixture"


class _FixtureOriginTransport(httpx.BaseTransport):
    """Route the external Telegram URL to a real, local fixture server."""

    def __init__(self, origin_url: str) -> None:
        self._origin = httpx.URL(origin_url)
        self._transport = httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "t.me":
            # The real shipped fetch hook must perform the preview rewrite.
            assert request.url.path == "/s/registry_fixture"
            request = httpx.Request(request.method, self._origin)
        else:
            assert request.url.host == "127.0.0.1"
        response = self._transport.handle_request(request)
        try:
            response.read()
            return response
        finally:
            response.close()

    def close(self) -> None:
        self._transport.close()


@pytest.fixture
def plugin_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "plugins"
    root.mkdir()
    monkeypatch.setenv("PEARSS_PLUGIN_DIR", str(root))
    return root


@pytest.fixture
def origin() -> Iterator[_FixtureOrigin]:
    fixture = REPO_ROOT / "src/plugins/collectors/telegram/fixtures/legacy_ids.html"
    with _FixtureOrigin(fixture.read_bytes()) as served:
        yield served


async def _listed(client: httpx.AsyncClient) -> set[str]:
    response = await client.get("/v1/plugins")
    assert response.status_code == 200, response.text
    ids = [row["id"] for row in response.json()["data"]]
    assert len(ids) == len(set(ids)), "a rebuild must not duplicate registrations"
    return set(ids)


async def _collect(client: httpx.AsyncClient, source_id: str) -> None:
    response = await client.post(f"/v1/sources/{source_id}:trigger", headers=_key())
    assert response.status_code == 200, response.text
    assert response.json()["succeeded"] is True
    assert response.json()["drafts"] == 1


@pytest.mark.parametrize("operation", ["reimport", "reload", "uninstall"])
async def test_user_keeps_shipped_sources_when_installing_a_plugin(
    served: str,
    app: FastAPI,
    plugin_dir: Path,
    origin: _FixtureOrigin,
    session_factory: sessionmaker[Session],
    operation: str,
) -> None:
    """Given a working shipped source, When importing, Then both sources work.

    A subsequent rebuild or uninstall must also preserve the shipped set.
    The second Telegram fetch asserts a parsed draft, not merely old DB rows.
    """
    _install_plugin(plugin_dir, "fetching", _FETCHING_COLLECTOR)
    with _FixtureOriginTransport(origin.url) as transport:
        app.state.collector_registry.set_http_transport("telegram", transport)
        async with _client(served) as client:
            assert await _listed(client) == SHIPPED
            telegram_id = await _add_source(client, TELEGRAM_URL)
            await _collect(client, telegram_id)
            assert _titles(session_factory, telegram_id) == [
                "Older previews carry only an id."
            ]

            imported = await client.post("/v1/plugins/fetching:import", headers=_key())
            assert imported.status_code == 200, imported.text
            assert await _listed(client) == SHIPPED | {"fetching"}
            await _collect(client, telegram_id)
            plugin_source_id = await _add_source(client, origin.url)
            await _collect(client, plugin_source_id)
            assert _titles(session_factory, plugin_source_id) == [
                "Plugin fetched: original"
            ]

            if operation == "uninstall":
                # Removing the source lets the normal uninstall gate admit it.
                deleted = await client.delete(
                    f"/v1/sources/{plugin_source_id}", headers=_key()
                )
                assert deleted.status_code == 200, deleted.text
                response = await client.delete("/v1/plugins/fetching", headers=_key())
                expected = SHIPPED
            else:
                suffix = "import" if operation == "reimport" else "reload"
                response = await client.post(
                    f"/v1/plugins/fetching:{suffix}", headers=_key()
                )
                expected = SHIPPED | {"fetching"}
            assert response.status_code == 200, response.text
            assert await _listed(client) == expected
            await _collect(client, telegram_id)
            assert _titles(session_factory, telegram_id) == [
                "Older previews carry only an id."
            ]
            if operation != "uninstall":
                await _collect(client, plugin_source_id)
                assert _titles(session_factory, plugin_source_id) == [
                    "Plugin fetched: original"
                ]


async def test_user_keeps_shipped_sources_after_rejected_import(
    served: str,
    app: FastAPI,
    plugin_dir: Path,
    origin: _FixtureOrigin,
    session_factory: sessionmaker[Session],
) -> None:
    """Given shipped collectors, When an invalid import is refused, Then no loss."""
    _install_plugin(plugin_dir, "broken", "def broken(:\n")
    with _FixtureOriginTransport(origin.url) as transport:
        app.state.collector_registry.set_http_transport("telegram", transport)
        async with _client(served) as client:
            assert await _listed(client) == SHIPPED
            telegram_id = await _add_source(client, TELEGRAM_URL)
            refused = await client.post("/v1/plugins/broken:import", headers=_key())
            assert refused.status_code == 400, refused.text
            assert refused.json()["detail"]["error"]["code"] == "plugin_load_failed"
            assert await _listed(client) == SHIPPED
            with session_factory() as session:
                assert (
                    list(
                        session.scalars(
                            sa.select(Plugin.id).where(Plugin.id == "broken")
                        )
                    )
                    == []
                )
            await _collect(client, telegram_id)
            assert _titles(session_factory, telegram_id) == [
                "Older previews carry only an id."
            ]
