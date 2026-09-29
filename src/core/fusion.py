"""Combining ranked lists by rank, not by score (ADR 0035 decision 7).

Reciprocal Rank Fusion sums `1 / (k + rank)` over the lists a document appears
in. Its whole point is that it never looks at a score: a bm25 value and a cosine
are not comparable, and any attempt to combine them needs a weight nobody has
data to choose. Ranks are comparable by construction, so the fusion is
parameter-free apart from `k`, which damps the influence of the top positions
(60 is the value from the original paper and from every implementation since).

Pure, and deliberately so: this is a rule about ordering, not about items, and
the two ranked lists it consumes come from an index and from a file store.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

# The constant from Cormack et al., and the one every reference implementation
# uses. It exists so that rank 1 does not dominate: with k=60 the first and tenth
# places are worth 1/61 and 1/70, which is the behaviour the paper measured as
# robust across collections.
DEFAULT_K: Final = 60


def reciprocal_rank_fusion(
    rank_lists: Sequence[Sequence[str]], *, k: int = DEFAULT_K
) -> list[tuple[str, float]]:
    """Fuse ranked id lists into one, highest score first.

    Ties break by id, and the order is therefore total: two runs over the same
    lists produce the same sequence, which is what lets a page cursor be a pair
    of values rather than an offset into a shifting window (the same reason the
    time-ordered list is a keyset).

    A document missing from a list simply contributes nothing from it -- there is
    no imputation and no penalty, because "absent from the lexical results" is
    exactly what a semantic-only match looks like (AC-105).
    """
    scores: dict[str, float] = {}
    for ranks in rank_lists:
        for position, item_id in enumerate(ranks, start=1):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (k + position)
    return sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))
