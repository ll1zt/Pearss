"""Two applications in one process must not share a collector registry.

Scenario register (normative source: issue #84, "应用拥有的运行时"):

  Given an application that has imported a third-party collector
  When  a second application is built in the same process
  Then  the second application's collector set does NOT contain the first's
        installed plugin
  And   both still serve the collectors that ship with Pearss

The assertion is the `GET /v1/plugins` listing, because the collector set an
operator sees is the user-visible fact. A test that reached for
`app.state.collector_registry` would prove a field exists, not that the
applications are independent; the listing is what a second application would
actually answer with.

Before this slice both applications read one module-global
`_INSTALLED_PLUGINS`, so the second listing showed the first's plugin. The
plugin is a real directory imported in a real child process -- the loader's
whole purpose is reading files an operator dropped on disk, so a fake would
be testing the fake.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.orm import Session, sessionmaker

from app.main import create_app

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

WRITE_TOKEN = "test-write-token"
READ_TOKEN = "test-read-token"

SHIPPED = {"rss", "telegram", "twitter", "zhihu", "zsxq"}

# A collector that needs neither network nor credentials, so the import path can
# be exercised without inventing an origin. Mirrors the shape the loader's own
# suite installs; kept local so this file can run alone.
_SIMPLE_COLLECTOR = '''
"""A minimal third-party collector for the registry-isolation test."""

from __future__ import annotations

from core.models import ItemDraft
from core.plugins.hooks import HOOKIMPL, Manifest


@HOOKIMPL
def collector_id() -> str:
    return "%(id)s"


@HOOKIMPL
def match_url(url: str) -> bool:
    return url.startswith("https://isolated.example/")


@HOOKIMPL
def source_config() -> Manifest:
    return Manifest(
        id="%(id)s",
        version="0.1.0",
        name="Isolation Plugin",
        description="Proves registries are per-application.",
        example_url="https://isolated.example/feed",
        features=frozenset(),
        required_config=(),
    )


@HOOKIMPL
def parse(raw) -> list[ItemDraft]:
    return [
        ItemDraft(
            origin_id=raw.source_id,
            external_id="isolated",
            title="From an installed plugin",
            canonical_url="https://example.invalid/isolated",
            published_at=None,
            content="<p>body</p>",
        )
    ]
'''


def _write_plugin(root: Path, plugin_id: str) -> None:
    """Drop a plugin directory where the loader looks for one."""
    directory = root / plugin_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "collector.py").write_text(
        _SIMPLE_COLLECTOR % {"id": plugin_id}, encoding="utf-8"
    )
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "id": plugin_id,
                "version": "0.1.0",
                "name": "Isolation Plugin",
                "description": "Proves registries are per-application.",
                "example_url": "https://isolated.example/feed",
                "features": [],
                "required_config": [],
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def plugin_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """The directory the loader reads plugins from."""
    root = tmp_path / "plugins"
    root.mkdir()
    monkeypatch.setenv("PEARSS_PLUGIN_DIR", str(root))
    yield root


@pytest.fixture(autouse=True)
def _api_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PEARSS_API_TOKEN", WRITE_TOKEN)
    monkeypatch.setenv("PEARSS_API_READ_TOKEN", READ_TOKEN)


def _client(app: FastAPI) -> httpx.AsyncClient:
    """A write-scoped client bound to `app` (one logical write per request)."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={
            "Authorization": f"Bearer {WRITE_TOKEN}",
            "Idempotency-Key": "registry-isolation-key",
        },
    )


async def _listed_ids(app: FastAPI) -> set[str]:
    async with _client(app) as client:
        response = await client.get("/v1/plugins")
        assert response.status_code == 200, response.text
        return {row["id"] for row in response.json()["data"]}


async def test_two_applications_do_not_share_installed_plugins(
    plugin_dir: Path, session_factory: sessionmaker[Session]
) -> None:
    """Given app A imports a plugin, When app B lists, Then B does not have it."""
    _write_plugin(plugin_dir, "isolated")

    first = create_app(session_factory)
    second = create_app(session_factory)

    async with _client(first) as client:
        imported = await client.post("/v1/plugins/isolated:import")
        assert imported.status_code == 200, imported.text

    first_ids = await _listed_ids(first)
    second_ids = await _listed_ids(second)

    # Both applications still serve the shipped set: independence must not be
    # bought by dropping collectors.
    assert first_ids >= SHIPPED
    assert second_ids >= SHIPPED
    assert "isolated" in first_ids
    assert "isolated" not in second_ids, (
        "a plugin imported through one application appeared in another's "
        "collector set: the registry is still shared module state (issue #84)"
    )
