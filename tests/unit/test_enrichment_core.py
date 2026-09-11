"""Unit tests for the processor-chain completion rule.

`chain_complete` is the publish gate's brain for chained sources: an item
stays pending until every *required* product exists in a terminal state.
The optional-spec branch (`required: false`) had no test at any layer -- a
regression that started blocking on optional products would deadlock every
chained source, silently.
"""

from __future__ import annotations

import pytest

from core.enrichment import (
    EnrichmentKind,
    EnrichmentSpec,
    EnrichmentStatus,
    chain_complete,
)

pytestmark = pytest.mark.unit


def _spec(plugin: str, *, required: bool = True) -> EnrichmentSpec:
    return EnrichmentSpec(
        plugin_id=plugin, kind=EnrichmentKind.TRANSLATION, required=required
    )


def _chain(*plugins: str, required: bool = True) -> tuple[EnrichmentSpec, ...]:
    return tuple(_spec(plugin, required=required) for plugin in plugins)


def _statuses(**by_plugin: str) -> dict[tuple[str, str, str | None], EnrichmentStatus]:
    """Build a statuses map keyed the way the gate looks products up."""
    return {
        (plugin, EnrichmentKind.TRANSLATION.value, None): EnrichmentStatus(status)
        for plugin, status in by_plugin.items()
    }


def test_an_empty_chain_is_complete() -> None:
    assert chain_complete((), {}) is True


def test_every_required_product_succeeded() -> None:
    assert chain_complete(_chain("translate"), _statuses(translate="succeeded")) is True


def test_a_required_product_still_queued_holds_the_gate() -> None:
    assert chain_complete(_chain("translate"), _statuses(translate="queued")) is False


def test_a_required_product_running_holds_the_gate() -> None:
    assert chain_complete(_chain("translate"), _statuses(translate="running")) is False


def test_a_required_product_failed_holds_the_gate() -> None:
    """A failed product is not a pass: the operator retries or drops it."""
    assert chain_complete(_chain("translate"), _statuses(translate="failed")) is False


def test_a_missing_product_holds_the_gate() -> None:
    """An absent entry is unfinished: an empty map never opens the gate."""
    assert chain_complete(_chain("translate"), {}) is False


def test_an_optional_product_is_ignored_whatever_its_state() -> None:
    """Optional products may fail without holding the gate.

    Every status must be ignored, not just the happy one: a regression that
    blocked on a failed optional product would deadlock chains configured
    with best-effort processors.
    """
    for status in EnrichmentStatus:
        assert chain_complete(_chain("x", required=False), _statuses(x=status)) is True


def test_an_optional_product_missing_entirely_is_fine() -> None:
    assert chain_complete(_chain("translate", required=False), {}) is True


def test_a_required_failure_does_not_mask_an_optional_success() -> None:
    """The gate reads required specs only, in both directions."""
    chain = (*_chain("optional-one", required=False), *_chain("required-one"))
    statuses = _statuses(**{"optional-one": "succeeded", "required-one": "failed"})

    assert chain_complete(chain, statuses) is False


def test_two_required_products_both_must_finish() -> None:
    chain = _chain("translate", "summarise")
    both_done = _statuses(translate="succeeded", summarise="succeeded")
    one_missing = _statuses(translate="succeeded")

    assert chain_complete(chain, both_done) is True
    assert chain_complete(chain, one_missing) is False


def test_the_gate_distinguishes_products_by_language() -> None:
    """A product for another target language is not this spec's product."""
    chain = (_spec("translate"),)
    other_lang: dict[tuple[str, str, str | None], EnrichmentStatus] = {
        ("translate", EnrichmentKind.TRANSLATION.value, "fr"): (
            EnrichmentStatus.SUCCEEDED
        )
    }

    assert chain_complete(chain, other_lang) is False
