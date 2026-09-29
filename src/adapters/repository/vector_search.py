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
from typing import Any

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
