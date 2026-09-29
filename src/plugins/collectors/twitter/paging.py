"""Timeline paging: frontier, gap detection, and resumable backfill.

Walks X's Home timeline newest-first and decides what to keep. The logic is
separated from the HTTP call and from the sleep between pages so all of it can
be tested against recorded payloads with no network and no waiting.

Three ideas, each of which exists because the naive version loses data:

**The seen frontier.** The newest tweet id already ingested. A catch-up stops
once a whole page contributes nothing above it, rather than running to the
budget every time. X tweet ids grow with time, so a single integer is enough:
no set of seen ids, and nothing sent to the origin that would change what it
returns.

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


def ids_above_frontier(
    ids: Sequence[object], seen_frontier: int | None
) -> list[object]:
    """Ids strictly newer than the newest already-ingested id.

    A *filter*, not a prefix cut. A page is not globally descending: a
    conversation module lists its parent (older) before its replies (newer), so
    an id above the frontier can follow one at or below it. Cutting at the first
    seen id dropped those newer ids -- and, because the walk read that cut as
    "the frontier was crossed", ended the round on the same page. That is how a
    catch-up silently lost tweets (issue #130).

    An id that does not parse is kept: it cannot be compared to the frontier,
    and dropping it on that basis would be the same silent loss in a new place.
    """
    if seen_frontier is None:
        return list(ids)
    return [
        value
        for value in ids
        if (numeric := to_int(value)) is None or numeric > seen_frontier
    ]


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

    `gap` marks a place where content is *known* to be missing between two
    pages (`gap_cursor` says where). Detection and recovery are separate
    concerns: the walk surfaces the break, and the host decides what a gap
    is worth -- it is logged, never silently chased.

    `floor_candidate` is the walk's own bottom cursor, set only when the walk
    ended by budget exhaustion without crossing the frontier. That is the one
    situation where a floor should be established (the first walk defining
    the interval's depth); every other ending -- frontier crossed, origin
    bottom, floor reached, failure -- leaves the stored floor alone. Cursors
    are opaque strings, so the host cannot compare positions; the walk saying
    "this ended on budget" is what makes the candidate meaningful.
    """

    pages: list[bytes] = field(default_factory=list)
    keep_ids: list[object] = field(default_factory=list)
    gap: bool = False
    gap_cursor: str | None = None
    pages_used: int = 0
    floor_candidate: str | None = None
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
    floor: str | None = None,
) -> WalkResult:
    """Page newest-first until the frontier, the floor, the bottom, or the budget.

    `fetch_page` receives the cursor to request (None for the newest page) and
    returns a `Page`. It raises to signal failure; the walk then decides
    whether to keep what it has.

    Termination, in the order the loop reaches them:

    * **frontier** -- a page that contributes nothing above the newest ingested
      tweet. The normal end of a catch-up walk: content is ordered
      newest-first, so once a whole page is at or below the frontier nothing
      newer can follow. (This is one page later than "the page that contains a
      seen id", because a conversation module can carry a seen parent above its
      newer replies.) Request count still tracks the origin's actual output
      rather than a policy number.
    * **floor** -- the interval's lower end, a cursor marking the oldest tweet
      the source ever walked. A stop sign, never a starting cursor. Best
      effort by nature: cursors are opaque and a later walk's chain may not
      revisit the stored floor's exact value -- the frontier is the
      terminator that matters, and this one is a backstop.
    * **origin bottom** -- no next cursor.
    * **budget** -- see the caller. For a first walk it defines the floor's
      depth (the operator's history sample); for a catch-up walk it is a
      runaway guard against an origin that loops cursors, not a policy cap --
      a budget that stops a walk before the frontier is how tweets get lost.
    """
    frontier = seen_frontier
    budget = max(0, max_pages)
    bodies: list[bytes] = []
    keep: list[object] = []
    cursor: str | None = None  # a walk always starts at the top of the timeline
    pages_used = 0
    gap_cursor: str | None = None
    previous_min_sort: int | None = None
    newest: int | None = None

    while pages_used < budget:
        try:
            page = fetch_page(cursor)
        except Exception as exc:  # classified by the caller
            if bodies:
                # Keep the progress. No floor candidate: a walk that died
                # partway has not established how deep the interval goes, and
                # pinning a floor to a partial reach would wall off the rest.
                return WalkResult(
                    pages=bodies,
                    keep_ids=keep,
                    gap=gap_cursor is not None,
                    gap_cursor=gap_cursor,
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

        kept_here = ids_above_frontier(page.ids, frontier)
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

        # A catch-up ends once a whole page contributes nothing above the
        # frontier. "This page contained a seen id" is not enough: a
        # conversation module puts an already-seen parent above its newer
        # replies, so that test ended the round while newer content was still
        # below it (issue #130). A page with no new ids proves the walk has
        # passed the frontier -- content is ordered newest-first, so nothing
        # newer can follow. A cursor-only page carries no ids and is not an
        # ending.
        crossed_frontier = frontier is not None and bool(page.ids) and not kept_here
        at_floor = floor is not None and page.next_cursor == floor
        if crossed_frontier or page.next_cursor is None or at_floor:
            # Caught up, reached the interval's floor, or the origin has
            # nothing deeper. No floor candidate: the interval's depth was
            # already fixed by the first walk, and none of these endings says
            # anything new about it.
            return WalkResult(
                pages=bodies,
                keep_ids=keep,
                gap=gap_cursor is not None,
                gap_cursor=gap_cursor,
                pages_used=pages_used,
                newest_id=newest,
            )

        # An origin that answers every cursor with itself would otherwise spin
        # for the whole budget; treat the repetition as the bottom.
        if page.next_cursor == cursor:
            return WalkResult(
                pages=bodies,
                keep_ids=keep,
                gap=gap_cursor is not None,
                gap_cursor=gap_cursor,
                pages_used=pages_used,
                newest_id=newest,
            )

        cursor = page.next_cursor

    # Budget exhausted without crossing the frontier: the walk's own bottom is
    # the floor candidate. Deliberately not the gap cursor -- a gap sits
    # *within* the walked window, above this point, and pinning the floor to
    # it would strand everything between the gap and the true bottom.
    return WalkResult(
        pages=bodies,
        keep_ids=keep,
        gap=gap_cursor is not None,
        gap_cursor=gap_cursor,
        pages_used=pages_used,
        newest_id=newest,
        floor_candidate=cursor,
    )


__all__ = [
    "Page",
    "WalkResult",
    "ids_above_frontier",
    "to_int",
    "walk",
]
