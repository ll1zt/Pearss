"""Fusion combines ranks, and a document absent from one list still ranks.

Scenario register (normative source: ADR 0035 §9; issue #83).
"""

from __future__ import annotations

import pytest

from core.fusion import DEFAULT_K, reciprocal_rank_fusion

pytestmark = pytest.mark.unit


def test_a_document_in_both_lists_beats_one_in_a_single_list() -> None:
    """Agreement between the paths is the signal, and it needs no scores to see."""
    fused = reciprocal_rank_fusion([["both", "lexical-only"], ["both", "vector-only"]])

    assert [item for item, _score in fused] == ["both", "lexical-only", "vector-only"]


def test_a_document_only_one_path_found_is_still_a_result() -> None:
    """AC-105's mechanism: no penalty for being absent from the other list.

    This is the property a cascade destroys, and the reason the fusion runs both
    paths in full: a document that shares no word with the query can only be
    found by one of them.
    """
    fused = reciprocal_rank_fusion([["keyword"], ["keyword", "semantic-only"]])

    assert "semantic-only" in [item for item, _score in fused]


def test_the_same_lists_always_give_the_same_order() -> None:
    """Determinism is what a keyset cursor over the fusion depends on."""
    first = reciprocal_rank_fusion([["b", "a"], ["c", "a", "b"]])
    second = reciprocal_rank_fusion([["b", "a"], ["c", "a", "b"]])

    assert first == second


def test_ties_break_by_id_so_the_order_is_total() -> None:
    """Two ids at the same rank in their own lists score identically."""
    fused = reciprocal_rank_fusion([["b"], ["a"]])

    assert [item for item, _score in fused] == ["a", "b"]
    assert fused[0][1] == fused[1][1]


def test_an_empty_list_contributes_nothing_and_is_not_an_error() -> None:
    """A path that matched nothing must not erase the other path's results."""
    assert reciprocal_rank_fusion([[], ["only"]]) == reciprocal_rank_fusion([["only"]])


def test_the_default_k_is_the_published_one() -> None:
    """Pinned as a value: changing it silently changes every ranking."""
    assert DEFAULT_K == 60
