"""The third-party plugin loader: install, use, remove (ADR 0023).

Every scenario drives the HTTP surface an operator actually uses and then
looks at what the system became -- the plugin list, the `plugin` table, the
items a collection stored. Nothing asserts that a helper was called, because
the loader's job is to make a plugin *usable*, and only the end of that path
can show it worked.

The plugins these tests install are real directories with a real
`manifest.json` and a real `collector.py`: the loader's whole purpose is to
read files an operator dropped on disk, so a fake filesystem would be
testing the fake. A `conftest`-level `PEARSS_PLUGIN_DIR` points the loader at
a temporary directory per test.

A note on what is deliberately *not* tested: that a malicious plugin is
contained. ADR 0023 states the position plainly -- the isolation bounds the
*blast radius*, the user owns the *trust decision*, and no mechanism here is
claimed to make an untrusted plugin safe. Testing otherwise would assert a
guarantee the design refuses to make.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy.orm import Session, sessionmaker

from app.main import create_app, create_engine_for
from app.middleware import CorrelationAndAuditMiddleware
from tests.migrated_schema import migrated_database

REPO_ROOT = Path(__file__).resolve().parents[2]
WRITE_TOKEN = "test-write-token"
READ_TOKEN = "test-read-token"

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


# --------------------------------------------------------------------------
# Building a plugin on disk
# --------------------------------------------------------------------------

# A collector that needs no network and no credentials: it answers from the
# payload it is handed, so a test can prove the whole load→collect path
# without inventing an origin.
_SIMPLE_COLLECTOR = '''
"""A minimal third-party collector for the loader tests."""

from __future__ import annotations

from core.models import ItemDraft
from core.plugins.hooks import (
    HOOKIMPL, ConfigRequirement, CollectorError, Manifest,
)


@HOOKIMPL
def collector_id() -> str:
    return "%(id)s"


@HOOKIMPL
def match_url(url: str) -> bool:
    return url.startswith("https://plugin-test.example/")


@HOOKIMPL
def source_config() -> Manifest:
    return Manifest(
        id="%(id)s",
        version="0.1.0",
        name="Plugin Test",
        description="A collector installed at runtime.",
        example_url="https://plugin-test.example/feed",
        features=frozenset(),
        required_config=(),
    )


@HOOKIMPL
def parse(raw) -> list[ItemDraft]:
    return [
        ItemDraft(
            source_id=raw.source_id,
            external_id="one",
            title="From an installed plugin",
            canonical_url="https://example.invalid/one",
            published_at=None,
            content="<p>body</p>",
        )
    ]
'''

# Declares `paged` while implementing no `collect_pages`: the loader must
# refuse it, because "declared but absent" is a contract violation (ADR 0017)
# and a plugin that cannot serve its own manifest is broken, not untrusted.
# The claim is in the code's `source_config()` -- that is the manifest the
# registry would use -- and the test's on-disk manifest.json agrees with it,
# so the refusal is about the missing hook and nothing else.
_MISDECLARED_COLLECTOR = _SIMPLE_COLLECTOR.replace(
    "features=frozenset()", 'features=frozenset({"paged"})'
)


def _write_plugin(
    root: Path,
    plugin_id: str,
    *,
    body: str | None = None,
    manifest: dict[str, object] | None = None,
) -> Path:
    """Drop a plugin directory where the loader looks for one."""
    directory = root / plugin_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "collector.py").write_text(
        (body or _SIMPLE_COLLECTOR) % {"id": plugin_id}, encoding="utf-8"
    )
    (directory / "manifest.json").write_text(
        json.dumps(
            manifest
            if manifest is not None
            else {
                "id": plugin_id,
                "version": "0.1.0",
                "name": "Plugin Test",
                "description": "A collector installed at runtime.",
                "example_url": "https://plugin-test.example/feed",
                "features": [],
                "required_config": [],
            }
        ),
        encoding="utf-8",
    )
    return directory


@pytest.fixture
def plugin_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The directory the loader reads plugins from."""
    root = tmp_path / "plugins"
    root.mkdir()
    monkeypatch.setenv("PEARSS_PLUGIN_DIR", str(root))
    return root


