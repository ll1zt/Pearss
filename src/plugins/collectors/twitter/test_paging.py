"""Tests for timeline paging: frontier, gap detection, budget, partial results.

`fetch_page` is a scripted stand-in, so every case is deterministic and no
sleep is involved -- pacing belongs to the caller and is not tested here.
"""

from __future__ import annotations

import pytest

from plugins.collectors.twitter.paging import (
    Page,
    WalkResult,
    reserve_for_backfill,
    should_drop_resume,
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
def test_backfill_reserve_scales_and_has_a_floor() -> None:
    assert reserve_for_backfill(20, None) == 0
    assert reserve_for_backfill(2, "C") == 0  # nothing spare at that budget
    assert reserve_for_backfill(3, "C") == 1
    assert reserve_for_backfill(20, "C") == 6


@pytest.mark.unit
def test_a_resume_gets_its_own_phase_after_the_fresh_pages() -> None:
    """New content and the backlog are walked in one cycle, fresh first."""
    script = Script(
        [
            # Fresh phase: reaches the frontier on page 1.
            page(["900", "500"], next_cursor="F1", sort_span=(900, 500)),
            # Backfill phase, starting at the resume cursor.
            page(["400"], next_cursor="B1", sort_span=(400, 300)),
            page(["300"], next_cursor=None, sort_span=(300, 200)),
        ]
    )
    result = two_phase_walk(
        fetch_page=script, max_pages=10, seen_frontier=500, resume="R1"
    )
    assert script.requested[0] is None
    assert "R1" in script.requested
    assert result.pages_used == 3
    assert result.ok


@pytest.mark.unit
def test_the_backfill_phase_ignores_the_frontier() -> None:
    """A re-walk must continue *through* already-seen ids, not stop at them.

    Both backfill pages sit below the frontier. Honouring it there would end
    the re-walk on the first page and lose exactly the content the re-walk
    exists to recover, so the second page must still be requested. The overlap
    is left to dedupe, which is what it is for.
    """
    calls: list[str | None] = []

    def fetch(cursor: str | None) -> Page:
        calls.append(cursor)
        if cursor == "R1":
            # Entirely below the frontier of 500, but there is deeper to go.
            return page(["400", "300"], next_cursor="R2")
        return page(["200", "100"], next_cursor=None)

    result = two_phase_walk(
        fetch_page=fetch, max_pages=6, seen_frontier=500, resume="R1"
    )
    assert calls[0] is None, "the fresh phase runs first"
    assert "R2" in calls, f"backfill stopped at the frontier: {calls}"
    # Everything on those pages is kept despite being already-seen.
    assert list(result.keep_ids) == ["400", "300", "200", "100"]


@pytest.mark.unit
def test_a_backfill_failure_does_not_cost_the_fresh_pages() -> None:
    script = Script(
        [
            page(["900", "500"], next_cursor="F1"),
            RuntimeError("risk control"),
        ]
    )
    result = two_phase_walk(
        fetch_page=script, max_pages=10, seen_frontier=600, resume="R1"
    )
    assert result.ok
    assert len(result.pages) == 1
    assert result.failed == "risk control"
    assert result.resume == "R1"


# --------------------------------------------------------------------------
# Resume ageing
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_an_unresolved_resume_is_dropped_after_a_bound() -> None:
    """Otherwise a deep backlog slides backwards forever and never closes."""
    assert should_drop_resume(0) is False
    assert should_drop_resume(2) is False
    assert should_drop_resume(3) is True
    assert should_drop_resume(9) is True


@pytest.mark.unit
def test_walk_result_ok_is_false_only_without_pages() -> None:
    assert WalkResult(pages=[], keep_ids=["x"]).ok is False
    assert WalkResult(pages=[b"body"], keep_ids=[]).ok is True
