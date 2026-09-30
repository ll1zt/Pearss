"""The item capability: an address in, drafts out — refused unless declared.

Scenario register (normative source: `SCENARIOS.md`, AC-168). The capability
lands one pull request ahead of its first implementer (rss, #169) on purpose:
the refusal half is what keeps an unsupported source from turning a caller's
address list into requests against an origin that never agreed to them, and
that half is testable the moment the seam exists.

The three checks here are the contract's three layers: the registry refuses an
undeclared capability before anything runs, the startup pass refuses a
declaration with no implementation behind it (both for a shipped collector and
for an installed plugin), and the frozen RPC protocol knows the method name.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from app import collector_registry, plugin_loader
from core.enums import ErrorClass
from core.models import ItemDraft
from core.plugins import protocol

pytestmark = pytest.mark.unit


def _draft(url: str) -> ItemDraft:
    return ItemDraft(
        source_id="s1",
        external_id="item:1",
        title="Fetched by address",
        canonical_url=url,
        published_at=None,
        content="<p>the article</p>",
    )


def _make_double(
    *, features: frozenset[str], implement_item: bool, collector_id: str = "double"
) -> Any:
    """A collector double; `implement_item=False` declares without implementing.

    The hook is attached after the class body so the *absent* case is a real
    absence: `getattr(executor, "collect_item", None)` is what the registry
    reads, and a class that defines it cannot express "declared but not
    implemented". `collector_id` names the slot the double stands in for --
    the registry checks a manifest's id against it, so an override for rss has
    to answer to "rss" or it fails before reaching the check under test.
    """

    class _Double:
        def collector_id(self) -> str:
            return collector_id

        def match_url(self, url: str) -> bool:
            return False

        def parse(self, raw: Any) -> list[ItemDraft]:
            raise AssertionError("parse is not part of this scenario")

        def source_config(self) -> dict[str, Any]:
            return {
                "id": collector_id,
                "version": "0.1.0",
                "name": "Double",
                "display_title": None,
                "description": "double",
                "example_url": "https://example.test/feed",
                "features": sorted(features),
                "required_config": [],
            }

    if implement_item:

        def collect_item(
            _self: Any, *, source_id: str, url: str, client: Any
        ) -> list[ItemDraft]:
            return [_draft(url)]

        _Double.collect_item = collect_item  # type: ignore[attr-defined]
    return _Double()


def _registration(*, item: bool, collector: Any) -> Any:
    return collector_registry.Registration(
        name="double",
        collector=collector,
        item=item,
        manifest={"id": "double", "features": ["item"] if item else []},
        checksum="x",
    )


# --------------------------------------------------------------------------
# AC-168: the gate is in front of the collector, not behind it
# --------------------------------------------------------------------------


def test_ac168_a_source_without_the_item_capability_is_refused_before_it_runs() -> None:
    """AC-168: the double *does* implement the hook, so only the gate can pass.

    A registration whose collector implements `collect_item` while the
    registration says it does not is the interesting case: if the refusal were
    checked late (or not at all) the collector would run and this test would
    see the draft instead of the error.
    """
    collector = _make_double(features=frozenset(), implement_item=True)

    with (
        httpx.Client() as client,
        pytest.raises(collector_registry.CollectorError) as excinfo,
    ):
        collector_registry.collect_item_with(
            _registration(item=False, collector=collector),
            source_id="s1",
            url="https://example.test/archives/7",
            client=client,
        )

    assert excinfo.value.error_class is ErrorClass.STRUCTURE_CHANGED
    assert "item" in excinfo.value.detail


def test_the_capability_delegates_to_the_collector_that_declares_it() -> None:
    """The declared half: the address reaches the collector and drafts come back."""
    collector = _make_double(features=frozenset({"item"}), implement_item=True)

    with httpx.Client() as client:
        drafts = collector_registry.collect_item_with(
            _registration(item=True, collector=collector),
            source_id="s1",
            url="https://example.test/archives/7",
            client=client,
        )

    assert [draft.external_id for draft in drafts] == ["item:1"]
    assert drafts[0].canonical_url == "https://example.test/archives/7"
    assert drafts[0].content == "<p>the article</p>"


# --------------------------------------------------------------------------
# The declaration must have an implementation behind it (ADR 0017's rule)
# --------------------------------------------------------------------------


def test_a_collector_declaring_item_without_implementing_it_fails_at_startup() -> None:
    """A declared capability with no hookimpl is a startup failure, not a gap.

    Checked against a registry built with the rss slot overridden: that is the
    same verification pass the shipped set goes through at startup, and the
    failure has to happen there rather than at the first collection that needs
    the missing half.
    """
    registry = collector_registry.CollectorRegistry()
    collector = _make_double(
        features=frozenset({"item"}), implement_item=False, collector_id="rss"
    )

    with pytest.raises(RuntimeError) as excinfo:
        registry._build_registry(overrides={"rss": collector})

    # Asserted on the *reason*, not on the feature name: before the capability
    # exists the same call fails with "declares unknown feature 'item'", and a
    # loose match would call that a pass.
    assert "without implementing it" in str(excinfo.value)


def test_an_installed_plugin_declaring_item_is_refused_without_the_hook() -> None:
    """The same rule on the install-time path, in the plugin author's vocabulary.

    Asserted on "implements no" rather than on the feature name alone: the
    loader also rejects *unknown* feature names, and accepting that message
    would make this test pass for the wrong reason.
    """
    plugin = plugin_loader.PluginOnDisk(
        plugin_id="double",
        directory=Path("."),
        manifest={"features": ["item"]},
        checksum="x",
    )
    impls: dict[str, Any] = {
        "declared_features": ["item"],
        "has_fetch": False,
        "has_parse": True,
        "has_match_url": True,
        "has_source_config": True,
        "has_collect_pages": False,
        "has_backfill": False,
        "has_collect_item": False,
    }

    with pytest.raises(plugin_loader.PluginLoadError) as excinfo:
        plugin_loader.verify_declarations(plugin, impls)

    assert "implements no" in str(excinfo.value)


def test_the_frozen_rpc_protocol_knows_the_item_method() -> None:
    """The method name is part of the frozen protocol, not a local convention.

    An unknown method is a protocol violation on both sides, so a host that
    sends `collect_item` without this name in the set gets a transport
    failure instead of an answer.
    """
    assert "collect_item" in protocol.METHODS