@pytest.fixture
def session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    url = migrated_database(tmp_path, "p.db")
    engine = create_engine_for(url)
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
        headers={
            "Authorization": f"Bearer {WRITE_TOKEN}",
            "Idempotency-Key": "loader-test-key",
        },
    ) as opened:
        yield opened


async def _listed(writer: httpx.AsyncClient) -> dict[str, Any]:
    """The plugins the operator can see, keyed by id."""
    response = await writer.get("/v1/plugins")
    assert response.status_code == 200, response.text
    return {row["id"]: row for row in response.json()["data"]}


def _plugin_rows(
    session_factory: sessionmaker[Session], plugin_id: str
) -> list[tuple[str, str]]:
    """(status, trust) rows for this plugin id, straight from the table."""
    with session_factory() as session:
        return [
            (str(row[0]), str(row[1]))
            for row in session.execute(
                sa.text("select status, trust from plugin where id = :id"),
                {"id": plugin_id},
            ).fetchall()
        ]


def _audit_outcomes(
    session_factory: sessionmaker[Session], operation: str
) -> list[str]:
    # Audit writes are asynchronous (A15); wait for the worker to drain.
    CorrelationAndAuditMiddleware.drain_audit()
    with session_factory() as session:
        return [
            str(row[0])
            for row in session.execute(
                sa.text(
                    "select outcome from audit_log where operation = :op"
                    " order by occurred_at"
                ),
                {"op": operation},
            ).fetchall()
        ]


# --------------------------------------------------------------------------
# Scenario: installing a plugin makes it available
# --------------------------------------------------------------------------


async def test_an_installed_plugin_appears_in_the_list(
    writer: httpx.AsyncClient, plugin_dir: Path
) -> None:
    """Given a plugin on disk, importing it makes it listable.

    The visible result is the plugin list, because that is what an operator
    checks after installing something.
    """
    _write_plugin(plugin_dir, "my-collector")

    before = await _listed(writer)
    assert "my-collector" not in before, "the plugin should not exist yet"

    response = await writer.post("/v1/plugins/my-collector:import")
    assert response.status_code == 200, response.text

    after = await _listed(writer)
    assert "my-collector" in after, "an imported plugin must be listed"
    assert after["my-collector"]["version"] == "0.1.0"


async def test_an_installed_plugin_is_recorded_as_third_party(
    writer: httpx.AsyncClient,
    plugin_dir: Path,
    session_factory: sessionmaker[Session],
) -> None:
    """The table records who supplied it, and it is not `builtin`.

    `trust` is descriptive (ADR 0023): it distinguishes shipped collectors
    from installed ones for the operator, and changes no behaviour.
    """
    _write_plugin(plugin_dir, "my-collector")
    await writer.post("/v1/plugins/my-collector:import")

    assert _plugin_rows(session_factory, "my-collector") == [("enabled", "third-party")]


async def test_importing_is_audited(
    writer: httpx.AsyncClient,
    plugin_dir: Path,
    session_factory: sessionmaker[Session],
) -> None:
    """Installing code is a state change, so it leaves an audit trail."""
    _write_plugin(plugin_dir, "my-collector")
    await writer.post("/v1/plugins/my-collector:import")

    outcomes = _audit_outcomes(session_factory, "POST /v1/plugins/{plugin_id}:import")
    assert outcomes == ["succeeded"], outcomes


# --------------------------------------------------------------------------
# Scenario: a plugin that contradicts itself is refused, leaving no trace
# --------------------------------------------------------------------------


