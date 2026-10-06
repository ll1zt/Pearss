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
from core.normalize import DedupeAction, decide_dedupe

TENANT = "default"


def item(
    *,
    item_id: str = "i1",
    source_id: str = "src-1",
    content_hash: str = "hash-a",
    status: PublishStatus = PublishStatus.PENDING,
    guid: str | None = None,
) -> Item:
    return Item(
        id=item_id,
        origin_id=source_id,
        guid=guid if guid is not None else f"guid-{item_id}",
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
    # Distinct ids as well as distinct hashes: sharing an id would make these
    # the same entry, which is the case the identity check exists for.
    decision = decide_dedupe(
        item(item_id="fresh", content_hash="new"),
        [item(item_id="taken", content_hash="other")],
    )
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
def test_published_twin_governs_even_when_listed_second() -> None:
    # With one ready and one published copy of the same content, the published
    # one decides the outcome regardless of the order the rows arrived in — a
    # database-order dependence here would make suppress-vs-related random.
    existing = [
        item(
            item_id="i1",
            source_id="src-1",
            content_hash="hash-a",
            status=PublishStatus.READY,
        ),
        item(
            item_id="i2",
            source_id="src-2",
            content_hash="hash-a",
            status=PublishStatus.PUBLISHED,
        ),
    ]
    decision = decide_dedupe(
        item(item_id="i3", source_id="src-3", content_hash="hash-a"), existing
    )
    assert decision.action is DedupeAction.SUPPRESS
    assert decision.matched_id == "i2"


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
def test_same_url_with_different_content_is_not_a_duplicate() -> None:
    # The URL is not the dedupe key: a corrected or updated body is a new
    # article at the same address. Asserted on the decision rather than on a
    # predicate, because the decision is the thing that runs (issue #87).
    one = item(content_hash="hash-a")
    other = item(item_id="i2", content_hash="hash-b")
    assert one.url == other.url
    assert decide_dedupe(other, [one]).matched_id is None


@pytest.mark.unit
def test_changed_content_while_pending_updates_the_row() -> None:
    stored = item(item_id="i1", content_hash="h-a", status=PublishStatus.PENDING)
    # Same id and same guid, different body: the entry moved under us.
    incoming = item(
        item_id="i1",
        content_hash="h-b",
        status=PublishStatus.PENDING,
        guid=stored.guid,
    )
    decision = decide_dedupe(incoming, [stored])
    assert decision.action is DedupeAction.UPDATE_PENDING
    assert decision.matched_id == "i1"


@pytest.mark.unit
def test_changed_content_after_publication_is_left_alone() -> None:
    # Neither rewriting (clients cached the old) nor inserting (the guid is
    # frozen and unique) is available.
    stored = item(item_id="i1", content_hash="h-a", status=PublishStatus.PUBLISHED)
    incoming = item(
        item_id="i1",
        content_hash="h-b",
        status=PublishStatus.PENDING,
        guid=stored.guid,
    )
    decision = decide_dedupe(incoming, [stored])
    assert decision.action is DedupeAction.SKIP_PUBLISHED_CONTENT_CHANGED
    assert "frozen" in decision.reason


@pytest.mark.unit
def test_identical_re_fetch_still_skips_even_though_identity_matches_first() -> None:
    stored = item(item_id="i1", content_hash="h-a", status=PublishStatus.READY)
    incoming = item(
        item_id="i1",
        content_hash="h-a",
        status=PublishStatus.PENDING,
        guid=stored.guid,
    )
    assert decide_dedupe(incoming, [stored]).action is DedupeAction.SKIP_SAME_SOURCE


@pytest.mark.unit
def test_identity_match_takes_precedence_over_a_cross_source_content_match() -> None:
    # Two rows share the content hash, but the candidate IS one of them by
    # identity. Reporting the cross-source relation would insert a guid
    # collision.
    same_entry = item(item_id="i1", content_hash="h-a", status=PublishStatus.PUBLISHED)
    twin = item(item_id="i2", source_id="src-2", content_hash="h-a")
    incoming = item(
        item_id="i1",
        content_hash="h-a",
        status=PublishStatus.PENDING,
        guid=same_entry.guid,
    )
    decision = decide_dedupe(incoming, [twin, same_entry])
    assert decision.action is DedupeAction.SKIP_SAME_SOURCE


# --------------------------------------------------------------------------
# Pins added after the mutation baseline (feat/test-driven-hardening)
#
# `just mutate` showed the identity branch and the matched_id carry below
# surviving: the `or` in the identity predicate is defensive (id and guid
# derive from the same triple today, and the `or` keeps the door open if
# they ever fork), and matched_id feeds the audit trail. Both now have a
# test that fails if the code stops honouring them.
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_identity_matches_on_guid_alone_not_only_on_both() -> None:
    """The identity predicate is an `or`, deliberately.

    Today id and guid derive from the same (tenant, source, external_id)
    triple, so `or` and `and` agree; if the two derivations ever fork, `and`
    would let a same-guid entry through to the content check and collide on
    insert. This test constructs the fork directly so the defensive branch
    stays pinned rather than relying on the derivations staying coupled.
    """
    incoming = item(item_id="fresh-id", guid="shared-guid", content_hash="new")
    stored = item(item_id="other-id", guid="shared-guid", content_hash="different")

    decision = decide_dedupe(incoming, [stored])

    assert decision.action is not DedupeAction.INSERT
    assert decision.matched_id == "other-id"


@pytest.mark.unit
def test_identity_match_by_internal_id_alone() -> None:
    incoming = item(item_id="same-id", guid="guid-incoming", content_hash="new")
    stored = item(item_id="same-id", guid="guid-stored", content_hash="different")

    decision = decide_dedupe(incoming, [stored])

    assert decision.action is DedupeAction.UPDATE_PENDING
    assert decision.matched_id == "same-id"


@pytest.mark.unit
def test_every_non_insert_decision_names_the_item_it_matched() -> None:
    """matched_id is the audit trail's pointer; None would point nowhere."""
    pending_twin = item(item_id="twin", source_id="src-other", content_hash="same")
    published_twin = item(
        item_id="pub",
        source_id="src-other",
        content_hash="same",
        status=PublishStatus.PUBLISHED,
    )
    same_entry = item(item_id="same", guid="g-same", content_hash="changed")

    decisions = [
        decide_dedupe(item(content_hash="same"), [pending_twin]),
        decide_dedupe(item(content_hash="same"), [published_twin]),
        decide_dedupe(item(content_hash="same"), [item(content_hash="same")]),
        decide_dedupe(
            same_entry, [item(item_id="same", guid="g-same", content_hash="old")]
        ),
    ]

    for decision in decisions:
        assert decision.action is not DedupeAction.INSERT
        assert decision.matched_id is not None, decision.reason


# --------------------------------------------------------------------------
# matched_id, branch by branch
#
# The check above is a loop over decisions, and that shape hides the failure it
# is meant to catch: `is not None` is satisfied if *any* decision in the list
# carries an id, so a branch returning None passes as long as another does not.
# These assert the exact id for each action instead, which is what the audit
# trail actually needs -- a pointer to the row that caused the decision, not
# merely some row.
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_skip_published_content_changed_points_at_the_published_twin() -> None:
    """The branch the loop above could not see.

    Same identity, changed body, already published: the decision is to leave
    the stored row alone, and the audit trail must name *that* row. Returning
    None here passed the loop because other decisions in the same list were
    populated, so nothing distinguished this branch from one that lost its
    pointer.
    """
    same_entry = item(item_id="same", guid="g-same", content_hash="changed")
    stored = item(
        item_id="same",
        guid="g-same",
        content_hash="old",
        status=PublishStatus.PUBLISHED,
    )

    decision = decide_dedupe(same_entry, [stored])

    assert decision.action is DedupeAction.SKIP_PUBLISHED_CONTENT_CHANGED
    assert decision.matched_id == "same"


@pytest.mark.unit
def test_skip_same_source_points_at_the_stored_row() -> None:
    stored = item(item_id="stored", content_hash="same")

    decision = decide_dedupe(item(item_id="stored", content_hash="same"), [stored])

    assert decision.action is DedupeAction.SKIP_SAME_SOURCE
    assert decision.matched_id == "stored"


@pytest.mark.unit
def test_update_pending_points_at_the_stored_row() -> None:
    same_entry = item(item_id="same", guid="g-same", content_hash="changed")
    stored = item(item_id="same", guid="g-same", content_hash="old")

    decision = decide_dedupe(same_entry, [stored])

    assert decision.action is DedupeAction.UPDATE_PENDING
    assert decision.matched_id == "same"


@pytest.mark.unit
def test_insert_related_points_at_the_twin_it_was_related_to() -> None:
    twin = item(item_id="twin", source_id="src-other", content_hash="same")

    decision = decide_dedupe(item(item_id="mine", content_hash="same"), [twin])

    assert decision.action is DedupeAction.INSERT_RELATED
    assert decision.matched_id == "twin"


@pytest.mark.unit
def test_suppress_points_at_the_published_twin() -> None:
    published = item(
        item_id="pub",
        source_id="src-other",
        content_hash="same",
        status=PublishStatus.PUBLISHED,
    )

    decision = decide_dedupe(item(item_id="mine", content_hash="same"), [published])

    assert decision.action is DedupeAction.SUPPRESS
    assert decision.matched_id == "pub"


@pytest.mark.unit
def test_an_insert_has_no_matched_id() -> None:
    """Nothing matched, so the field stays empty.

    The counterpart to the assertions above: a mutant that populated
    matched_id on the insert path would invent an audit pointer to a row that
    had nothing to do with the decision.
    """
    decision = decide_dedupe(item(content_hash="new"), [])

    assert decision.action is DedupeAction.INSERT
    assert decision.matched_id is None


@pytest.mark.unit
def test_every_action_names_itself_in_the_reason() -> None:
    """Each action's reason is a distinct, non-empty explanation.

    The reason is what an operator reads when auditing why an item was
    suppressed. It is not required to be any particular wording, but an empty
    or shared reason would make the six actions indistinguishable in the audit
    log, which is the one thing data-model.md says the rule must not be.
    """
    pending_twin = item(item_id="twin", source_id="src-other", content_hash="same")
    published_twin = item(
        item_id="pub",
        source_id="src-other",
        content_hash="same",
        status=PublishStatus.PUBLISHED,
    )
    same_entry = item(item_id="same", guid="g-same", content_hash="changed")

    decisions = [
        decide_dedupe(item(content_hash="new"), []),
        decide_dedupe(
            item(item_id="s", content_hash="same"),
            [item(item_id="s", content_hash="same")],
        ),
        decide_dedupe(item(content_hash="same"), [pending_twin]),
        decide_dedupe(item(content_hash="same"), [published_twin]),
        decide_dedupe(
            same_entry, [item(item_id="same", guid="g-same", content_hash="old")]
        ),
        decide_dedupe(
            same_entry,
            [
                item(
                    item_id="same",
                    guid="g-same",
                    content_hash="old",
                    status=PublishStatus.PUBLISHED,
                )
            ],
        ),
    ]

    reasons = [decision.reason for decision in decisions]
    assert all(reason for reason in reasons), reasons
    assert len(set(reasons)) == len(reasons), reasons
