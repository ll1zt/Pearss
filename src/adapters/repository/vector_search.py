"""Vector retrieval: the stored products, and the nearest items to a query.

The capability behind ADR 0035 decision 5, and nothing about how it is asked for:
mixing it with the lexical path, and the surfaces that expose either, belong to
the issues that own them (#83).

Two properties are load-bearing rather than stylistic:

- The gate is applied in SQL, through the same `visible_item_rows()` predicate
  every other read uses. A search that fetched first and filtered in Python would
  let a pending row reach memory, where a later change could render it -- the
  exact thing `_shared.visible_item_rows` exists to prevent.
- A payload that cannot be read or decoded is skipped, not fatal, and it says so
  in the log. One unreadable vector must not take down a search (the same
  reasoning as `read_body` returning ""), but a silent skip would turn a broken
  product into "this item is just not relevant", which is the kind of quiet
  wrongness this project keeps finding.
"""

from __future__ import annotations

import base64
import binascii
import heapq
import logging
import math
import struct
from collections.abc import Iterator, Sequence
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from adapters.db_models import Item as ItemRow
from adapters.db_models import ItemEnrichment
from adapters.filestore import read_body
from adapters.repository._shared import visible_item_rows
from core.enrichment import EnrichmentKind, EnrichmentStatus

LOGGER = logging.getLogger(__name__)

# A vector of a different width is skipped rather than compared: the identity
# mechanism (ADR 0035 decision 6) is what should prevent one from existing, so
# seeing one means something upstream went wrong and is worth saying out loud.
_WIDTH_MISMATCH = "embedding for item %s has %d dimensions, the query has %d"

# Two floors, both measured on the real model, and neither is a round number
# anybody chose. Cosine values sit on a scale that depends on how long the texts
# are, which is why the same look-alike pair scores differently at the two ends:
#
#   long passages      identical 1.0000 | light edit 0.9969 | title changed 0.9927
#                      trailing added 0.9951 | paraphrase 0.9787
#                      same topic 0.8988 | unrelated 0.8472
#   short titles       light edit 0.9911 | trailing note 0.9814 | title prefix 0.9786
#                      same topic 0.9336 | unrelated 0.8759 | unrelated 0.8554
#
# Related: the unrelated end tops out at 0.8759 and the related end starts at
# 0.8988, so these two floors bracket the gap between them. Both are measured on
# pairs their own length, because a single threshold picked from one regime would
# sit inside the other's overlap.
RELATED_SIMILARITY: Final = 0.89

# Near duplicate: what the measurement actually says is that a *copy* and a
# *paraphrase* cannot be separated. A title-prefixed copy of a short text scores
# 0.9786 and a paraphrase of a long one scores 0.9787 -- the same value for two
# different things. So this number errs towards missing a copy rather than towards
# calling a paraphrase one: a caller shown "these might be duplicates" acts on it,
# and the action is wrong in the second case far more often than it is missing in
# the first. It is also why nothing here merges, deletes or rewrites: ADR 0021
# leaves deletion to the user precisely because a system cannot be sure.
NEAR_DUPLICATE_SIMILARITY: Final = 0.985


def decode_vector(body: str) -> list[float] | None:
    """The float32 vector a stored payload holds, or None when it is not one.

    The encoding is the processor's (base64 of little-endian float32), and it is
    the only place that knows it -- a second decoder would be a second thing to
    keep in step with it.
    """
    if not body:
        return None
    try:
        packed = base64.b64decode(body, validate=True)
    except (binascii.Error, ValueError):
        LOGGER.warning("an embedding payload is not base64; skipping it")
        return None
    if len(packed) % 4 != 0:
        LOGGER.warning("an embedding payload is not a whole number of float32")
        return None
    return list(struct.unpack(f"<{len(packed) // 4}f", packed))


