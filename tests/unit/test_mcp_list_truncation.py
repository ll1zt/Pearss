"""The presentation fallback must not overrule the caller's page size.

Unit-layer coverage for the list bound (issue #65); the end-to-end half is
`tests/integration/test_mcp_list_truncation.py` (AC-41, AC-41b). These pin the
rule itself and the boundary around it, at the fast tier.

The rule: the caller's own result set — the top-level `data` list, whose size
the endpoint already bounds by the requested `limit` — is returned whole.
Every *other* list, at any depth, keeps the `MAX_LIST_ITEMS` fallback and the
`truncated_items` marker.

Why the exemption is exactly `data`, and only at the top level: that is the key
every list endpoint returns its page under, and the one whose length the caller
chose. A fallback that also cuts it turns `limit` into a suggestion, which is
how #65 read from the outside. Everything else stays capped, so a nested array
that unexpectedly grows (a manifest's feature list, a row's tags) still cannot
eat the context, and a write result's per-item rows stay bounded rather than
echoing a large submission back.
"""

from __future__ import annotations

import pytest

from app.mcp.rendering import MAX_LIST_ITEMS, _shrink, _truncate

pytestmark = pytest.mark.unit

# Comfortably past the fallback, comfortably under the API ceiling (100).
ROWS = 60


def _rows(count: int = ROWS) -> list[dict[str, str]]:
    return [{"id": f"row-{index:03d}"} for index in range(count)]


# --------------------------------------------------------------------------
# AC-41 (unit): the result set is returned whole
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_the_result_set_is_not_cut_by_the_fallback() -> None:
    out = _truncate({"data": _rows()})
    assert len(out["data"]) == ROWS
    assert not any("truncated_items" in row for row in out["data"])


@pytest.mark.unit
def test_nothing_about_the_result_set_suggests_truncation() -> None:
    """No marker anywhere in the result set, at any depth.

    The marker is the *only* thing that distinguishes "20 rows exist" from "20
    of them were shown", so its presence is the assertion that matters here.
    """
    out = _truncate({"data": _rows(), "total": ROWS, "next_cursor": None})
    assert "truncated_items" not in str(out)


@pytest.mark.unit
def test_a_result_set_shorter_than_the_fallback_is_also_untouched() -> None:
    out = _truncate({"data": _rows(3)})
    assert [row["id"] for row in out["data"]] == ["row-000", "row-001", "row-002"]


# --------------------------------------------------------------------------
# AC-42: the fallback still protects everything else
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_a_nested_array_is_still_capped_and_says_so() -> None:
    """A nested list that grows unexpectedly is still bounded, and marked.

    This is the fallback's actual job, and it must survive making the result
    set exempt: a wrapper that simply stopped truncating would pass the result
    set tests above while removing the protection everywhere.
    """
    payload = {"data": [{"id": "only"}], "meta": {"things": _rows()}}

    out = _truncate(payload)

    # The result set is untouched...
    assert len(out["data"]) == 1
    # ...while the nested array is capped, and admits it.
    nested = out["meta"]["things"]
    assert len(nested) == MAX_LIST_ITEMS + 1, len(nested)
    assert nested[-1] == {"truncated_items": ROWS - MAX_LIST_ITEMS}


@pytest.mark.unit
def test_an_array_inside_a_row_is_still_capped() -> None:
    payload = {"data": [{"id": "row", "features": [f"f{i}" for i in range(ROWS)]}]}

    out = _truncate(payload)

    assert len(out["data"]) == 1
    features = out["data"][0]["features"]
    assert len(features) == MAX_LIST_ITEMS + 1
    assert features[-1] == {"truncated_items": ROWS - MAX_LIST_ITEMS}


@pytest.mark.unit
def test_a_non_data_list_at_the_top_level_is_still_capped() -> None:
    """`results` (a write's per-item rows) keeps the bound.

    Exempting every top-level list would let a large submission echo itself
    back; only the caller-chosen page is exempt.
    """
    out = _truncate({"submitted": ROWS, "inserted": ROWS, "results": _rows()})

    assert len(out["results"]) == MAX_LIST_ITEMS + 1
    assert out["results"][-1] == {"truncated_items": ROWS - MAX_LIST_ITEMS}


@pytest.mark.unit
def test_a_nested_data_array_is_still_capped() -> None:
    """The exemption is the top level only, not "any key called data"."""
    payload = {"data": [{"id": "row", "data": _rows()}]}

    out = _truncate(payload)

    nested = out["data"][0]["data"]
    assert len(nested) == MAX_LIST_ITEMS + 1
    assert nested[-1] == {"truncated_items": ROWS - MAX_LIST_ITEMS}


# --------------------------------------------------------------------------
# The bound still applies to what the exemption covers, via `_shrink`
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_scalars_are_still_bounded_by_the_string_ceiling() -> None:
    """Exempting the result set must not exempt the strings inside it.

    A long body or title in a row is still cut to MAX_STRING_CHARS, which is
    the other half of the context ceiling.
    """
    from app.mcp.rendering import MAX_STRING_CHARS

    out = _truncate({"data": [{"title": "x" * (MAX_STRING_CHARS + 50)}]})
    title = out["data"][0]["title"]
    assert len(title) > MAX_STRING_CHARS  # includes the "…(N chars)" suffix
    assert title.startswith("x" * MAX_STRING_CHARS)
    assert "chars)" in title


@pytest.mark.unit
def test_shrink_still_caps_a_bare_list() -> None:
    """`_shrink` is the recursion; called on a list it still applies the bound.

    The exemption lives in the top-level pass (`_truncate`), so the recursive
    entry point keeps its unconditional behaviour and cannot be leaned on to
    bypass it.
    """
    out = _shrink(_rows())
    assert isinstance(out, list)
    assert len(out) == MAX_LIST_ITEMS + 1
