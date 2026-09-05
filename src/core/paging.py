"""Paging policy that the repository and the collectors must agree on.

Only the bound lives here. Walking a page at a time is a collector concern;
persisting where the walk stopped is a repository concern; how long a backlog
may stay unresolved before it is abandoned is an invariant both sides need the
same answer to, which is what makes it domain policy.
"""

from __future__ import annotations

from typing import Final

# Consecutive cycles a resume cursor may stay unresolved before it is dropped.
# A backlog deeper than one cycle's budget has no natural stopping anchor, so
# without a bound the walk retreats further into the past every cycle and never
# closes: the collector spends each run re-walking old history while new content
# piles up behind it. Dropping the cursor accepts a bounded gap in deep history
# instead of an unbounded slide.
MAX_RESUME_ROUNDS: Final = 3
