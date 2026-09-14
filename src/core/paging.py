"""Paging policy that the repository and the collectors must agree on.

Only the bound lives here. Walking a page at a time is a collector concern;
persisting where the walk stopped is a repository concern; how long a backlog
may stay unresolved before it is abandoned is an invariant both sides need the
same answer to, which is what makes it domain policy.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WalkState:
    """What a paged walk needs to know about the host's paging position.

    The host owns persistence (adapters.repository.PagingState is the stored
    view of this); the walk and the sandboxed child only ever read these
    fields, so the child may pass any object with this shape — no adapters
    import required.

    `floor_cursor` is the interval's lower end: the deepest point any walk of
    this source has reached. A stop sign, not a place to resume from --
    resuming below it would crawl history with no end. It is set by the first
    walk's budget-limited bottom and only ever moved deeper.
    """

    seen_frontier: int | None = None
    floor_cursor: str | None = None