async def test_a_plugin_whose_manifest_overpromises_is_refused(
    writer: httpx.AsyncClient,
    plugin_dir: Path,
    session_factory: sessionmaker[Session],
) -> None:
    """Declaring `paged` without implementing it must not install.

    This is the ADR 0017 contract check moved to install time: a plugin that
    cannot serve its own manifest would fail at the first collection instead,
    which is the wrong moment to discover it.
    """
    _write_plugin(
        plugin_dir,
        "broken",
        body=_MISDECLARED_COLLECTOR,
        manifest={
            "id": "broken",
            "version": "0.1.0",
            "name": "Overpromising",
            "description": "Declares paged without implementing it.",
            "example_url": "https://plugin-test.example/feed",
            "features": ["paged"],
            "required_config": [],
        },
    )

    response = await writer.post("/v1/plugins/broken:import")

    assert response.status_code == 400, response.text
    message = json.dumps(response.json())
    assert "paged" in message, "the refusal must name the unmet declaration"
    assert "broken" not in {row["id"] for row in (await _listed(writer)).values()}
    assert _plugin_rows(session_factory, "broken") == []


async def test_importing_a_directory_that_does_not_exist_is_refused(
    writer: httpx.AsyncClient, plugin_dir: Path
) -> None:
    """A missing plugin says where it looked, not just that it failed."""
    response = await writer.post("/v1/plugins/absent:import")

    assert response.status_code == 404, response.text
    body = response.json()["detail"]["error"]
    assert str(plugin_dir) in json.dumps(body), (
        "the error must name the directory the loader searched"
    )


async def test_importing_a_plugin_whose_code_does_not_run_is_refused(
    writer: httpx.AsyncClient, plugin_dir: Path, session_factory: sessionmaker[Session]
) -> None:
    """A plugin that cannot be imported at all leaves nothing behind.

    The check is a real import in a child process (ADR 0023), so a syntax
    error or a bad top-level statement is found at install time.
    """
    _write_plugin(plugin_dir, "syntax", body="def broken(:\n    pass\n")

    response = await writer.post("/v1/plugins/syntax:import")

    assert response.status_code == 400, response.text
    assert _plugin_rows(session_factory, "syntax") == []


async def test_a_plugin_whose_id_does_not_match_its_directory_is_refused(
    writer: httpx.AsyncClient, plugin_dir: Path
) -> None:
    """The directory name is the id the operator typed; a mismatch is a trap.

    Silently accepting it would install a plugin under one name that calls
    itself another, and the listing would disagree with the filesystem.
    """
    _write_plugin(
        plugin_dir,
        "documents-name",
        manifest={
            "id": "different-name",
            "version": "0.1.0",
            "name": "Mismatch",
            "description": "id disagrees with the directory.",
            "example_url": "https://plugin-test.example/feed",
            "features": [],
            "required_config": [],
        },
    )

    response = await writer.post("/v1/plugins/documents-name:import")

    assert response.status_code == 400, response.text
    assert "different-name" in json.dumps(response.json())


# --------------------------------------------------------------------------
# Scenario: removing a plugin
# --------------------------------------------------------------------------


async def test_uninstalling_removes_it_from_the_list_but_keeps_the_files(
    writer: httpx.AsyncClient,
    plugin_dir: Path,
    session_factory: sessionmaker[Session],
) -> None:
    """The host owns its state, not the operator's directory.

    Deleting files would fuse a reversible act (uninstall) with an
    irreversible one (deleting the user's data).
    """
    _write_plugin(plugin_dir, "my-collector")
    await writer.post("/v1/plugins/my-collector:import")

    response = await writer.delete("/v1/plugins/my-collector")
    assert response.status_code == 200, response.text

    assert "my-collector" not in await _listed(writer)
    assert _plugin_rows(session_factory, "my-collector") == []
    assert (plugin_dir / "my-collector" / "collector.py").exists(), (
        "uninstall must not delete the operator's files"
    )


async def test_uninstalling_a_plugin_with_a_live_source_is_refused(
    writer: httpx.AsyncClient, plugin_dir: Path
) -> None:
    """Pulling a collector out from under active sources is not silent.

    The sources would otherwise fail on their next tick with a classification
    that blamed the origin.
    """
    _write_plugin(plugin_dir, "my-collector")
    await writer.post("/v1/plugins/my-collector:import")
    await writer.post(
        "/v1/sources",
        json={
            "url": "https://plugin-test.example/feed",
            "type": "web",
            "schedule_policy": {"type": "fixed", "interval": "60m"},
        },
    )

    response = await writer.delete("/v1/plugins/my-collector")

    assert response.status_code == 409, response.text
    assert "my-collector" in await _listed(writer), (
        "a refused uninstall removes nothing"
    )


