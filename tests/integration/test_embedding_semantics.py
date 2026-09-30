"""The real model is multilingual, and the prefixes are the ones it expects.

Scenario register (normative source: ADR 0035 §9; issue #82).

These need the actual weights (~135 MB, fetched by `just fetch-model`) and skip
without them, so the skip is visible instead of the suite quietly proving
nothing. CI fetches the model before the gates for exactly this file.

They exist because the model's semantics are not a property of any code here: an
encoder that pools, masks and normalises correctly can still be attached to a
model that is not multilingual, and the only symptom is retrieval that ranks by
nothing at all. ADR 0035 decision 5 requires the cross-lingual check before the
capability counts as done, and this is where that check lives.

Assertions are orderings, not thresholds. What has to hold is that a paraphrase
scores above an unrelated text and that a translation scores above an unrelated
one; the absolute numbers move with the quantisation and with the text, so
pinning them would pin the fixture rather than the property.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from plugins.processors.embed.encoder import Encoder
from tests import model_weights

pytestmark = pytest.mark.integration

DIMENSIONS = 384


def _encoder_or_skip() -> Encoder:
    directory = model_weights.readable_weights()
    if directory is None:
        pytest.skip(
            f"no readable embedding model at {model_weights.configured_path()}; "
            "run `just fetch-model`"
        )
    return Encoder(directory)


def _cosine(left: list[float], right: list[float]) -> float:
    """Both vectors are L2-normalised, so the dot product is the cosine."""
    return sum(a * b for a, b in zip(left, right, strict=True))


def test_the_real_model_produces_unit_vectors_of_the_declared_width() -> None:
    """AC-117: the contract the fixture pins, now against the real weights."""
    vector = _encoder_or_skip().encode("内核调度器如何工作")

    assert len(vector) == DIMENSIONS
    assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0, rel_tol=1e-5)


def test_a_paraphrase_is_closer_than_an_unrelated_text() -> None:
    """Semantic, not lexical: the two share almost no words."""
    encoder = _encoder_or_skip()
    query = encoder.encode("操作系统进程调度", as_query=True)
    paraphrase = encoder.encode("Linux 内核的调度器是怎么工作的")
    unrelated = encoder.encode("红烧肉的家常做法")

    assert _cosine(query, paraphrase) > _cosine(query, unrelated), (
        _cosine(query, paraphrase),
        _cosine(query, unrelated),
    )


def test_a_chinese_query_is_closest_to_the_same_meaning_in_japanese() -> None:
    """AC-100's substance: the model is multilingual, which nothing else checks.

    A model that is not multilingual passes every other test in this repository
    -- it encodes, pools and stores perfectly -- and fails only here, as
    retrieval that returns nothing for a language other than the one the corpus
    happens to be in. The corpus is mixed (ADR 0035 decision 5), so this is the
    check that the choice of model was right.
    """
    encoder = _encoder_or_skip()
    query = encoder.encode("操作系统进程调度", as_query=True)
    japanese = encoder.encode("オペレーティングシステムのプロセススケジューリング")
    english = encoder.encode("Operating system process scheduling")
    unrelated = encoder.encode("红烧肉的家常做法")

    assert _cosine(query, japanese) > _cosine(query, unrelated), (
        _cosine(query, japanese),
        _cosine(query, unrelated),
    )
    assert _cosine(query, english) > _cosine(query, unrelated), (
        _cosine(query, english),
        _cosine(query, unrelated),
    )


def test_the_query_and_passage_prefixes_are_actually_applied() -> None:
    """e5 is trained with distinct prefixes; using one for both degrades ranking.

    The failure mode is invisible -- vectors still come out, still normalised --
    so the difference is asserted rather than assumed. The manifest carries the
    prefixes, which is why they are not a caller's choice.
    """
    encoder = _encoder_or_skip()
    text = "linux scheduler"

    assert encoder.encode(text, as_query=True) != encoder.encode(text)


def test_an_unreadable_model_directory_skips_instead_of_failing(
    unreadable_model_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-179: "cannot look" is not a failure of the suite.

    The weights exist, owned by another account -- the shape a multi-user host
    has -- and the cases that need them must skip with a reason that says how to
    get them, which is what this file's docstring promises. Before the shared
    check, `Path.is_file()` raised here and the four cases above failed.
    """
    monkeypatch.setenv("PEARSS_EMBEDDING_MODEL_DIR", str(unreadable_model_dir))

    with pytest.raises(pytest.skip.Exception) as excinfo:
        _encoder_or_skip()

    assert "just fetch-model" in str(excinfo.value)
