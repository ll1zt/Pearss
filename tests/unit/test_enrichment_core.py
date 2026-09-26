"""Unit tests for the processor-chain completion rule.

`chain_complete` is the publish gate's brain for chained sources: an item
stays pending until every *required* product exists in a terminal state.
The optional-spec branch (`required: false`) had no test at any layer -- a
regression that started blocking on optional products would deadlock every
chained source, silently.

`upstream_for` is the chain's dataflow rule (ADR 0030): which earlier product
a later spec's input is. It is pure, so its whole truth table is pinned here;
the integration suite proves the worker and the reader obey it.
"""

from __future__ import annotations

import pytest

from core.enrichment import (
    EnrichmentKind,
    EnrichmentSpec,
    EnrichmentStatus,
    chain_complete,
    upstream_for,
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


# --------------------------------------------------------------------------
# ADR 0030: which product a later spec consumes
#
# Pure domain rule, so it is pinned here rather than only through the worker.
# The chain tests in tests/integration/test_chain_dataflow.py prove the rule is
# *applied*; these prove the rule itself, including the cases a chain test
# cannot easily arrange (a tts_audio spec, an empty chain).
# --------------------------------------------------------------------------


def _kinds(*specs: tuple[str, EnrichmentKind]) -> tuple[EnrichmentSpec, ...]:
    return tuple(EnrichmentSpec(plugin_id=plugin, kind=kind) for plugin, kind in specs)


def test_a_later_spec_consumes_the_nearest_text_product() -> None:
    chain = _kinds(
        ("fulltext", EnrichmentKind.FULLTEXT),
        ("translate", EnrichmentKind.TRANSLATION),
    )
    fulltext, translate = chain

    assert upstream_for(chain, translate) is fulltext


def test_position_decides_not_kind_pairing() -> None:
    """A summary between a fulltext and a translation is what gets translated.

    Position rather than a kind-to-kind mapping table: adding a spec in the
    middle must change the pipeline in the obvious way, and the mapping
    approach would silently keep sending the full text.
    """
    chain = _kinds(
        ("fulltext", EnrichmentKind.FULLTEXT),
        ("summary", EnrichmentKind.SUMMARY),
        ("translate", EnrichmentKind.TRANSLATION),
    )
    _fulltext, summary, translate = chain

    assert upstream_for(chain, translate) is summary


def test_the_first_spec_has_no_upstream() -> None:
    chain = _kinds(
        ("fulltext", EnrichmentKind.FULLTEXT),
        ("translate", EnrichmentKind.TRANSLATION),
    )

    assert upstream_for(chain, chain[0]) is None


def test_a_translation_is_not_consumable() -> None:
    """A translation must not be another spec's input.

    Two reasons, both load-bearing: the prose it holds is already in the target
    language, so re-consuming it would be a no-op that costs a provider call,
    and a chain of translations would otherwise be able to feed itself.
    """
    chain = _kinds(
        ("translate", EnrichmentKind.TRANSLATION),
        ("translate2", EnrichmentKind.TRANSLATION),
    )
    _first, second = chain

    assert upstream_for(chain, second) is None


def test_a_non_text_product_is_not_consumable() -> None:
    """`tts_audio` is an artifact, not a text input to anything after it."""
    chain = _kinds(
        ("tts", EnrichmentKind.TTS_AUDIO),
        ("translate", EnrichmentKind.TRANSLATION),
    )
    _tts, translate = chain

    assert upstream_for(chain, translate) is None


def test_a_single_spec_chain_is_unchanged() -> None:
    """The overwhelmingly common shape: `[translate]` alone."""
    chain = _kinds(
        ("translate", EnrichmentKind.TRANSLATION),
    )

    assert upstream_for(chain, chain[0]) is None


def test_an_unknown_spec_is_not_in_the_chain() -> None:
    """A spec that is not in the chain has no position, so no upstream.

    Guards the worker's lookup: it matches a row's (plugin, kind, lang) against
    the configured chain, and a row left over from a chain edit must fall back
    to the item's own content rather than to an arbitrary neighbour.
    """
    chain = _kinds(
        ("fulltext", EnrichmentKind.FULLTEXT),
        ("translate", EnrichmentKind.TRANSLATION),
    )
    stranger = EnrichmentSpec(plugin_id="gone", kind=EnrichmentKind.TRANSLATION)

    assert upstream_for(chain, stranger) is None
