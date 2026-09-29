"""Tests for timeline paging: frontier, gap detection, budget, partial results.

Scenario register (normative source: SCENARIOS.md; issue #130; AC-117..119).

`fetch_page` is a scripted stand-in, so every case is deterministic and no
sleep is involved -- pacing belongs to the caller and is not tested here.
"""

from __future__ import annotations

import pytest

from plugins.collectors.twitter.paging import (
    Page,
    WalkResult,
    ids_above_frontier,
    to_int,
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
def test_ids_above_the_frontier_are_kept_regardless_of_page_order() -> None:
    """An id above the frontier can appear *after* one at or below it.

    A page is not globally descending: a conversation module lists its parent
    (older) before its replies (newer), so a prefix scan stops at the first
    seen id and silently drops the newer ids that follow it (AC-118).
    """
    ids = ["300", "200", "250", "100"]
    # 250 > 200 but sits after it: the prefix scan this replaced returned ["300"].
    assert ids_above_frontier(ids, 200) == ["300", "250"]
    assert ids_above_frontier(ids, 350) == []
    assert ids_above_frontier(ids, 50) == ids
    assert ids_above_frontier(ids, None) == ids


@pytest.mark.unit
def test_a_catch_up_ends_on_a_page_that_contributes_nothing_new() -> None:
    """Termination is "this page had no id above the frontier".

    It is *not* "this page contained a seen id": a conversation module puts an
    already-seen parent above its newer replies, so stopping at the first seen
    id ends the round while newer content is still below it (AC-118).
    """
    script = Script(
        [
            page(["300", "200", "250"], next_cursor="C1"),
            page(["150", "100"], next_cursor="C2"),
        ]
    )
    result = walk(fetch_page=script, max_pages=5, seen_frontier=200)
    assert list(result.keep_ids) == ["300", "250"]
    assert result.pages_used == 2
    assert result.floor_candidate is None
    assert script.requested == [None, "C1"]


@pytest.mark.unit
def test_the_walk_stops_once_a_page_is_entirely_at_or_below_the_frontier() -> None:
    """A whole seen page is the terminator; one seen id is not enough.

    The extra page is deliberate: a page above the frontier can still carry a
    seen id (a module's old parent) while newer ids remain below it, so only a
    page with nothing new proves the frontier has been passed.
    """
    script = Script(
        [
            page(["300", "250"], next_cursor="C1"),
            page(["200", "100"], next_cursor="C2"),
        ]
    )
    result = walk(fetch_page=script, max_pages=5, seen_frontier=260)
    assert list(result.keep_ids) == ["300"]
    assert result.pages_used == 2
    assert result.floor_candidate is None
    assert script.requested == [None, "C1"]


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
    assert result.floor_candidate == "C1"
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
    assert result.gap_cursor == "C1"


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
    assert result.floor_candidate is None


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
    assert result.gap_cursor == "C1"


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
    # A failed walk establishes no floor: it never said how deep to go.
    assert result.floor_candidate is None
    assert result.failed == "socket blip"


@pytest.mark.unit
def test_a_first_page_failure_propagates() -> None:
    """With nothing retrieved there is no partial result to salvage."""
    script = Script([RuntimeError("no route")])
    with pytest.raises(RuntimeError, match="no route"):
        walk(fetch_page=script, max_pages=5)


# --------------------------------------------------------------------------
# The interval: floor candidate, gap reporting, budget
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_the_walk_stops_at_the_floor() -> None:
    """The floor is the interval's lower end: reaching it ends the walk.

    It marks the oldest tweet the source ever walked, so the walk must treat
    its cursor as the bottom of the timeline -- requesting the page *after*
    it would be the unbounded history crawl the floor exists to prevent.
    """
    script = Script(
        [
            page(["1100", "900"], next_cursor="F1", sort_span=(1100, 900)),
            page(["700", "600"], next_cursor="DEEP", sort_span=(700, 600)),
        ]
    )
    result = walk(fetch_page=script, max_pages=10, seen_frontier=500, floor="DEEP")
    # Page 2's next_cursor is the floor: stop without requesting it.
    assert script.requested == [None, "F1"]
    assert result.pages_used == 2
    assert result.ok
    # Ending on the floor says nothing about the interval's depth, so it is
    # not a candidate.
    assert result.floor_candidate is None


@pytest.mark.unit
def test_a_budget_limited_walk_offers_its_bottom_as_the_floor() -> None:
    """This is how the interval gets its depth: the first walk runs out of
    budget without reaching the frontier, and where it stopped becomes the
    floor. Nothing else in the walk's endings is allowed to set it."""
    script = Script(
        [
            page(["900", "800"], next_cursor="C1"),
            page(["700", "600"], next_cursor="C2"),
        ]
    )
    result = walk(fetch_page=script, max_pages=2, seen_frontier=500, floor=None)
    assert result.pages_used == 2
    assert result.floor_candidate == "C2"
    assert "FLOOR" not in script.requested


@pytest.mark.unit
def test_crossing_the_frontier_is_not_a_floor_candidate() -> None:
    """The common catch-up ending must leave the stored floor alone."""
    script = Script(
        [
            page(["900", "400"], next_cursor="C1"),
            page(["300", "200"], next_cursor=None),
        ]
    )
    result = walk(fetch_page=script, max_pages=10, seen_frontier=500)
    assert result.floor_candidate is None
    assert result.ok
    assert result.failed is None


@pytest.mark.unit
def test_reaching_the_origin_bottom_is_not_a_floor_candidate() -> None:
    script = Script([page(["800", "700"], next_cursor=None)])
    result = walk(fetch_page=script, max_pages=10, seen_frontier=500)
    assert result.floor_candidate is None


@pytest.mark.unit
def test_a_failed_walk_offers_no_floor_candidate() -> None:
    """A walk that died partway has not established how deep to go.

    Pinning a floor to a partial reach would wall off the rest of the
    history the first walk was supposed to sample.
    """
    script = Script(
        [
            page(["900", "800"], next_cursor="C1"),
            RuntimeError("risk control"),
        ]
    )
    result = walk(fetch_page=script, max_pages=5, seen_frontier=500)
    assert result.failed == "risk control"
    assert result.floor_candidate is None


@pytest.mark.unit
def test_a_gap_is_reported_without_becoming_the_floor() -> None:
    """A sortIndex break is surfaced, never used to bound the interval.

    A gap sits *within* the walked window. Pinning the floor to the break
    would strand everything between it and the true bottom -- which is why
    the candidate is the walk's bottom cursor and the gap rides its own
    field.
    """
    script = Script(
        [
            page(["900", "800"], next_cursor="C1", sort_span=(900, 800)),
            # Next page's max (850) is above the previous min (800): a break.
            page(["850", "700"], next_cursor="C2", sort_span=(850, 700)),
            page(["600", "500"], next_cursor=None, sort_span=(600, 500)),
        ]
    )
    result = walk(fetch_page=script, max_pages=10, seen_frontier=400)
    assert result.gap is True
    assert result.gap_cursor == "C1"
    # The walk ran to the origin bottom, so there is no floor candidate --
    # the gap must not have stood in for one.
    assert result.floor_candidate is None


@pytest.mark.unit
def test_an_origin_that_repeats_a_cursor_ends_the_walk() -> None:
    """A cursor answering with itself would spin for the whole budget."""
    script = Script([page(["900", "800"], next_cursor="SAME")])
    result = walk(fetch_page=script, max_pages=50, seen_frontier=100)
    assert result.pages_used == 1
    assert result.floor_candidate is None


@pytest.mark.unit
def test_walk_result_ok_is_false_only_without_pages() -> None:
    assert WalkResult(pages=[], keep_ids=["x"]).ok is False
    assert WalkResult(pages=[b"body"], keep_ids=[]).ok is True


@pytest.mark.unit
def test_a_gap_does_not_become_the_floor_when_the_budget_runs_out() -> None:
    """The case that made the floor wrong: gap present *and* budget exhausted.

    The gap cursor sits above the walk's bottom, so using it as the floor
    would strand every tweet between the break and the real bottom -- content
    the walk had already fetched past. The two are separate fields precisely
    so this cannot be conflated.
    """
    script = Script(
        [
            page(["900", "800"], next_cursor="C1", sort_span=(900, 800)),
            # Next page's max (850) is above the previous min (800): a break.
            page(["850", "700"], next_cursor="C2", sort_span=(850, 700)),
        ]
    )
    result = walk(fetch_page=script, max_pages=2, seen_frontier=100)
    assert result.gap is True
    assert result.gap_cursor == "C1"
    # The floor is where the walk stopped, not where it noticed a hole.
    assert result.floor_candidate == "C2"
