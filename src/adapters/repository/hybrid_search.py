"""Two full paths, fused by rank (ADR 0035 decision 7).

The lexical path is FTS5 over titles and bodies; the semantic one is a vector
scan. Neither is a prefilter for the other: a cascade -- take the best 200 by
keyword, then rerank by vector -- caps semantic recall at whatever the first path
returned, and "the query's words do not appear in the document" is precisely the
case the vector path exists to catch (AC-105). Both run in full, to the same
depth, and the ranks are fused by `core.fusion`.

What each path enforces on its own is visibility: both consult the gate in SQL, so
a pending item is unreachable by either.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from sqlalchemy.orm import Session

from adapters.repository.search_index import matching_item_ids
from adapters.repository.vector_search import nearest
from core.fusion import reciprocal_rank_fusion

# How deep each path goes before fusion. ADR 0035 measured the vector path at
# ~11ms for 7,000 items, so depth is not the constraint; this is the point past
# which a fused result is no longer plausibly about the query.
PER_PATH_LIMIT: Final = 200


def fused_item_ids(
    session: Session,
    *,
    text: str,
    query: Sequence[float],
    tenant_id: str,
    per_path: int = PER_PATH_LIMIT,
) -> list[tuple[str, float]]:
    """The fused `(item_id, score)` list, best first.

    The caller's filters are *not* applied here, only visibility: the ranks come
    from an index and a scan that know nothing about a source or a date range. A
    filtered item therefore still occupies its rank, which thins the fused list
    rather than changing the order of what remains -- the alternative would be
    passing every filter into both paths, and the two would then have to agree
    about what a filter means.
    """
    lexical = matching_item_ids(session, query=text, limit=per_path)
    semantic = [
        item.id
        for item, _score in nearest(
            session, query=query, tenant_id=tenant_id, limit=per_path
        )
    ]
    return reciprocal_rank_fusion([lexical, semantic])
