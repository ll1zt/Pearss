"""Tests for timeline paging: frontier, gap detection, budget, partial results.

`fetch_page` is a scripted stand-in, so every case is deterministic and no
sleep is involved -- pacing belongs to the caller and is not tested here.
"""

from __future__ import annotations

import pytest

from plugins.collectors.twitter.paging import (
    Page,
    WalkResult,
    to_int,
    truncate_at_seen,
    two_phase_walk,
    walk,
)


def page(
    ids: list[str],
    *,
    next_cursor: str | None = None,
    sort_span: tuple[int, int] | None = None,
) -> Page:
    return Page(
        body=f"page:{ids}".encode(),
        ids=ids,
        next_cursor=next_cursor,
        sort_span=sort_span,
    )


class Script:
    """Serves canned pages and records which cursors were requested."""

    def __init__(self, pages: list[Page | Exception]) -> None:
        self._pages = list(pages)
        self.requested: list[str | None] = []

    def __call__(self, cursor: str | None) -> Page:
        self.requested.append(cursor)
        if not self._pages:
            raise AssertionError("requested more pages than scripted")
        item = self._pages.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


# --------------------------------------------------------------------------
# Frontier
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_ids_parse_from_both_str_and_int() -> None:
    """The live API sends strings; recorded fixtures use integers."""
    assert to_int("123") == 123
    assert to_int(123) == 123
    assert to_int("nope") is None
    assert to_int(None) is None
    # bool is an int subclass; it is never a tweet id.
    assert to_int(True) is None


@pytest.mark.unit
def test_truncation_stops_at_the_first_id_already_ingested() -> None:
    ids = ["300", "200", "100"]
    assert truncate_at_seen(ids, 250) == ["300"]
    assert truncate_at_seen(ids, 350) == []
    assert truncate_at_seen(ids, 50) == ids
    assert truncate_at_seen(ids, None) == ids


@pytest.mark.unit
def test_the_walk_stops_at_the_frontier_without_reading_deeper() -> None:
    """Reaching already-seen ids means there is nothing left to ingest.

    Continuing would spend requests on content the store already has, and on a
    live timeline the newest page can be entirely seen while deeper pages are
    not, so the stop has to be per-page.
    """
    script = Script(
        [
            page(["300", "250"], next_cursor="C1"),
            page(["200", "100"], next_cursor="C2"),
        ]
    )
    result = walk(fetch_page=script, max_pages=5, seen_frontier=260, resume=None)
    assert result.pages_used == 1
    assert list(result.keep_ids) == ["300"]
    assert result.resume is None
    assert len(script.requested) == 1


@pytest.mark.unit
def test_frontier_none_reads_to_the_bottom() -> None:
    script = Script([page(["300"], next_cursor="C1"), page(["200"], next_cursor=None)])
    result = walk(fetch_page=script, max_pages=5)
    assert result.pages_used == 2
    assert list(result.keep_ids) == ["300", "200"]


# --------------------------------------------------------------------------
# Budget
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_budget_exhaustion_hands_back_the_deep_water_cursor() -> None:
    """The walk stopped for want of budget, not because it caught up."""
    script = Script([page([str(1000 - i)], next_cursor=f"C{i}") for i in range(3)])
    result = walk(fetch_page=script, max_pages=2)
    assert result.pages_used == 2
    assert result.resume == "C1"
    assert result.gap is False


@pytest.mark.unit
def test_zero_budget_fetches_nothing() -> None:
    script = Script([])
    result = walk(fetch_page=script, max_pages=0)
    assert result.pages_used == 0
    assert result.ok is False


# --------------------------------------------------------------------------
# Gap detection
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_a_sort_index_discontinuity_is_reported_as_a_gap() -> None:
    """Content between two non-contiguous pages appears in no page at all.

    Dedupe cannot recover it -- it only prevents storing the same item twice,
    and these were never stored. The cursor of the page that broke
    continuity is the place to re-walk from.
    """
    script = Script(
        [
            page(["300", "290"], next_cursor="C1", sort_span=(900, 800)),
            # Next page's max (850) is above the previous page's min (800):
            # the 800-850 band was skipped.
            page(["200", "190"], next_cursor="C2", sort_span=(850, 700)),
            page(["100"], next_cursor=None, sort_span=(600, 500)),
        ]
    )
    result = walk(fetch_page=script, max_pages=5)
    assert result.gap is True
    assert result.resume == "C1"


@pytest.mark.unit
def test_contiguous_pages_report_no_gap() -> None:
    script = Script(
        [
            page(["300"], next_cursor="C1", sort_span=(900, 800)),
            page(["200"], next_cursor="C2", sort_span=(799, 700)),
            page(["100"], next_cursor=None, sort_span=(699, 600)),
        ]
    )
    result = walk(fetch_page=script, max_pages=5)
    assert result.gap is False
    assert result.resume is None


