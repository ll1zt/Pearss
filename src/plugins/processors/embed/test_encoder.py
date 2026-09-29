"""Text becomes one unit vector of the declared width, or nothing does.

Scenario register (normative source: ADR 0035 §9; issue #82).

These run against the committed fixture, which is shaped like the real export
(ids and a mask in, one hidden state per token out) rather than like the task.
That is deliberate: a fixture that pre-pooled would leave the pooling, the mask
and the normalisation -- the parts this module actually implements -- untested.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from adapters.embedding_model import ModelManifest
from core.enrichment import ModelIdentity
from core.enums import ErrorClass
from core.plugins.hooks import ProcessorError
from plugins.processors.embed.encoder import Encoder

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_a_passage_becomes_a_unit_vector_of_the_declared_width() -> None:
    """AC: the width is the manifest's, and the norm is one.

    The norm is asserted rather than assumed because the dot product downstream
    is only a cosine if it holds; a vector that is merely "384 floats" would
    still rank, just by magnitude instead of by meaning.
    """
    vector = Encoder(FIXTURES).encode("内核调度器如何工作")

    assert len(vector) == 384
    assert all(isinstance(value, float) for value in vector)
    assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0, rel_tol=1e-6)


def test_the_same_text_twice_gives_the_same_vector() -> None:
    """Determinism is what makes the product reusable rather than merely stored."""
    encoder = Encoder(FIXTURES)
    assert encoder.encode("linux scheduler") == encoder.encode("linux scheduler")


def test_a_batch_gives_a_text_the_vector_it_would_get_alone() -> None:
    """Padding is masked, so a longer neighbour cannot change a text's vector.

    Without the mask this is the kind of bug that never shows up in a test that
    only encodes one text at a time, and shows up in production as ranking that
    depends on which items happened to be swept together.
    """
    encoder = Encoder(FIXTURES)
    short = "linux"
    long = "linux scheduler the a vector of text 内核调度器如何工作"

    assert encoder.encode_batch([short, long])[0] == encoder.encode(short)


def test_an_empty_text_still_produces_a_vector() -> None:
    """AC-103: a short or empty item is a fact about the item, not a crash."""
    vector = Encoder(FIXTURES).encode("")

    assert len(vector) == 384
    assert any(value != 0.0 for value in vector), "an empty text is not a zero vector"


def test_a_missing_model_file_is_a_structure_error(tmp_path: Path) -> None:
    """AC-104: what is wrong is the directory, and the class has to say so."""
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "name": "m",
                "dimensions": 384,
                "revision": "r",
                "model_file": "absent.onnx",
                "tokenizer_file": "absent.json",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ProcessorError) as failure:
        Encoder(tmp_path).encode("linux")

    assert failure.value.error_class is ErrorClass.STRUCTURE_CHANGED
    assert "absent.onnx" in failure.value.detail


def test_a_directory_without_a_manifest_is_a_structure_error(tmp_path: Path) -> None:
    with pytest.raises(ProcessorError) as failure:
        Encoder(tmp_path).encode("linux")

    assert failure.value.error_class is ErrorClass.STRUCTURE_CHANGED
    assert "no manifest" in failure.value.detail


def test_a_width_that_disagrees_with_the_manifest_is_refused() -> None:
    """The mismatch ADR 0035 decision 6 exists to prevent, caught at the shape.

    This is the last point where it is still visible: one layer down it is a dot
    product that quietly returns a number.
    """
    manifest = ModelManifest(
        name="declared-eight",
        dimensions=8,
        revision="r",
        model_file="tiny-encoder.onnx",
        tokenizer_file="tiny-tokenizer.json",
    )

    with pytest.raises(ProcessorError) as failure:
        Encoder(FIXTURES, manifest=manifest).encode("linux")

    assert failure.value.error_class is ErrorClass.STRUCTURE_CHANGED
    assert "384" in failure.value.detail, failure.value.detail
    assert "8" in failure.value.detail, failure.value.detail


def test_the_identity_is_what_the_manifest_declares() -> None:
    """The triple a product is keyed by comes from the directory, not a caller.

    Asserted as an exact value rather than a shape: this identity is folded into
    `input_hash`, so a change here silently re-keys every stored vector (issue
    #82; a change needs a data migration, not a test update).
    """
    encoder = Encoder(FIXTURES)

    assert encoder.identity == ModelIdentity(
        name="pearss-test-fixture", dimensions=384, revision="tiny-1"
    )
    assert encoder.dimensions == 384


def test_a_manifest_that_is_not_json_is_a_structure_error(tmp_path: Path) -> None:
    (tmp_path / "manifest.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(ProcessorError) as failure:
        Encoder(tmp_path).encode("linux")

    assert failure.value.error_class is ErrorClass.STRUCTURE_CHANGED
    assert "unreadable manifest" in failure.value.detail


def test_a_manifest_missing_its_identity_is_a_structure_error(tmp_path: Path) -> None:
    """A manifest without a name cannot key anything, so it is refused up front."""
    (tmp_path / "manifest.json").write_text(
        json.dumps({"dimensions": 384, "revision": "r"}), encoding="utf-8"
    )

    with pytest.raises(ProcessorError) as failure:
        Encoder(tmp_path).encode("linux")

    assert failure.value.error_class is ErrorClass.STRUCTURE_CHANGED
    assert "incomplete manifest" in failure.value.detail


def test_a_model_file_that_is_not_a_model_is_a_structure_error(tmp_path: Path) -> None:
    """AC-104 for the load itself: a foreign file is classified, not raised raw.

    The tokenizer is a real file that is not an ONNX model, which is the shape a
    truncated download or a half-copied model directory has. The class matters
    because the worker's retry policy and the operator's run record read
    `error_class`, and an unclassified exception reaches neither.
    """
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "name": "not-a-model",
                "dimensions": 384,
                "revision": "r",
                "model_file": "tiny-tokenizer.json",
                "tokenizer_file": "tiny-tokenizer.json",
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "tiny-tokenizer.json").write_bytes(
        (FIXTURES / "tiny-tokenizer.json").read_bytes()
    )

    with pytest.raises(ProcessorError) as failure:
        Encoder(tmp_path).encode("linux")

    assert failure.value.error_class is ErrorClass.STRUCTURE_CHANGED
    assert "could not be loaded as an ONNX model" in failure.value.detail


def test_encoding_no_texts_is_no_vectors() -> None:
    """A batch of nothing is not an error, and it must not load the model either."""
    assert Encoder(FIXTURES).encode_batch([]) == []
