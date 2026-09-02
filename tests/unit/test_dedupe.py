"""Tests for dedupe decisions.

docs/domain/data-model.md requires that items sharing a content_hash be
merged, related or suppressed by an explicit rule — silent duplicate
publication is forbidden. These tests pin each branch of that rule.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core.enums import PublishStatus
from core.models import Item
from core.normalize import DedupeAction, decide_dedupe, is_duplicate_of

TENANT = "default"


def item(
    *,
    item_id: str = "i1",
    source_id: str = "src-1",
    content_hash: str = "hash-a",
    status: PublishStatus = PublishStatus.PENDING,
) -> Item:
    return Item(
        id=item_id,
        source_id=source_id,
        guid=f"guid-{item_id}",
        content_hash=content_hash,
        title="t",
        url="https://example.com/a",
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
        publish_status=status,
        tenant_id=TENANT,
    )


@pytest.mark.unit
def test_unseen_content_is_inserted() -> None:
    decision = decide_dedupe(item(content_hash="new"), [])
    assert decision.action is DedupeAction.INSERT
    assert decision.matched_id is None


@pytest.mark.unit
def test_unrelated_items_do_not_interfere() -> None:
    decision = decide_dedupe(item(content_hash="new"), [item(content_hash="other")])
    assert decision.action is DedupeAction.INSERT


@pytest.mark.unit
def test_re_fetch_from_same_source_is_skipped() -> None:
    existing = item(item_id="i1", content_hash="hash-a")
    decision = decide_dedupe(item(item_id="i2", content_hash="hash-a"), [existing])
    assert decision.action is DedupeAction.SKIP_SAME_SOURCE
    assert decision.matched_id == "i1"


@pytest.mark.unit
def test_same_content_from_another_source_is_related() -> None:
    existing = item(item_id="i1", source_id="src-1", content_hash="hash-a")
    decision = decide_dedupe(
        item(item_id="i2", source_id="src-2", content_hash="hash-a"), [existing]
    )
    assert decision.action is DedupeAction.INSERT_RELATED
    assert decision.matched_id == "i1"


@pytest.mark.unit
def test_suppress_when_the_twin_is_already_published() -> None:
    # The reader has already been shown this article. Publishing our copy under
    # a different GUID is the silent duplicate the model forbids.
    existing = item(
        item_id="i1",
        source_id="src-1",
        content_hash="hash-a",
        status=PublishStatus.PUBLISHED,
    )
    decision = decide_dedupe(
        item(item_id="i2", source_id="src-2", content_hash="hash-a"), [existing]
    )
    assert decision.action is DedupeAction.SUPPRESS
    assert decision.matched_id == "i1"


@pytest.mark.unit
def test_ready_twin_is_related_not_suppressed() -> None:
    # Not yet shown to any client, so recording the relationship is still safe.
    existing = item(
        item_id="i1",
        source_id="src-1",
        content_hash="hash-a",
        status=PublishStatus.READY,
    )
    decision = decide_dedupe(
        item(item_id="i2", source_id="src-2", content_hash="hash-a"), [existing]
    )
    assert decision.action is DedupeAction.INSERT_RELATED


@pytest.mark.unit
def test_same_source_takes_precedence_over_published_twin() -> None:
    # A re-fetch of the row we already have is a skip, regardless of what other
    # sources have done with the same content.
    existing = [
        item(
            item_id="i1",
            source_id="src-1",
            content_hash="hash-a",
            status=PublishStatus.PUBLISHED,
        ),
        item(
            item_id="i2",
            source_id="src-2",
            content_hash="hash-a",
            status=PublishStatus.PUBLISHED,
        ),
    ]
    decision = decide_dedupe(
        item(item_id="i3", source_id="src-1", content_hash="hash-a"), existing
    )
    assert decision.action is DedupeAction.SKIP_SAME_SOURCE


@pytest.mark.unit
def test_every_decision_carries_a_reason() -> None:
    # The reason is what makes the rule auditable rather than implicit.
    for decision in (
        decide_dedupe(item(content_hash="new"), []),
        decide_dedupe(item(content_hash="hash-a"), [item(content_hash="hash-a")]),
    ):
        assert decision.reason


@pytest.mark.unit
def test_duplicate_means_same_content_hash() -> None:
    assert is_duplicate_of(item(content_hash="same"), item(content_hash="same"))


@pytest.mark.unit
def test_same_url_with_different_content_is_not_a_duplicate() -> None:
    # The URL is not the dedupe key: a corrected or updated body is a new
    # article at the same address.
    one = item(content_hash="hash-a")
    other = item(item_id="i2", content_hash="hash-b")
    assert one.url == other.url
    assert not is_duplicate_of(one, other)


@pytest.mark.unit
def test_different_content_is_not_a_duplicate() -> None:
    assert not is_duplicate_of(item(content_hash="a"), item(content_hash="b"))
