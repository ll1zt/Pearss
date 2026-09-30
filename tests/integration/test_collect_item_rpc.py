"""The item capability over the sandbox: a real child process, no mocks.

`collect_item` is the one hook whose caller hands the collector an *address*
rather than the source URL, so its RPC shape is part of the contract: the
address and the source id must cross the process boundary, and drafts must
come back as drafts (issue #168).

A fabricated plugin is the only implementer available until rss declares the
capability (#169) — and an *installed* plugin is the only path that reaches
this code over RPC at all, because the shipped rss collector is called
in-process. Spawning a real child is the point: it is the one way to find out
whether the method name, the params and the draft encoding agree on both
sides of the boundary.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from app import plugin_loader
from app.plugin_runtime import PluginChild, SandboxedCollector

pytestmark = pytest.mark.integration

_ID = "item-plugin"

# A plugin in the shape ADR 0023 requires: `collector.py` beside a
# `manifest.json`, declaring the capability it implements. Every collector
# must provide collector_id / match_url / parse / source_config, so a fixture
# that omitted them would fail for the wrong reason.
_COLLECTOR = '''\
"""A fabricated item-address plugin (test fixture, not a shipped collector)."""

from core.models import ItemDraft


def collector_id() -> str:
    return "{id}"


def match_url(url: str) -> bool:
    return "item-plugin.example" in url


def parse(raw):
    return []


def source_config():
    return {{
        "id": "{id}",
        "version": "0.1.0",
        "name": "Item Plugin",
        "description": "fixture",
        "example_url": "https://item-plugin.example/feed",
        "features": ["item"],
        "required_config": [],
    }}


def collect_item(*, source_id: str, url: str, client):
    return [
        ItemDraft(
            source_id=source_id,
            external_id="item-plugin:1",
            title="Fetched by address",
            canonical_url=url,
            published_at=None,
            content="<p>the article</p>",
        )
    ]
'''


@pytest.fixture
def item_child(tmp_path: Path) -> Iterator[PluginChild]:
    directory = tmp_path / _ID
    directory.mkdir()
    (directory / "collector.py").write_text(_COLLECTOR.format(id=_ID), encoding="utf-8")
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "id": _ID,
                "version": "0.1.0",
                "name": "Item Plugin",
                "description": "fixture",
                "example_url": "https://item-plugin.example/feed",
                "features": ["item"],
                "required_config": [],
            }
        ),
        encoding="utf-8",
    )

    child = PluginChild(_ID, pythonpath=plugin_loader.child_pythonpath(directory))
    child.start()
    yield child
    child.stop()


def test_the_address_crosses_the_boundary_and_the_drafts_come_back(
    item_child: PluginChild,
) -> None:
    with httpx.Client() as client:
        drafts = SandboxedCollector(item_child).collect_item(
            source_id="s1",
            url="https://item-plugin.example/archives/7",
            client=client,
        )

    assert [draft.external_id for draft in drafts] == ["item-plugin:1"]
    assert drafts[0].canonical_url == "https://item-plugin.example/archives/7"
    assert drafts[0].content == "<p>the article</p>"
    # The source id is the draft's provenance and is not the caller's to pick.
    assert drafts[0].source_id == "s1"


def test_an_installed_plugin_that_declares_item_verifies(
    item_child: PluginChild,
) -> None:
    """The install-time contract check agrees with the code (ADR 0023).

    Declared-and-implemented has to pass this check, or the capability would
    be installable but unusable -- the case the check exists to catch.
    """
    impls = item_child.call("verify", {})

    assert impls["has_collect_item"] is True
    assert impls["declared_features"] == ["item"]
