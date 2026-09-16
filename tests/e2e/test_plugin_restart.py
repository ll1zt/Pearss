"""User-installed collectors survive an actual service process replacement.

Given plugins imported through HTTP into a file-backed database, when a fresh
OS process opens that database, then only durable installations are available,
with their lifecycle decisions intact. No registry reset substitutes for restart.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from adapters.repository import list_plugin_rows
from tests.e2e.conftest import REPO_ROOT, _FixtureOrigin, _free_port
from tests.e2e.test_plugin_collection import (
    _FETCHING_COLLECTOR,
    _add_source,
    _client,
    _install_plugin,
    _key,
    _titles,
)

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]


@contextmanager
def _service_process(tmp_path: Path) -> Iterator[str]:
    """Serve the production entry point in an independent Python interpreter."""
    import httpx

    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    with (tmp_path / "service.log").open("w+") as log:
        process = subprocess.Popen(  # noqa: S603 - fixed local service command
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=REPO_ROOT,
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")},
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 30
            with httpx.Client(timeout=0.3, trust_env=False) as probe:
                while process.poll() is None and time.monotonic() < deadline:
                    try:
                        if probe.get(url + "/healthz").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    # Readiness polling, not a wait for a business outcome.
                    threading.Event().wait(0.05)
                else:
                    log.seek(0)
                    pytest.fail(f"service failed to start: {log.read()}")
            yield url
        finally:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def _write_collector(root: Path, plugin_id: str) -> None:
    body = _FETCHING_COLLECTOR.replace(
        'return url.startswith("http://127.0.0.1:")',
        'return url.startswith("http://127.0.0.1:") and "?plugin=%(id)s" in url',
    )
    _install_plugin(root, plugin_id, body, label=plugin_id)


def _break_collector(root: Path, plugin_id: str) -> None:
    """Corrupt an installed plugin's code the way a bad restore would."""
    (root / plugin_id / "collector.py").write_text(
        "this is not python\n", encoding="utf-8"
    )


def _drop_manifest(root: Path, plugin_id: str) -> None:
    (root / plugin_id / "manifest.json").unlink()


def _make_changed_credential_manifest(root: Path, plugin_id: str) -> None:
    """Ask for a credential the operator never consented to at import time."""
    manifest = json.loads(
        (root / plugin_id / "manifest.json").read_text(encoding="utf-8")
    )
    manifest["required_config"] = [{"env_name": "SECRET_"}]
    (root / plugin_id / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )


def _recorded_plugins(
    factory: sessionmaker[Session],
) -> dict[str, tuple[str, str, str]]:
    with factory() as session:
        return {
            row.id: (row.status, row.checksum, row.manifest_json)
            for row in list_plugin_rows(session, tenant_id="default")
        }


async def test_renamed_or_missing_files_are_reported_not_silently_dropped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
) -> None:
    """User restarts with broken files; records survive but code is not loaded."""
    root = tmp_path / "plugins"
    monkeypatch.setenv("PEARSS_PLUGIN_DIR", str(root))
    _write_collector(root, "stays-broken")
    _write_collector(root, "losts-manifest")

    with _FixtureOrigin(b"BODY") as origin:
        with _service_process(tmp_path) as first:
            async with _client(first) as client:
                for plugin_id in ("stays-broken", "losts-manifest"):
                    imported = await client.post(
                        f"/v1/plugins/{plugin_id}:import", headers=_key()
                    )
                    assert imported.status_code == 200, imported.text
                source_id = await _add_source(
                    client, origin.url + "?plugin=stays-broken"
                )

        recorded = _recorded_plugins(session_factory)
        _break_collector(root, "stays-broken")
        _drop_manifest(root, "losts-manifest")

        with _service_process(tmp_path) as second:
            async with _client(second) as client:
                failed = await client.post(
                    f"/v1/sources/{source_id}:trigger", headers=_key()
                )
                assert failed.status_code == 502, failed.text
                plugins = {
                    p["id"]: p for p in (await client.get("/v1/plugins")).json()["data"]
                }
                assert "stays-broken" not in plugins
                assert "losts-manifest" not in plugins
                assert _recorded_plugins(session_factory) == recorded
                assert _titles(session_factory, source_id) == []
                listed = await client.get("/v1/plugins/stays-broken")
                assert listed.status_code == 404


