"""Unit-layer coverage for the batch fingerprint used by submit_items.

The scenarios themselves are registered in
`tests/integration/test_submit_items_confirmation.py` (AC-44..AC-46, issue #67);
this file pins the pure function and the resolver wiring they rest on, at the
fast tier. No new AC numbers are claimed here.

Why the fingerprint exists at all, and not just a count comparison: the form
lists titles, because a body can be arbitrarily long and a prompt the user
cannot read is not a confirmation. So a batch with the same titles and the same
count but different bodies passes every check the old implementation had.
Measured (issue #67): with the framework's request-state binding removed, that
batch's second round **really wrote to the database** (three drifted bodies
landed). The fingerprint is the local guard for that layer.

Titles alone would not close it -- these batches are identical to the form --
which is why the tests below fix the titles and vary only the bodies.
"""

from __future__ import annotations

import pytest

from app.mcp.confirmation import (
    _items_fingerprint,
    _submit_items_confirmation,
)

pytestmark = pytest.mark.unit


def _batch(*bodies: str) -> list[dict[str, str]]:
    """A batch whose titles are fixed and whose bodies vary.

    Fixed titles are the point: the confirmation form shows only titles, so a
    fingerprint that keys off titles would see these batches as identical.
    """
    return [
        {
            "external_id": f"e{index}",
            "title": f"T{index}",
            "canonical_url": f"https://f.example/{index}",
            "content": body,
        }
        for index, body in enumerate(bodies)
    ]


# --------------------------------------------------------------------------
# AC-44: content drift changes the fingerprint
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_a_body_only_change_produces_a_different_fingerprint() -> None:
    """The measured gap: identical titles, identical count, different bodies."""
    confirmed = _batch("<p>one</p>", "<p>two</p>")
    drifted = _batch("<p>EVIL</p>", "<p>EVIL</p>")

    # The premise: these two batches are indistinguishable to the form.
    assert [item["title"] for item in confirmed] == [item["title"] for item in drifted]
    assert len(confirmed) == len(drifted)

    assert _items_fingerprint(confirmed) != _items_fingerprint(drifted)


@pytest.mark.unit
def test_the_fingerprint_tells_two_batches_apart_in_the_confirmation() -> None:
    """The batches that look identical by title are different requests.

    And they are shown as different ones.

    Before #250 the fingerprint was the value the *client* echoed back and
    `_matches` refused the drifted batch. The default path has no field to echo
    any more (issue #250), so there is nothing to refuse: what the fingerprint
    has to do now is make the two batches *visible* as different requests,
    because the user cannot read the bodies.
    """
    confirmed = _batch("<p>one</p>", "<p>two</p>")
    drifted = _batch("<p>EVIL</p>", "<p>EVIL</p>")

    marker = _submit_items_confirmation("src-1", confirmed)
    other = _submit_items_confirmation("src-1", drifted)

    assert marker.fields["items_fingerprint"] == _items_fingerprint(confirmed)
    assert marker.fields["items_fingerprint"] != other.fields["items_fingerprint"]
    assert marker.message != other.message, (
        "two batches whose bodies differ must not read as the same request"
    )


# --------------------------------------------------------------------------
# AC-45: determinism (or every confirmation would be refused as drift)
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_the_same_batch_always_produces_the_same_fingerprint() -> None:
    batch = _batch("<p>one</p>", "<p>two</p>")
    assert _items_fingerprint(batch) == _items_fingerprint(
        [dict(item) for item in batch]
    )
    # And across a JSON round trip, which is how the batch actually travels.
    import json

    round_tripped = json.loads(json.dumps(batch))
    assert _items_fingerprint(round_tripped) == _items_fingerprint(batch)


# --------------------------------------------------------------------------
# AC-46: order, and key order inside an item
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_reordering_the_batch_changes_the_fingerprint() -> None:
    """A permutation is a different request, on purpose.

    The user was shown the titles in an order and the response reports results
    in that order, so treating a permutation as the same request would be the
    wrong licence.
    """
    ordered = _batch("<p>a</p>", "<p>b</p>", "<p>c</p>")
    shuffled = [ordered[2], ordered[0], ordered[1]]
    assert _items_fingerprint(ordered) != _items_fingerprint(shuffled)


@pytest.mark.unit
def test_key_order_inside_an_item_does_not_change_the_fingerprint() -> None:
    """Key order within an item is not a difference; only list order is.

    `sort_keys` canonicalises the dicts. Without it, the same item rendered by
    two clients (or two JSON encoders) would look like drift.
    """
    one = [{"title": "T", "content": "<p>x</p>", "external_id": "e"}]
    other = [{"external_id": "e", "content": "<p>x</p>", "title": "T"}]
    assert _items_fingerprint(one) == _items_fingerprint(other)


@pytest.mark.unit
def test_the_confirmation_carries_both_the_human_titles_and_the_fingerprint() -> None:
    """The form stays readable, and the machine check rides alongside it.

    `titles` is for the user and is deliberately truncated to the first five;
    the fingerprint is the part that covers every item and every field. Losing
    either would be a regression: titles alone cannot see bodies, and a digest
    alone is not a confirmation anyone can read.
    """
    batch = _batch("<p>one</p>", "<p>two</p>")
    marker = _submit_items_confirmation("src-1", batch)

    assert marker.fields["titles"] == "T0; T1"
    assert marker.fields["items_fingerprint"] == _items_fingerprint(batch)
    # The count remains, as the cheap first gate.
    assert marker.fields["item_count"] == "2"
    # And the two that the digest exists for are in the line the user reads:
    # #250 moved the visible surface from the schema's fields to the summary.
    assert "T0; T1" in marker.message
    assert _items_fingerprint(batch) in marker.message
