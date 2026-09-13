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

    `reached_floor` says the walk ended on the interval's lower end. Cursors
    are opaque strings, so the host cannot tell a floor from a shallow
    budget-stop cursor by comparing them -- this flag is what authorises the
    host to move the stored floor at all.
    """

    pages: list[bytes] = field(default_factory=list)
    keep_ids: list[object] = field(default_factory=list)
    resume: str | None = None
    gap: bool = False
    pages_used: int = 0
    reached_floor: bool = False
    # The newest id seen, for persisting the frontier even when nothing was kept.
    newest_id: int | None = None
    failed: str | None = None

    @property
    def ok(self) -> bool:
        """Whether at least one page was retrieved."""
        return bool(self.pages)


def walk(
    *,
    fetch_page: Callable[[str | None], Page],
    max_pages: int,
    seen_frontier: int | None = None,
    resume: str | None = None,
    use_frontier: bool = True,
    floor: str | None = None,
) -> WalkResult:
    """Page newest-first until the frontier, the floor, or the budget.

    `fetch_page` receives the cursor to request (None for the newest page) and
    returns a `Page`. It raises to signal failure; the walk then decides
    whether to keep what it has.

    `use_frontier` is the single switch for that: the frontier is still passed
    in, so a caller can see what is being ignored rather than reading `None`
    and having to infer it.

    `floor` is the interval's lower end: a cursor marking the oldest tweet
    the source ever walked. Reaching it is reaching the bottom -- the walk
    stops there exactly as if the origin had no deeper page, so history
    below it is never crawled. It is a stop sign, never a starting cursor.
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
        at_floor = floor is not None and page.next_cursor == floor
        if crossed_frontier or page.next_cursor is None or at_floor:
            # Caught up, reached the interval's floor, or the origin has
            # nothing deeper. A detected gap still outranks any of these:
            # skipped content is in no page, so neither the frontier nor the
            # floor can ever cover it.
            return WalkResult(
                pages=bodies,
                keep_ids=keep,
                resume=gap_cursor,
                gap=gap_cursor is not None,
                pages_used=pages_used,
                newest_id=newest,
                reached_floor=at_floor,
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
    floor: str | None = None,
) -> WalkResult:
    """Walk from the top until the frontier, the floor, or the budget.

    The interval model: the frontier is the newest ingested tweet, `floor`
    is the oldest tweet the first walk ever reached (a cursor marking where
    history ends for this source). One phase suffices, because the walk
    always starts at the top of the timeline: new content sits above the
    frontier, so a single pass that stops at either the frontier or the
    floor cannot miss anything between them.

    The floor is a stop sign, not a place to continue from. Passing it as a
    resume cursor would walk *below* the oldest tweet ever kept -- a history
    crawl with no natural end, observed live as a timeline dragged back to
    2023. When the budget runs out before the frontier is crossed the walk
    simply returns; the next cycle starts at the top again and catches up,
    with dedupe absorbing the overlap.

    `gap` detection still applies within the window: a sortIndex break means
    tweets are known-missing between two pages, and the walk reports the
    cursor where the break happened so an operator (or a later, more generous
    budget) can look there deliberately.
    """
    return walk(
        fetch_page=fetch_page,
        max_pages=max_pages,
        seen_frontier=seen_frontier,
        floor=floor,
    )


__all__ = [
    "Page",
    "WalkResult",
    "to_int",
    "truncate_at_seen",
    "two_phase_walk",
    "walk",
]