def embedded_items(
    session: Session, *, tenant_id: str
) -> Iterator[tuple[Any, list[float]]]:
    """Every visible item with a succeeded embedding, and its vector.

    Succeeded only: a queued or failed row has no product to search, and a
    running one may be mid-write.
    """
    rows = session.execute(
        select(ItemRow, ItemEnrichment.payload_ref)
        .join(ItemEnrichment, ItemEnrichment.item_id == ItemRow.id)
        .where(
            visible_item_rows(),
            ItemRow.tenant_id == tenant_id,
            ItemEnrichment.kind == EnrichmentKind.EMBEDDING.value,
            ItemEnrichment.status == EnrichmentStatus.SUCCEEDED.value,
            ItemEnrichment.payload_ref.is_not(None),
        )
    )
    for item, ref in rows:
        vector = decode_vector(read_body(ref))
        if vector is not None:
            yield item, vector


def nearest(
    session: Session,
    *,
    query: Sequence[float],
    tenant_id: str,
    limit: int,
    exclude_item_id: str | None = None,
) -> list[tuple[Any, float]]:
    """The `limit` items whose vectors are closest to `query`, best first.

    Brute force over every stored vector with `math.sumprod`, which ADR 0035
    measured at ~11ms for 7,000 items and ~49ms at 30,000: an ANN index solves a
    problem this corpus does not have and would be a second thing to keep
    correct. Cosine is the dot product because the encoder normalises, which is
    what makes the two sides comparable at all.

    `exclude_item_id` is for "articles like this one", where the article itself is
    never its own neighbour.
    """
    width = len(query)
    scored: list[tuple[Any, float]] = []
    for item, vector in embedded_items(session, tenant_id=tenant_id):
        if item.id == exclude_item_id:
            continue
        if len(vector) != width:
            LOGGER.warning(_WIDTH_MISMATCH, item.id, len(vector), width)
            continue
        scored.append((item, math.sumprod(query, vector)))
    return heapq.nlargest(limit, scored, key=lambda pair: pair[1])


def _vector_of(session: Session, *, item_id: str, tenant_id: str) -> list[float] | None:
    """The stored vector of one visible item, or None if it has none."""
    for item, vector in embedded_items(session, tenant_id=tenant_id):
        if item.id == item_id:
            return vector
    return None


def related_items(
    session: Session,
    *,
    item_id: str,
    tenant_id: str,
    limit: int,
    floor: float = RELATED_SIMILARITY,
) -> list[tuple[Any, float]] | None:
    """The items most like `item_id`, best first, or None if it has no vector.

    None rather than an empty list: "this item has no embedding" and "nothing is
    similar to it" are different facts, and a caller that cannot tell them apart
    would read a missing product as an absence of neighbours.

    The floor is what keeps this from being "the whole corpus in similarity
    order": a vector search always has a nearest neighbour, however unrelated, and
    a list that always returns something teaches the caller to ignore it.
    """
    vector = _vector_of(session, item_id=item_id, tenant_id=tenant_id)
    if vector is None:
        return None
    return [
        (item, score)
        for item, score in nearest(
            session,
            query=vector,
            tenant_id=tenant_id,
            limit=limit,
            exclude_item_id=item_id,
        )
        if score >= floor
    ]


def duplicates_of(
    session: Session,
    *,
    item_id: str,
    tenant_id: str,
    limit: int,
    threshold: float = NEAR_DUPLICATE_SIMILARITY,
) -> list[tuple[Any, float]] | None:
    """Items that look like another copy of `item_id`, or None if it has no vector.

    A hint and only a hint: nothing here merges, deletes or rewrites, because the
    measurement above says a copy and a paraphrase are 0.014 apart and no
    threshold can be right about that. The caller decides what to do with a pair;
    the system only says which pairs it noticed (ADR 0021).
    """
    vector = _vector_of(session, item_id=item_id, tenant_id=tenant_id)
    if vector is None:
        return None
    return [
        (item, score)
        for item, score in nearest(
            session,
            query=vector,
            tenant_id=tenant_id,
            limit=limit,
            exclude_item_id=item_id,
        )
        if score >= threshold
    ]