@pytest.mark.unit
def test_a_gap_outranks_being_caught_up() -> None:
    """Reaching the frontier is not "done" if content was skipped.

    The frontier covers what was ingested; a gap is what was never in any
    page, so the frontier can never account for it.
    """
    script = Script(
        [
            page(["900", "800"], next_cursor="C1", sort_span=(900, 800)),
            page(["300", "100"], next_cursor="C2", sort_span=(850, 700)),
        ]
    )
    result = walk(fetch_page=script, max_pages=5, seen_frontier=150)
    assert result.gap is True
    assert result.resume == "C1"


@pytest.mark.unit
def test_pages_without_sort_index_cannot_prove_continuity() -> None:
    """A missing sortIndex is not treated as a gap, nor as proof of none."""
    script = Script(
        [
            page(["300"], next_cursor="C1"),
            page(["200"], next_cursor=None),
        ]
    )
    result = walk(fetch_page=script, max_pages=5)
    assert result.gap is False
    assert result.pages_used == 2


# --------------------------------------------------------------------------
# Partial results
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_a_later_failure_keeps_earlier_pages() -> None:
    """A mid-walk outage should not discard what already arrived."""
    script = Script([page(["300"], next_cursor="C1"), RuntimeError("socket blip")])
    result = walk(fetch_page=script, max_pages=5)
    assert result.ok
    assert len(result.pages) == 1
    assert result.resume == "C1"
    assert result.failed == "socket blip"


@pytest.mark.unit
def test_a_first_page_failure_propagates() -> None:
    """With nothing retrieved there is no partial result to salvage."""
    script = Script([RuntimeError("no route")])
    with pytest.raises(RuntimeError, match="no route"):
        walk(fetch_page=script, max_pages=5)


# --------------------------------------------------------------------------
# Two-phase walk
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_the_walk_stops_at_the_floor() -> None:
    """The floor is the interval's lower end: reaching it ends the walk.

    It marks the oldest tweet the source ever walked. History below it is
    unfetched by design, so the walk must treat the floor's cursor as the
    bottom of the timeline -- requesting the page *after* it would be the
    unbounded history crawl the floor exists to prevent.
    """
    script = Script(
        [
            page(["1100", "900"], next_cursor="F1", sort_span=(1100, 900)),
            page(["700", "600"], next_cursor="DEEP", sort_span=(700, 600)),
        ]
    )
    result = two_phase_walk(
        fetch_page=script, max_pages=10, seen_frontier=500, floor="DEEP"
    )
    # Page 2's next_cursor is the floor: stop without requesting it.
    assert script.requested == [None, "F1"]
    assert result.pages_used == 2
    assert result.ok
    assert result.resume is None


def test_the_floor_never_becomes_a_resume_cursor() -> None:
    """A walk that ends at the floor is complete, not unfinished.

    Reporting the floor as a resume would make the next cycle continue from
    below it -- the history crawl the interval model forbids.
    """
    script = Script(
        [page(["700", "600"], next_cursor="DEEP")],
    )
    result = two_phase_walk(
        fetch_page=script, max_pages=10, seen_frontier=500, floor="DEEP"
    )
    assert result.resume is None
    assert result.ok


def test_budget_exhaustion_above_the_floor_stops_this_cycle() -> None:
    """An overshooting cycle reports its own bottom cursor and stops.

    The next cycle starts from the top again, not from this cursor: resuming
    downward would skip whatever the timeline serves in between. The stored
    floor only moves when a walk genuinely reaches deeper than before; that
    decision belongs to the host's save_paging_state, which only accepts a
    cursor it does not already hold.
    """
    script = Script(
        [
            page(["900", "800"], next_cursor="C1"),
            page(["700", "600"], next_cursor="C2"),
        ]
    )
    result = two_phase_walk(
        fetch_page=script, max_pages=2, seen_frontier=500, floor="FLOOR"
    )
    assert result.pages_used == 2
    assert result.resume == "C2"
    assert "FLOOR" not in script.requested


def test_a_gap_still_outranks_the_floor() -> None:
    """A sortIndex break means tweets are known-missing; report the cursor.

    Even at the floor, a gap inside the window is content no page carried,
    so neither the frontier nor the floor covers it. The walk surfaces the
    break so a later pass can look there deliberately.
    """
    script = Script(
        [
            page(["900", "800"], next_cursor="C1", sort_span=(900, 800)),
            # Next page's max (850) is above the previous min (800): a break.
            # Content sits between 800 and 850 that no page carried.
            page(["850", "700"], next_cursor="DEEP", sort_span=(850, 700)),
        ]
    )
    result = two_phase_walk(
        fetch_page=script, max_pages=10, seen_frontier=500, floor="DEEP"
    )
    assert result.gap is True
    assert result.resume == "C1"


@pytest.mark.unit
def test_walk_result_ok_is_false_only_without_pages() -> None:
    assert WalkResult(pages=[], keep_ids=["x"]).ok is False
    assert WalkResult(pages=[b"body"], keep_ids=[]).ok is True
