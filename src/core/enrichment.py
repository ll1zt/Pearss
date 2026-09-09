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


class EnrichmentKind(StrEnum):
    """The product kinds the pipeline knows about."""

    TRANSLATION = "translation"
    SUMMARY = "summary"
    TTS_AUDIO = "tts_audio"


class EnrichmentStatus(StrEnum):
    """Lifecycle of one enrichment row."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


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
