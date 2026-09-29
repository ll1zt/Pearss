"""A chain that names the embedder carries the model identity into the row.

Scenario register (normative source: ADR 0035 §9; issue #82).

This is the half of ADR 0035 decision 6 that lives outside the processor: the row
is keyed when it is *enqueued*, so the identity has to be known then -- before
anything loads a model -- or the row is reusable by a different model and the
processor's careful identity is beside the point.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from adapters.embedding_model import load_manifest, model_directory
from adapters.repository.enrichment import parse_chain
from core.enrichment import EnrichmentKind, ModelIdentity
from core.enums import ErrorClass
from core.plugins.hooks import ProcessorError

pytestmark = pytest.mark.unit

FIXTURES = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "plugins"
    / "processors"
    / "embed"
    / "fixtures"
)

EMBED_CHAIN = json.dumps([{"plugin_id": "embed", "kind": "embedding"}])


@pytest.fixture(autouse=True)
def _model_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PEARSS_EMBEDDING_MODEL_DIR", str(FIXTURES))


def test_an_embedding_spec_carries_the_configured_model_identity() -> None:
    """AC-101's other half: the key is fixed at enqueue time, not at run time."""
    (spec,) = parse_chain(EMBED_CHAIN, target_lang="en")

    assert spec.kind is EnrichmentKind.EMBEDDING
    assert spec.model == ModelIdentity(
        name="pearss-test-fixture", dimensions=384, revision="tiny-1"
    )


def test_the_identity_is_the_manifest_the_processor_will_read() -> None:
    """One description of the model, read by both sides.

    The enqueue path and the processor must not be able to disagree about which
    model a row belongs to; reading the same file is what makes that structural
    rather than a convention.
    """
    (spec,) = parse_chain(EMBED_CHAIN, target_lang="en")

    assert spec.model == load_manifest(model_directory()).identity


def test_a_non_embedding_spec_carries_no_model() -> None:
    """A translation is a function of the content, so the content version keys it."""
    (spec,) = parse_chain(
        json.dumps([{"plugin_id": "translate", "kind": "translation"}]),
        target_lang="fr",
    )

    assert spec.model is None


def test_an_embedding_chain_without_a_model_is_refused_at_configuration() -> None:
    """Refused where the operator can see it, not left to deadlock the gate."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("PEARSS_EMBEDDING_MODEL_DIR", "/nonexistent/models")

        with pytest.raises(ProcessorError) as failure:
            parse_chain(EMBED_CHAIN, target_lang="en")

    assert failure.value.error_class is ErrorClass.STRUCTURE_CHANGED
