"""Timeline paging: frontier, gap detection, and resumable backfill.

Walks X's Home timeline newest-first and decides what to keep. The logic is
separated from the HTTP call and from the sleep between pages so all of it can
be tested against recorded payloads with no network and no waiting.

Three ideas, each of which exists because the naive version loses data:

**The seen frontier.** The newest tweet id already ingested. Paging stops at
the first page that reaches it, rather than running to the budget every time.
X tweet ids grow with time, so a single integer is enough: no set of seen ids,
and nothing sent to the origin that would change what it returns.

**Gap detection.** The timeline is live. A tweet can be inserted between two
page requests, and then the next page starts *above* where the previous one
ended, so the skipped content appears in no page at all. Dedupe cannot help --
it only prevents storing the same item twice, it cannot know something was
missed. Continuity is checked through each page's `sortIndex` span, and a break
is handed back as a resume cursor that outranks every other kind.

**Partial results.** If a page fails after earlier ones succeeded, those pages
are kept and the failure cursor is returned as `resume`. Discarding them would
turn a transient blip into a wasted cycle, and reporting the run as a clean
success would hide that the walk stopped early.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Final

from core.paging import MAX_RESUME_ROUNDS

# How much of a cycle's budget is reserved for an unfinished backfill when one
# exists, so a very active account cannot starve its own history by consuming
# the whole budget on new content every time.
BACKFILL_RESERVE_NUMERATOR: Final = 3
BACKFILL_RESERVE_DENOMINATOR: Final = 10
# Below this many pages there is nothing spare to reserve: with 2 pages, giving
# one to the backfill leaves the fresh phase unable to reach the frontier.
MIN_PAGES_FOR_RESERVE: Final = 3

# Consecutive cycles an unresolved resume may slide backwards before it is
# dropped. A backlog deeper than the frontier has no natural stopping anchor,
# so without a bound the walk retreats further into the past every cycle and
# never catches up; dropping it accepts a bounded gap in deep history instead
# of an unbounded one.
# Consecutive cycles an unresolved resume may slide backwards before it is
# dropped. The bound itself lives in core.paging, because the repository ages
# the counter and this module reads it -- the two must agree.


def to_int(value: object) -> int | None:
    """Parse a tweet id as an integer, tolerating str and int alike."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    return None


def truncate_at_seen(ids: Sequence[object], seen_frontier: int | None) -> list[object]:
    """Ids up to (not including) the first one already ingested.

    The frontier is the newest ingested id, and pages arrive newest-first, so
    everything from the first id at-or-below it onwards is already stored.
    """
    if seen_frontier is None:
        return list(ids)
    for index, value in enumerate(ids):
        numeric = to_int(value)
        if numeric is not None and numeric <= seen_frontier:
            return list(ids[:index])
    return list(ids)


@dataclass(frozen=True, slots=True, kw_only=True)
class Page:
    """What one fetched page contributed."""

    body: bytes
    ids: Sequence[object]
    next_cursor: str | None
    sort_span: tuple[int, int] | None


@dataclass(frozen=True, slots=True, kw_only=True)
class WalkResult:
    """Outcome of one paginated walk.

    `resume` is non-None when work is unfinished. `gap` says which kind: a gap
    is a place where content is *known* to be missing, whereas a plain cursor
    only means the budget ran out. Callers prioritise the former.
    """

    pages: list[bytes] = field(default_factory=list)
    keep_ids: list[object] = field(default_factory=list)
    resume: str | None = None
    gap: bool = False
    pages_used: int = 0
    # The newest id seen, for persisting the frontier even when nothing was kept.
    newest_id: int | None = None
    failed: str | None = None

    @property
    def ok(self) -> bool:
        """Whether at least one page was retrieved."""
        return bool(self.pages)


def reserve_for_backfill(max_pages: int, resume_in: str | None) -> int:
    """Pages held back for an unfinished backfill, or 0 when there is none."""
    if not resume_in or max_pages < MIN_PAGES_FOR_RESERVE:
        return 0
    reserve = max(
        1, max_pages * BACKFILL_RESERVE_NUMERATOR // BACKFILL_RESERVE_DENOMINATOR
    )
    return min(reserve, max_pages)


