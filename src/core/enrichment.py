"""The processor-chain domain: what a pipeline of enrichments means.

Processing is the P3 contract between collectors and publishers: an Item's
content may need derived artifacts (translation, summary, TTS audio) before
the publish gate opens. The rules live here as pure domain logic:

- A *chain* is an ordered list of enrichment specs; the gate opens only when
  every required spec has a successful product.
- Products never rewrite the Item (docs/domain/data-model.md): each lives in
  `item_enrichment`, pinned to the content version it was produced from
  (`input_hash`), so a content change invalidates the product rather than
  mutating stored history.
- `chain_complete` is the pure function the publish gate consults through
  `TransitionContext.processing_complete` — a failed or missing enrichment
  keeps the gate shut, which is the entire point of the gate.

No I/O here: the worker and the repository execute what this module decides.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class EnrichmentKind(StrEnum):
    """The product kinds the pipeline knows about."""

    TRANSLATION = "translation"
    SUMMARY = "summary"
    TTS_AUDIO = "tts_audio"
    # The article body of a summary-only feed item, fetched from the item
    # URL by the fulltext processor (issue #28). A product like any other:
    # pinned to input_hash, never rewriting the item.
    FULLTEXT = "fulltext"


class EnrichmentStatus(StrEnum):
    """Lifecycle of one enrichment row."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


# Which product kinds may be *consumed as input* by a later spec (ADR 0030).
#
# A chain is a pipeline, so a later spec reads the earlier spec's product
# rather than the Item's own body -- otherwise `[fulltext, translate]`
# translates the feed's summary while the full text sits beside it (#34).
#
# A text product is consumable; a non-text one (tts_audio) is not a text
# input to anything, and a translation is deliberately excluded so a chain of
# translations cannot feed itself -- the prose is already in the target
# language at that point, so re-consuming it would both loop and be a no-op.
CONSUMABLE_KINDS: Final[frozenset[EnrichmentKind]] = frozenset(
    {
        EnrichmentKind.FULLTEXT,
        EnrichmentKind.SUMMARY,
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class EnrichmentSpec:
    """One required product: a kind, the processor, its output language.

    `lang` is the target language for language products (the source's
    `target_lang` decision); None for non-language artifacts such as TTS
    audio. `required` is what makes a chain blocking: an optional spec may
    fail without holding the gate, a required one cannot.
    """

    plugin_id: str
    kind: EnrichmentKind
    lang: str | None = None
    required: bool = True


def chain_complete(
    chain: tuple[EnrichmentSpec, ...],
    statuses: dict[tuple[str, str, str | None], EnrichmentStatus],
) -> bool:
    """Whether every required spec in `chain` has a successful product.

    `statuses` is keyed by (plugin_id, kind, lang) — the identity a product
    is looked up by. A spec absent from the map is unfinished, so an empty
    map means "nothing has run yet" and the gate stays closed for any
    non-empty chain. An empty chain is trivially complete (P1 behaviour:
    no chain, nothing to wait for).
    """
    for spec in chain:
        if not spec.required:
            continue
        status = statuses.get((spec.plugin_id, spec.kind.value, spec.lang))
        if status is not EnrichmentStatus.SUCCEEDED:
            return False
    return True


def upstream_for(
    chain: tuple[EnrichmentSpec, ...], spec: EnrichmentSpec
) -> EnrichmentSpec | None:
    """The spec whose product must feed `spec`'s input, or None (ADR 0030).

    The *nearest preceding* consumable spec: `[fulltext, summary, translate]`
    has translate consume the summary, and `[fulltext, translate]` has
    translate consume the full text. Consumption is defined by chain position
    rather than by kind pairing, so adding a spec between two others changes
    the pipeline in the obvious way instead of needing a mapping table.

    None means "the Item's own content", which is the behavior for a first
    spec and for any spec whose predecessors are not text producers -- i.e.
    exactly the single-spec chains that predate this rule.
    """
    upstream: EnrichmentSpec | None = None
    for candidate in chain:
        if candidate == spec:
            return upstream
        if candidate.kind in CONSUMABLE_KINDS:
            upstream = candidate
    return None
