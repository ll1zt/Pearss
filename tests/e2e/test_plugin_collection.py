"""An installed plugin collecting for real (ADR 0023).

The integration suite proves the loader validates and records a plugin. This
file proves the part that needs a live application: a source routed to an
installed plugin actually collects, and the plugin's own code is what ran.

It lives in e2e because collection is what needs the pieces a bare ASGI
transport does not provide -- the app lifespan (which injects the shared
egress client the sandboxed child proxies through) and a real child process.
A fixture origin serves the plugin's HTTP, so nothing about the plugin path
is stubbed: the plugin is a real directory, imported in a real child, and its
request goes out over real TCP to a local server.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from core.enums import ErrorClass
from tests.e2e.conftest import API_TOKEN, _FixtureOrigin

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]


# A plugin that really fetches: it asks for the URL's page through the child's
# proxied client (so the host's SSRF guard and the origin's fixture are both
# in the path) and turns the body into one draft.
#
# `%(label)s` is substituted per install, which is what makes an edited
# revision observable: the stored title says which revision produced it.
_FETCHING_COLLECTOR = '''
"""A third-party collector that fetches, for the loader e2e tests."""

from __future__ import annotations

from adapters.http import fetch as plain_fetch
from core.models import ItemDraft
from core.plugins.hooks import HOOKIMPL, Manifest


@HOOKIMPL
def collector_id() -> str:
    return "%(id)s"


@HOOKIMPL
def match_url(url: str) -> bool:
    return url.startswith("http://127.0.0.1:")


@HOOKIMPL
def source_config() -> Manifest:
    return Manifest(
        id="%(id)s",
        version="0.1.0",
        name="Fetching Plugin",
        description="Fetches a page and stores its body.",
        example_url="http://127.0.0.1:1/feed",
        features=frozenset({"fetch"}),
        required_config=(),
    )


@HOOKIMPL
def fetch(url: str, *, client, source_id: str, conditional=None):
    return plain_fetch(url, client=client, source_id=source_id)


@HOOKIMPL
def parse(raw) -> list[ItemDraft]:
    return [
        ItemDraft(
            origin_id=raw.source_id,
            external_id="fetched-one",
            title="Plugin fetched: %(label)s",
            canonical_url="https://example.invalid/fetched",
            published_at=None,
            content="<p>" + raw.text().strip()[:200] + "</p>",
        )
    ]
'''

_MANIFEST = {
    "id": "",
    "version": "0.1.0",
    "name": "Fetching Plugin",
    "description": "Fetches a page and stores its body.",
    "example_url": "http://127.0.0.1:1/feed",
    "features": ["fetch"],
    "required_config": [],
}


@pytest.fixture
def plugin_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "plugins"
    root.mkdir()
    monkeypatch.setenv("PEARSS_PLUGIN_DIR", str(root))
    return root


def _install_plugin(
    root: Path, plugin_id: str, body: str, *, label: str = "original"
) -> None:
    """Write a plugin revision to disk, replacing any previous one."""
    directory = root / plugin_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "collector.py").write_text(
        body % {"id": plugin_id, "label": label}, encoding="utf-8"
    )
    (directory / "manifest.json").write_text(
        json.dumps({**_MANIFEST, "id": plugin_id}), encoding="utf-8"
    )


@pytest.fixture
def origin() -> Iterator[_FixtureOrigin]:
    """A real local HTTP server the plugin will fetch."""
    with _FixtureOrigin(b"ORIGINAL PAGE BODY") as served:
        yield served


def _client(served: str) -> httpx.AsyncClient:
    """A write-scoped client with no default Idempotency-Key.

    Each write in these tests is a distinct logical operation, so the key is
    sent per request. A client-wide key would make the *second* `POST
    /v1/sources` replay the first response -- including the first source's id
    -- and the test would silently exercise one source twice.
    """
    return httpx.AsyncClient(
        base_url=served,
        headers={"Authorization": f"Bearer {API_TOKEN}"},
    )


def _key() -> dict[str, str]:
    """A fresh Idempotency-Key: one per logical write, per the API contract."""
    import uuid

    return {"Idempotency-Key": f"e2e-plugin-{uuid.uuid4()}"}


def _titles(session_factory: sessionmaker[Session], source_id: str) -> list[str]:
    with session_factory() as session:
        return [
            str(row[0])
            for row in session.execute(
                sa.text("select title from item where source_id = :id order by rowid"),
                {"id": source_id},
            ).fetchall()
        ]


async def _add_source(client: httpx.AsyncClient, url: str) -> str:
    response = await client.post(
        "/v1/sources",
        headers=_key(),
        json={
            "url": url,
            "type": "web",
            "schedule_policy": {"type": "fixed", "interval": "60m"},
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def test_an_installed_plugin_collects_through_the_sandbox(
    served: str,
    plugin_dir: Path,
    origin: _FixtureOrigin,
    session_factory: sessionmaker[Session],
) -> None:
    """A source routed to an installed plugin stores the plugin's item.

    End to end in the sense that matters here: the operator installed a
    plugin, created a source, triggered it, and found an item in the database
    whose content the plugin's own code produced.
    """
    _install_plugin(plugin_dir, "fetching", _FETCHING_COLLECTOR)

    async with _client(served) as client:
        imported = await client.post("/v1/plugins/fetching:import", headers=_key())
        assert imported.status_code == 200, imported.text

        source_id = await _add_source(client, origin.url)
        triggered = await client.post(
            f"/v1/sources/{source_id}:trigger", headers=_key()
        )
        assert triggered.status_code == 200, triggered.text

    assert _titles(session_factory, source_id) == ["Plugin fetched: original"]


async def test_reload_changes_what_the_next_collection_produces(
    served: str,
    plugin_dir: Path,
    origin: _FixtureOrigin,
    session_factory: sessionmaker[Session],
) -> None:
    """After editing the plugin, a reload makes the new revision run.

    Asserted through the stored title, because that is the only evidence the
    edit reached the running child: a reload that reported success while the
    old process kept serving would store the old label here.

    The edit changes the plugin's own behaviour (the label it writes), not the
    page -- so the stored title is evidence about which revision executed
    rather than about what the origin served.
    """
    _install_plugin(plugin_dir, "fetching", _FETCHING_COLLECTOR, label="original")

    async with _client(served) as client:
        assert (
            await client.post("/v1/plugins/fetching:import", headers=_key())
        ).status_code == 200

        first_id = await _add_source(client, origin.url)
        first = await client.post(f"/v1/sources/{first_id}:trigger", headers=_key())
        assert first.status_code == 200, first.text
        assert _titles(session_factory, first_id) == ["Plugin fetched: original"]

        _install_plugin(plugin_dir, "fetching", _FETCHING_COLLECTOR, label="edited")
        reloaded = await client.post("/v1/plugins/fetching:reload", headers=_key())
        assert reloaded.status_code == 200, reloaded.text

        # A second source so this collection is not deduped against the first
        # item (same external_id, same content hash).
        second_id = await _add_source(client, origin.url + "?revision=2")
        second = await client.post(f"/v1/sources/{second_id}:trigger", headers=_key())
        assert second.status_code == 200, second.text

    assert _titles(session_factory, second_id) == ["Plugin fetched: edited"], (
        "the reload did not reach the child that collected"
    )


async def test_a_plugin_that_crashes_leaves_the_host_serving(
    served: str,
    plugin_dir: Path,
    origin: _FixtureOrigin,
    session_factory: sessionmaker[Session],
) -> None:
    """A plugin failing its parse does not take the service down.

    This is the isolation claim reduced to something observable: the run is
    recorded as a failure, and the API still answers afterwards. The guarantee
    is about blast radius (ADR 0023), not about the plugin being trustworthy,
    so this is the property worth pinning.
    """
    crashing = _FETCHING_COLLECTOR.replace(
        "    return [\n        ItemDraft(",
        "    raise RuntimeError('plugin exploded')\n    return [\n        ItemDraft(",
    )
    _install_plugin(plugin_dir, "crashing", crashing, label="crashing")

    async with _client(served) as client:
        assert (
            await client.post("/v1/plugins/crashing:import", headers=_key())
        ).status_code == 200

        source_id = await _add_source(client, origin.url)
        triggered = await client.post(
            f"/v1/sources/{source_id}:trigger", headers=_key()
        )

        # The failure is reported as a classified parse failure, and the
        # service keeps answering.
        assert triggered.status_code == 502, triggered.text
        assert triggered.json()["error_class"] == "parse", triggered.text
        assert (await client.get("/healthz")).status_code == 200
        assert (await client.get("/v1/plugins")).status_code == 200

    assert _titles(session_factory, source_id) == []
    with session_factory() as session:
        statuses = [
            str(row[0])
            for row in session.execute(
                sa.text("select status from job_run order by started_at")
            ).fetchall()
        ]
    assert statuses and statuses[-1] != "succeeded", statuses


# --------------------------------------------------------------------------
# Classified parse failures (issue #2): the class the child reported is the
# class the run records, for every ErrorClass member, and the detail that
# travelled here is not amputated at 200 characters.
# --------------------------------------------------------------------------


def _raise_in_parse(error_class: str, detail: str) -> str:
    """A collector revision whose parse raises the given classified error."""
    return _FETCHING_COLLECTOR.replace(
        "    return [",
        "    from core.enums import ErrorClass\n"
        "    from core.plugins.hooks import CollectorError\n"
        f'    raise CollectorError(ErrorClass("{error_class}"), "{detail}")\n'
        "    return [",
    )


@pytest.mark.parametrize(
    "error_class", [member.value for member in ErrorClass], ids=str
)
async def test_user_a_classified_plugin_parse_keeps_its_class(
    served: str,
    plugin_dir: Path,
    origin: _FixtureOrigin,
    session_factory: sessionmaker[Session],
    error_class: str,
) -> None:
    """Given a plugin whose parse raises a classified CollectorError, when
    its source is collected, then the run records THAT class — not a
    blanket parse.

    The child's error whitelist carries every ErrorClass member and the
    fetch hook preserved it, but the parse path let ClassifiedChildError
    escape into the scheduler's last-resort handler, which labels
    everything parse. structure_changed never appeared in job_run, so an
    operator debugging a quiet account was pointed at the parser.
    """

    _install_plugin(
        plugin_dir,
        "classified",
        _raise_in_parse(error_class, "origin refused this shape"),
        label="classified",
    )

    async with _client(served) as client:
        assert (
            await client.post("/v1/plugins/classified:import", headers=_key())
        ).status_code == 200

        source_id = await _add_source(client, origin.url)
        triggered = await client.post(
            f"/v1/sources/{source_id}:trigger", headers=_key()
        )
        assert triggered.status_code == 502, triggered.text
        assert triggered.json()["error_class"] == error_class, triggered.text

        with session_factory() as session:
            recorded = session.execute(
                sa.text("select error_class from job_run")
            ).scalar_one()
        assert recorded == error_class, recorded


async def test_user_the_classified_detail_is_not_amputated(
    served: str,
    plugin_dir: Path,
    origin: _FixtureOrigin,
    session_factory: sessionmaker[Session],
) -> None:
    """The detail a plugin reported reaches the run past the old 200-character
    cap: the tail is where "which call, what did the origin say" lived.
    """
    tail = "d" * 300 + "-END-OF-ORIGIN-DETAIL"
    _install_plugin(
        plugin_dir,
        "chatty",
        _raise_in_parse("structure_changed", tail),
        label="chatty",
    )

    async with _client(served) as client:
        assert (
            await client.post("/v1/plugins/chatty:import", headers=_key())
        ).status_code == 200
        source_id = await _add_source(client, origin.url)
        await client.post(f"/v1/sources/{source_id}:trigger", headers=_key())

        with session_factory() as session:
            metrics = session.execute(
                sa.text("select metrics from job_run")
            ).scalar_one()
        assert tail in metrics, metrics[-120:]