def walk(
    *,
    fetch_page: Callable[[str | None], Page],
    max_pages: int,
    seen_frontier: int | None = None,
    resume: str | None = None,
    use_frontier: bool = True,
) -> WalkResult:
    """Page newest-first until the frontier, the bottom, or the budget.

    `fetch_page` receives the cursor to request (None for the newest page) and
    returns a `Page`. It raises to signal failure; the walk then decides
    whether to keep what it has.

    `use_frontier` is the single switch for that: the frontier is still passed
    in, so a caller can see what is being ignored rather than reading `None`
    and having to infer it.
    """
    frontier = seen_frontier if use_frontier else None
    budget = max(0, max_pages)
    bodies: list[bytes] = []
    keep: list[object] = []
    cursor = resume
    pages_used = 0
    gap_cursor: str | None = None
    previous_min_sort: int | None = None
    newest: int | None = None

    while pages_used < budget:
        try:
            page = fetch_page(cursor)
        except Exception as exc:  # classified by the caller
            if bodies:
                # Keep the progress; the failing cursor is where to continue.
                return WalkResult(
                    pages=bodies,
                    keep_ids=keep,
                    resume=cursor,
                    gap=gap_cursor is not None,
                    pages_used=pages_used,
                    newest_id=newest,
                    failed=str(exc),
                )
            raise

        pages_used += 1
        for value in page.ids:
            numeric = to_int(value)
            if numeric is not None and (newest is None or numeric > newest):
                newest = numeric

        kept_here = truncate_at_seen(page.ids, frontier)
        bodies.append(page.body)
        keep.extend(kept_here)

        # A gap is where this page's newest sortIndex fails to fall below the
        # previous page's oldest: content sits between them and appears in no
        # page. Only the first break in a walk is recorded, since paging runs
        # newest-to-oldest and that one is the most recent missing region.
        if page.sort_span is not None:
            current_max, current_min = page.sort_span
            if (
                gap_cursor is None
                and previous_min_sort is not None
                and current_max >= previous_min_sort
            ):
                gap_cursor = cursor
            previous_min_sort = current_min

        crossed_frontier = len(kept_here) < len(page.ids)
        if crossed_frontier or page.next_cursor is None:
            # Caught up, or the origin has nothing deeper. A detected gap
            # still outranks "caught up": skipped content is in no page, so
            # the frontier can never cover it.
            return WalkResult(
                pages=bodies,
                keep_ids=keep,
                resume=gap_cursor,
                gap=gap_cursor is not None,
                pages_used=pages_used,
                newest_id=newest,
            )

        cursor = page.next_cursor

    # Budget exhausted: hand back the gap if there is one, else the deep-water
    # cursor for the next cycle to continue from.
    resume_out = gap_cursor or cursor
    return WalkResult(
        pages=bodies,
        keep_ids=keep,
        resume=resume_out,
        gap=gap_cursor is not None,
        pages_used=pages_used,
        newest_id=newest,
    )


def two_phase_walk(
    *,
    fetch_page: Callable[[str | None], Page],
    max_pages: int,
    seen_frontier: int | None,
    resume: str | None,
) -> WalkResult:
    """Fresh pages first, then catch up an unfinished backfill with the rest.

    The fresh phase reserves part of the budget for the backfill when a resume
    exists, so a highly active account cannot permanently starve its own
    history. The backfill deliberately ignores the frontier, for the reason
    given on `walk(use_frontier=False)`.
    """
    reserve = reserve_for_backfill(max_pages, resume)
    fresh_budget = max_pages - reserve

    fresh = walk(
        fetch_page=fetch_page,
        max_pages=fresh_budget,
        seen_frontier=seen_frontier,
    )

    if not resume or max_pages - fresh.pages_used <= 0:
        return fresh

    try:
        backfill = walk(
            fetch_page=fetch_page,
            max_pages=max_pages - fresh.pages_used,
            seen_frontier=seen_frontier,
            resume=resume,
            use_frontier=False,
        )
    except Exception as exc:  # classified by the caller
        # Phase A's pages are still worth ingesting; losing them to a phase B
        # failure would make a partial outage cost a whole cycle's content.
        return WalkResult(
            pages=fresh.pages,
            keep_ids=fresh.keep_ids,
            resume=resume,
            gap=fresh.gap,
            pages_used=fresh.pages_used,
            newest_id=fresh.newest_id,
            failed=str(exc),
        )

    # A gap from the fresh phase outranks everything: it is a known hole.
    # Otherwise the backfill's own continuation cursor wins over the fresh
    # phase's deep-water cursor, which the next cycle would re-derive anyway.
    resume_out = fresh.resume if fresh.gap else (backfill.resume or fresh.resume)
    return WalkResult(
        pages=fresh.pages + backfill.pages,
        keep_ids=fresh.keep_ids + backfill.keep_ids,
        resume=resume_out,
        gap=fresh.gap or backfill.gap,
        pages_used=fresh.pages_used + backfill.pages_used,
        newest_id=max(
            filter(None, [fresh.newest_id, backfill.newest_id]), default=None
        ),
    )


def should_drop_resume(rounds_unresolved: int, limit: int = MAX_RESUME_ROUNDS) -> bool:
    """Whether a resume cursor has slid backwards too many times to keep.

    Bounded on purpose: without a limit a backlog deeper than the frontier
    retreats further into the past every cycle and is never closed, so the
    collector spends every run re-walking old history while new content
    accumulates behind it.
    """
    return rounds_unresolved >= limit


__all__ = [
    "MAX_RESUME_ROUNDS",
    "Page",
    "WalkResult",
    "reserve_for_backfill",
    "should_drop_resume",
    "to_int",
    "truncate_at_seen",
    "two_phase_walk",
    "walk",
]