async def test_renewed_credential_consent_is_required_after_manifest_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
) -> None:
    """User restarts after credential changes; no code runs without new consent."""
    root = tmp_path / "plugins"
    monkeypatch.setenv("PEARSS_PLUGIN_DIR", str(root))
    _write_collector(root, "credential-grab")

    with _FixtureOrigin(b"BODY") as origin:
        with _service_process(tmp_path) as first:
            async with _client(first) as client:
                imported = await client.post(
                    "/v1/plugins/credential-grab:import", headers=_key()
                )
                assert imported.status_code == 200, imported.text
                source_id = await _add_source(
                    client, origin.url + "?plugin=credential-grab"
                )

        recorded = _recorded_plugins(session_factory)
        _make_changed_credential_manifest(root, "credential-grab")
        collector = root / "credential-grab" / "collector.py"
        collector.write_text(
            collector.read_text(encoding="utf-8")
            + "\nfrom pathlib import Path\n"
            + 'Path(__file__).with_name("executed").write_text("ran")\n',
            encoding="utf-8",
        )

        with _service_process(tmp_path) as second:
            async with _client(second) as client:
                failed = await client.post(
                    f"/v1/sources/{source_id}:trigger", headers=_key()
                )
                assert failed.status_code == 502, failed.text
                plugins = {
                    p["id"]: p for p in (await client.get("/v1/plugins")).json()["data"]
                }
                assert "credential-grab" not in plugins
                assert _recorded_plugins(session_factory) == recorded
                assert _titles(session_factory, source_id) == []
                assert not (root / "credential-grab" / "executed").exists()


async def test_user_restarts_service_and_keeps_only_installed_collectors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
) -> None:
    """Given imports and disable/uninstall, when restarted, collection still works."""
    root = tmp_path / "plugins"
    monkeypatch.setenv("PEARSS_PLUGIN_DIR", str(root))
    for plugin_id in (
        "active-plugin",
        "disabled-plugin",
        "removed-plugin",
        "disk-only",
    ):
        _write_collector(root, plugin_id)

    with _FixtureOrigin(b"RESTART PAGE BODY") as origin:
        with _service_process(tmp_path) as first:
            async with _client(first) as client:
                for plugin_id in ("active-plugin", "disabled-plugin", "removed-plugin"):
                    imported = await client.post(
                        f"/v1/plugins/{plugin_id}:import", headers=_key()
                    )
                    assert imported.status_code == 200, imported.text
                disabled = await client.post(
                    "/v1/plugins/disabled-plugin:disable", headers=_key()
                )
                assert disabled.status_code == 200, disabled.text
                removed = await client.delete(
                    "/v1/plugins/removed-plugin", headers=_key()
                )
                assert removed.status_code == 200, removed.text
                source_id = await _add_source(
                    client, origin.url + "?plugin=active-plugin"
                )
                disabled_id = await _add_source(
                    client, origin.url + "?plugin=disabled-plugin"
                )
                shipped_before = {
                    p["id"]: p
                    for p in (await client.get("/v1/plugins")).json()["data"]
                    if p["id"] not in {"active-plugin", "disabled-plugin"}
                }

        # The first interpreter is dead. The second has no inherited module state.
        with _service_process(tmp_path) as second:
            async with _client(second) as client:
                triggered = await client.post(
                    f"/v1/sources/{source_id}:trigger", headers=_key()
                )
                assert triggered.status_code == 200, triggered.text
                assert _titles(session_factory, source_id) == [
                    "Plugin fetched: active-plugin"
                ]
                plugins = {
                    p["id"]: p for p in (await client.get("/v1/plugins")).json()["data"]
                }
                assert plugins["active-plugin"]["status"] == "enabled"
                assert plugins["disabled-plugin"]["status"] == "disabled"
                assert set(plugins) == set(shipped_before) | {
                    "active-plugin",
                    "disabled-plugin",
                }
                assert {
                    name: plugins[name] for name in shipped_before
                } == shipped_before
                refused = await client.post(
                    f"/v1/sources/{disabled_id}:trigger", headers=_key()
                )
                assert refused.status_code == 200, refused.text
                assert refused.json()["succeeded"] is True, refused.text
                assert refused.json()["drafts"] == 0, refused.text
                assert refused.json()["inserted"] == 0, refused.text
                assert _titles(session_factory, disabled_id) == []
                assert (root / "removed-plugin" / "collector.py").is_file()
