"""The item capability over the sandbox: a real child process, no mocks.

`collect_item` is the one hook whose caller hands the collector an *address*
rather than the source URL, so its RPC shape is part of the contract: the
address and the source id must cross the process boundary, the drafts must
come back as drafts, and a failure must keep the class the child assigned it
(issue #168).

A fabricated plugin is the only implementer available until rss declares the
capability (#169) — and an *installed* plugin is the only path that reaches
this code over RPC at all, because the shipped rss collector is called
in-process.

Two of these tests call the child's dispatch function **in this process**. That
is deliberate and is the phase-1 lesson: the parent's coverage does not observe
another process, so a branch that only ever runs in the child reads as
uncovered and the changed-line gate fails (the same gap `walk.py` carries an
in-process test for). The real child runs the same code above; only the
boundary is missing from these two.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from app import plugin_loader
from app.plugin_runtime import PluginChild, SandboxedCollector
from core.enums import ErrorClass

pytestmark = pytest.mark.integration

_ID = "item-plugin"

# A plugin in the shape ADR 0023 requires: `collector.py` beside a
# `manifest.json`, declaring the capability it implements. Every collector
# must provide collector_id / match_url / parse / source_config, so a fixture
# that omitted them would fail for the wrong reason.
#
# The failure branches are driven by the address rather than by a second
# fixture: one plugin module, and the URL decides which of the three answers
# comes back. A `FetchError` is the shape the hookspec allows a collecting
# implementation to raise; `CollectorError` is the shape a parse failure
# takes; anything else is a plugin bug the host must still survive.
_COLLECTOR = '''\
"""A fabricated item-address plugin (test fixture, not a shipped collector)."""

from adapters.http import FetchError
from core.enums import ErrorClass
from core.models import ItemDraft
from core.plugins.hooks import CollectorError


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
    if url.endswith("/fetch-error"):
        raise FetchError(ErrorClass.BLOCKED, "the origin refused the address")
    if url.endswith("/classified"):
        raise CollectorError(ErrorClass.NETWORK, "the origin is unreachable")
    if url.endswith("/unclassified"):
        raise ValueError("a plugin bug, not a classified failure")
    return [
        ItemDraft(
            origin_id=source_id,
            external_id="item-plugin:1",
            title="Fetched by address",
            canonical_url=url,
            published_at=None,
            content="<p>the article</p>",
        )
    ]
'''


def _write_plugin(root: Path) -> Path:
    """Write the fabricated plugin (code + manifest) and return its directory."""
    directory = root / _ID
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
    return directory


@pytest.fixture
def item_child(tmp_path: Path) -> Iterator[PluginChild]:
    directory = _write_plugin(tmp_path)
    child = PluginChild(_ID, pythonpath=plugin_loader.child_pythonpath(directory))
    child.start()
    yield child
    child.stop()


# --------------------------------------------------------------------------
# Over the real boundary
# --------------------------------------------------------------------------


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
    assert drafts[0].origin_id == "s1"


def test_a_classified_failure_keeps_its_class_across_the_boundary(
    item_child: PluginChild,
) -> None:
    """The child's class is the host's class: a real 404 must not become `parse`.

    Issue #2's lesson at this seam. The child knows which of the five classes
    fired; relabelling it here would hide `structure_changed` (or `auth`, or
    `blocked`) behind a generic parse failure in every run record.
    """
    from core.plugins.hooks import CollectorError

    with httpx.Client() as client, pytest.raises(CollectorError) as excinfo:
        SandboxedCollector(item_child).collect_item(
            source_id="s1",
            url="https://item-plugin.example/classified",
            client=client,
        )

    assert excinfo.value.error_class is ErrorClass.NETWORK


def test_an_unclassified_plugin_failure_is_a_parse_failure(
    item_child: PluginChild,
) -> None:
    """A plugin that raises anything is a *source* failure, not a broken host."""
    from core.plugins.hooks import CollectorError

    with httpx.Client() as client, pytest.raises(CollectorError) as excinfo:
        SandboxedCollector(item_child).collect_item(
            source_id="s1",
            url="https://item-plugin.example/unclassified",
            client=client,
        )

    assert excinfo.value.error_class is ErrorClass.PARSE


def test_an_installed_plugin_that_declares_item_verifies(
    item_child: PluginChild,
) -> None:
    """The install-time contract check agrees with the code (ADR 0023).

    Declared-and-implemented has to pass this check, or the capability would be
    installable but unusable -- the case the check exists to catch.
    """
    impls = item_child.call("verify", {})

    assert impls["has_collect_item"] is True
    assert impls["declared_features"] == ["item"]


# --------------------------------------------------------------------------
# The same dispatch, in this process (so the changed-line gate can see it)
# --------------------------------------------------------------------------


def _dispatch_in_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str
) -> object:
    """Call the child's `handle` in this process against the same plugin.

    The plugin directory goes on `sys.path` and the child's module cache is
    cleared either side, because `handle` resolves an installed plugin by
    importing the module named `collector`.
    """
    from plugins.collectors import __main__ as child_main

    directory = _write_plugin(tmp_path)
    monkeypatch.syspath_prepend(str(directory))
    monkeypatch.delitem(sys.modules, "collector", raising=False)
    child_main._COLLECTORS.pop(_ID, None)
    try:
        return child_main.handle(
            _ID, "collect_item", {"source_id": "s1", "url": url}, None
        )
    finally:
        sys.modules.pop("collector", None)
        child_main._COLLECTORS.pop(_ID, None)


def test_the_child_dispatch_serves_collect_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drafts = _dispatch_in_process(
        tmp_path, monkeypatch, "https://item-plugin.example/archives/7"
    )

    assert isinstance(drafts, list)
    assert drafts[0]["external_id"] == "item-plugin:1"
    assert drafts[0]["canonical_url"] == "https://item-plugin.example/archives/7"


def test_the_child_dispatch_converts_a_fetch_failure_to_a_classified_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A FetchError from the collector reaches the host as CollectorError.

    `serve()` turns only `CollectorError` into a classified error object, so
    without this conversion a fetch failure inside `collect_item` would escape
    as a generic child fault and lose its class.
    """
    from core.plugins.hooks import CollectorError

    with pytest.raises(CollectorError) as excinfo:
        _dispatch_in_process(
            tmp_path, monkeypatch, "https://item-plugin.example/fetch-error"
        )

    assert excinfo.value.error_class is ErrorClass.BLOCKED