async def test_user_retries_uninstall_without_losing_the_successful_result(
    writer: httpx.AsyncClient, plugin_dir: Path, session_factory: sessionmaker[Session]
) -> None:
    """Given an uninstall, when its identical key is retried, then success replays."""
    _write_plugin(plugin_dir, "my-collector")
    assert (await writer.post("/v1/plugins/my-collector:import")).status_code == 200
    first = await writer.delete("/v1/plugins/my-collector")
    retry = await writer.delete("/v1/plugins/my-collector")
    assert first.status_code == retry.status_code == 200
    assert retry.json() == first.json()
    assert retry.headers["Idempotent-Replay"] == "true"
    assert _plugin_rows(session_factory, "my-collector") == []


@pytest.mark.parametrize("action", ["uninstall", "reload"])
async def test_user_lifecycle_write_without_key_is_refused_without_effect(
    writer: httpx.AsyncClient,
    plugin_dir: Path,
    action: str,
) -> None:
    """An installed plugin stays installed when the write arrives without a key."""
    _write_plugin(plugin_dir, "my-collector")
    assert (await writer.post("/v1/plugins/my-collector:import")).status_code == 200
    before = await _listed(writer)
    request = writer.build_request(
        "DELETE" if action == "uninstall" else "POST",
        "/v1/plugins/my-collector" + (":reload" if action == "reload" else ""),
    )
    del request.headers["Idempotency-Key"]
    response = await writer.send(request)
    assert response.status_code == 400
    assert response.json()["detail"]["error"]["code"] == "idempotency_key_required"
    assert await _listed(writer) == before


@pytest.mark.parametrize("action", ["uninstall", "reload"])
async def test_user_lifecycle_key_with_changed_body_is_refused(
    writer: httpx.AsyncClient,
    plugin_dir: Path,
    action: str,
) -> None:
    """Given completed lifecycle work, a mismatched retry is 409 without new effects."""
    _write_plugin(plugin_dir, "my-collector")
    assert (await writer.post("/v1/plugins/my-collector:import")).status_code == 200
    method = "DELETE" if action == "uninstall" else "POST"
    path = "/v1/plugins/my-collector" + (":reload" if action == "reload" else "")
    first = await writer.request(method, path, json={"request": "original"})
    assert first.status_code == 200
    before = await _listed(writer)
    refused = await writer.request(method, path, json={"request": "changed"})
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"]["code"] == "idempotency_key_conflict"
    assert "Idempotent-Replay" not in refused.headers
    assert await _listed(writer) == before


async def test_user_retried_reload_keeps_the_loaded_version(
    writer: httpx.AsyncClient,
    plugin_dir: Path,
) -> None:
    """A reload retried under its key never replaces the loaded plugin again."""
    directory = _write_plugin(plugin_dir, "my-collector")
    assert (await writer.post("/v1/plugins/my-collector:import")).status_code == 200
    first = await writer.post("/v1/plugins/my-collector:reload")
    assert first.status_code == 200
    # A real on-disk edit would be picked up by a second lifecycle execution.
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["version"] = "0.2.0"
    manifest_path.write_text(json.dumps(manifest))
    code_path = directory / "collector.py"
    code_path.write_text(
        code_path.read_text().replace('version="0.1.0"', 'version="0.2.0"')
    )
    retry = await writer.post("/v1/plugins/my-collector:reload")
    assert retry.status_code == 200
    assert retry.json() == first.json()
    assert retry.headers["Idempotent-Replay"] == "true"
    assert (await _listed(writer))["my-collector"]["version"] == "0.1.0"


async def test_uninstalling_an_unknown_plugin_is_404(
    writer: httpx.AsyncClient, plugin_dir: Path
) -> None:
    response = await writer.delete("/v1/plugins/never-installed")

    assert response.status_code == 404, response.text
