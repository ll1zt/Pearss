"""Tests for draft normalisation and identity derivation.

The values decided here — guid, id, content_hash — are frozen once an item is
published, because an RSS client caches what it fetched and cannot be shown a
correction. That irreversibility is why these tests pin determinism so
precisely.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from core.enums import PublishStatus
from core.models import Item, ItemDraft
from core.normalize import (
    NormalizationResult,
    content_hash,
    derive_guid,
    derive_item_id,
    normalize,
    origin_display_label,
    plain_text,
)

TENANT = "default"
SOURCE = "src-1"


def draft(**overrides: object) -> ItemDraft:
    """A valid draft, customised by keyword."""
    defaults: dict[str, object] = {
        "origin_id": SOURCE,
        "external_id": "entry-1",
        "title": "A real headline",
        "canonical_url": "https://example.com/2026/entry-1",
        "published_at": datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        "content": "<p>Body text of the article.</p>",
    }
    defaults.update(overrides)
    return ItemDraft(**defaults)  # type: ignore[arg-type]


@pytest.mark.unit
def test_normalize_produces_a_pending_item() -> None:
    result = normalize(draft(), tenant_id=TENANT)
    # The gate is closed on arrival; advancing depends on the processing chain.
    assert result.item.publish_status is PublishStatus.PENDING
    assert result.item.published_to_feed_at is None


@pytest.mark.unit
def test_normalize_canonicalizes_the_url() -> None:
    result = normalize(
        draft(canonical_url="https://example.com/a?utm_source=x&id=1#f"),
        tenant_id=TENANT,
    )
    assert result.item.url == "https://example.com/a?id=1"


@pytest.mark.unit
def test_identity_is_deterministic() -> None:
    first = normalize(draft(), tenant_id=TENANT)
    second = normalize(draft(), tenant_id=TENANT)
    # Re-fetching must not mint a new identity, or the same article would be
    # published twice.
    assert first.item.id == second.item.id
    assert first.item.guid == second.item.guid


@pytest.mark.unit
def test_identity_depends_on_external_id() -> None:
    one = normalize(draft(external_id="a"), tenant_id=TENANT)
    other = normalize(draft(external_id="b"), tenant_id=TENANT)
    assert one.item.guid != other.item.guid


@pytest.mark.unit
def test_identity_is_scoped_to_tenant() -> None:
    one = normalize(draft(), tenant_id="tenant-a")
    other = normalize(draft(), tenant_id="tenant-b")
    assert one.item.guid != other.item.guid


@pytest.mark.unit
def test_identity_is_scoped_to_source() -> None:
    one = normalize(draft(origin_id="src-1"), tenant_id=TENANT)
    other = normalize(draft(origin_id="src-2"), tenant_id=TENANT)
    assert one.item.guid != other.item.guid


@pytest.mark.unit
def test_id_and_guid_are_distinct_namespaces() -> None:
    # Same inputs, different derivations: sharing a value would couple the
    # public GUID to internal row identity.
    assert derive_item_id(
        tenant_id=TENANT, origin_id=SOURCE, external_id="x"
    ) != derive_guid(tenant_id=TENANT, origin_id=SOURCE, external_id="x")


@pytest.mark.unit
def test_guid_survives_a_database_rebuild() -> None:
    # A randomly generated GUID would change, and NNW would show the reader a
    # duplicate instead of an update.
    guid = normalize(draft(), tenant_id=TENANT).item.guid
    assert guid == normalize(draft(), tenant_id=TENANT).item.guid


# --------------------------------------------------------------------------
# Golden identity values
#
# The tests above assert *relations*: deterministic, tenant-scoped, origin-
# scoped, distinct namespaces. Every one of them still holds if a field is
# dropped from the digest input, because dropping a field preserves all four
# properties at once -- so a mutant that removes `origin_id` from
# `derive_item_id` satisfies the entire relational suite while colliding two
# different origins' entries onto one row.
#
# These tests pin the exact digest instead. They are the only assertions that
# fail when the derivation's *inputs* change rather than its shape.
#
# A failure here is not a bug to fix in the test. It means every stored item id
# and every published GUID changes, so the change needs a data migration and,
# for the GUID, a client-visible duplicate the reader cannot be told about.
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_the_item_id_digest_is_pinned() -> None:
    assert (
        derive_item_id(tenant_id="default", origin_id="src-1", external_id="entry-42")
        == "7eb676ac40e283e7b68b72c240e4ad74"
    )


@pytest.mark.unit
def test_the_guid_digest_is_pinned() -> None:
    assert (
        derive_guid(tenant_id="default", origin_id="src-1", external_id="entry-42")
        == "dcf8478f6cb2d0eb959f5a9e06aee5c6"
    )


@pytest.mark.unit
def test_the_content_hash_digest_is_pinned() -> None:
    # Pinned through the real entry point, so a change to either the scheme
    # tag or the normalisation that feeds it shows up here.
    assert (
        content_hash(title="Hello  World", content="<p>Body &amp; more</p>")
        == "dcf2e2501747ccd5e7fb20f07fdb7eb9"
    )


@pytest.mark.unit
def test_every_identity_input_is_load_bearing() -> None:
    """Each of the three inputs must change the digest on its own.

    `origin_id` is the one this catches: it is absent from the id's *visible*
    behaviour (nothing else keys on it) and its removal still leaves the
    relational suite green, but two origins sharing an external_id would then
    produce one id and one item would silently overwrite the other.
    """
    base = derive_item_id(tenant_id="t", origin_id="s", external_id="e")
    assert derive_item_id(tenant_id="t2", origin_id="s", external_id="e") != base
    assert derive_item_id(tenant_id="t", origin_id="s2", external_id="e") != base
    assert derive_item_id(tenant_id="t", origin_id="s", external_id="e2") != base


@pytest.mark.unit
def test_the_scheme_tag_separates_old_and_new_derivations() -> None:
    """The scheme string is part of the digest input, not decoration.

    It is what lets a deliberately changed derivation be rolled out beside the
    old one instead of colliding with it, so the constant must actually reach
    the hash. Asserted by recomputing what the digest would be if the tag were
    dropped or altered: a test that only compared two real digests would pass
    whether or not the tag was involved.
    """
    import hashlib

    payload = "\x1f".join(("id1", "item", "default", "src-1", "entry-42"))
    if_the_tag_were_dropped = hashlib.sha256(
        "\x1f".join(("item", "default", "src-1", "entry-42")).encode()
    ).hexdigest()[:32]

    assert (
        derive_item_id(tenant_id="default", origin_id="src-1", external_id="entry-42")
        == hashlib.sha256(payload.encode()).hexdigest()[:32]
    )
    assert (
        derive_item_id(tenant_id="default", origin_id="src-1", external_id="entry-42")
        != if_the_tag_were_dropped
    )


@pytest.mark.unit
def test_the_digest_never_splits_an_input() -> None:
    """The separator must make the field boundaries unambiguous.

    Joining the inputs with no separator (or a separator that can appear inside
    one) would make {("ab","c")} and {("a","bc")} hash the same, so two
    genuinely different items could collide.
    """
    assert derive_item_id(tenant_id="t", origin_id="ab", external_id="c") != (
        derive_item_id(tenant_id="t", origin_id="a", external_id="bc")
    )


@pytest.mark.unit
def test_content_hash_ignores_source_and_url() -> None:
    # Cross-source dedupe (B2) only works if the hash covers just the article.
    one = draft(origin_id="src-1", canonical_url="https://a.example/1")
    other = draft(origin_id="src-2", canonical_url="https://b.example/2")
    assert (
        normalize(one, tenant_id=TENANT).item.content_hash
        == normalize(other, tenant_id=TENANT).item.content_hash
    )


@pytest.mark.unit
def test_content_hash_ignores_markup_differences() -> None:
    plain = draft(content="Body text of the article.")
    marked = draft(content="<p>Body <em>text</em> of the article.</p>")
    assert (
        normalize(plain, tenant_id=TENANT).item.content_hash
        == normalize(marked, tenant_id=TENANT).item.content_hash
    )


@pytest.mark.unit
def test_content_hash_ignores_case_and_whitespace() -> None:
    lower = draft(title="headline", content="body")
    noisy = draft(title="  HEADLINE  ", content="BODY\n\n")
    assert (
        normalize(lower, tenant_id=TENANT).item.content_hash
        == normalize(noisy, tenant_id=TENANT).item.content_hash
    )


@pytest.mark.unit
def test_content_hash_changes_with_the_body() -> None:
    assert content_hash(title="t", content="a") != content_hash(title="t", content="b")


@pytest.mark.unit
def test_title_is_normalised() -> None:
    result = normalize(draft(title="  Spaced   Out  "), tenant_id=TENANT)
    assert result.item.title == "Spaced Out"
    assert not result.title_derived


@pytest.mark.unit
def test_missing_title_is_derived_and_flagged() -> None:
    # A derived title must be marked, not silently passed off as the source's.
    result = normalize(draft(title=""), tenant_id=TENANT)
    assert result.item.title
    assert result.title_derived


@pytest.mark.unit
def test_derived_title_is_truncated() -> None:
    result = normalize(draft(title="", content="word " * 100), tenant_id=TENANT)
    assert len(result.item.title) <= 82
    assert result.title_derived


@pytest.mark.unit
def test_untitled_when_there_is_nothing_to_derive_from() -> None:
    result = normalize(draft(title="", content=""), tenant_id=TENANT)
    assert result.item.title == "Untitled"
    assert result.title_derived


@pytest.mark.unit
def test_body_keeps_the_source_markup() -> None:
    # The body is what a reader displays, so links and formatting must survive.
    # Flattening it here would make the CDATA passthrough in the RSS output
    # pointless, since there would be no markup left to preserve.
    html = '<p>One</p><p>Two <a href="https://e.com/x">link</a></p>'
    result = normalize(draft(content=html), tenant_id=TENANT)
    assert result.body == html


@pytest.mark.unit
def test_dedupe_is_unaffected_by_markup_differences() -> None:
    # The counterparty to keeping markup: two sources carrying the same article
    # with different HTML must still hash identically, or cross-source dedupe
    # (B2) silently stops working.
    plain = draft(content="Same body here.")
    marked = draft(content="<p>Same   body</p> <p>here.</p>")
    assert (
        normalize(plain, tenant_id=TENANT).item.content_hash
        == normalize(marked, tenant_id=TENANT).item.content_hash
    )


@pytest.mark.unit
def test_empty_content_is_distinguished_from_unrecoverable_content() -> None:
    empty = normalize(draft(content=""), tenant_id=TENANT)
    # Markup with no text means the body did not survive cleaning, which is a
    # different problem from a source that legitimately has no body.
    unrecoverable = normalize(draft(content="<img src='x.png'>"), tenant_id=TENANT)
    assert not empty.content_missing
    assert unrecoverable.content_missing


@pytest.mark.unit
def test_naive_published_at_is_treated_as_utc() -> None:
    naive = datetime(2026, 6, 1, 10, 0)  # no zone
    result = normalize(draft(published_at=naive), tenant_id=TENANT)
    assert result.item.published_at is not None
    assert result.item.published_at.tzinfo is not None
    assert result.item.published_at == naive.replace(tzinfo=UTC)


@pytest.mark.unit
def test_naive_published_at_is_not_shifted_by_local_time() -> None:
    # Regression: astimezone() on a naive datetime interprets it as *local*
    # time and shifts it, so 10:00 stored on a UTC+8 host became 02:00. That
    # misdates every article whose feed omits a zone, which is common.
    naive = datetime(2026, 6, 1, 10, 0)
    result = normalize(draft(published_at=naive), tenant_id=TENANT)
    assert result.item.published_at == datetime(2026, 6, 1, 10, 0, tzinfo=UTC)
    assert result.item.published_at is not None
    assert result.item.published_at.hour == 10


@pytest.mark.unit
def test_offset_published_at_is_converted_to_utc() -> None:
    tokyo = datetime(2026, 6, 1, 10, 0, tzinfo=timezone(timedelta(hours=9)))
    result = normalize(draft(published_at=tokyo), tenant_id=TENANT)
    assert result.item.published_at == tokyo.astimezone(UTC)


@pytest.mark.unit
def test_missing_published_at_stays_none() -> None:
    # Inventing a timestamp would reorder the feed and misdate the article.
    assert (
        normalize(draft(published_at=None), tenant_id=TENANT).item.published_at is None
    )


@pytest.mark.unit
def test_content_ref_is_attached_when_supplied() -> None:
    result = normalize(draft(), tenant_id=TENANT, content_ref="blob://abc")
    assert result.item.content_ref == "blob://abc"


@pytest.mark.unit
def test_now_sets_both_timestamps() -> None:
    stamp = datetime(2026, 3, 1, tzinfo=UTC)
    result = normalize(draft(), tenant_id=TENANT, now=stamp)
    assert result.item.created_at == stamp
    assert result.item.updated_at == stamp


@pytest.mark.unit
def test_naive_now_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        normalize(draft(), tenant_id=TENANT, now=datetime(2026, 3, 1))


@pytest.mark.unit
def test_empty_tenant_is_rejected() -> None:
    with pytest.raises(ValueError, match="tenant_id"):
        normalize(draft(), tenant_id="")


@pytest.mark.unit
def test_plain_text_strips_control_and_zero_width_characters() -> None:
    assert plain_text("a​b\x07c") == "abc"


@pytest.mark.unit
def test_result_carries_the_item_and_body(result: NormalizationResult) -> None:
    assert result.item.title == "A real headline"
    assert result.body


@pytest.fixture
def result() -> NormalizationResult:
    return normalize(draft(), tenant_id=TENANT)


@pytest.mark.unit
def test_item_rejects_naive_timestamps() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        Item(
            id="i",
            origin_id="s",
            guid="g",
            content_hash="h",
            title="t",
            url="u",
            published_at=datetime(2026, 1, 1),
            publish_status=PublishStatus.PENDING,
        )


# ---------------------------------------------------------------------------
# origin_display_label
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_display_label_prefers_the_recorded_title() -> None:
    assert (
        origin_display_label(
            title="Planet Python",
            url="https://planetpython.org/rss20.xml",
            canonical_url="https://planetpython.org/rss20.xml",
            origin_id="a" * 32,
        )
        == "Planet Python"
    )


@pytest.mark.unit
def test_display_label_uses_the_channel_for_telegram_urls() -> None:
    """Every Telegram channel lives on t.me, so the host names nothing."""
    for url in (
        "https://t.me/s/durov",
        "https://t.me/durov/546",
        "https://t.me/@durov",
    ):
        assert (
            origin_display_label(
                title=None, url=url, canonical_url=url, origin_id="b" * 32
            )
            == "durov"
        ), url


@pytest.mark.unit
def test_display_label_falls_back_to_the_host_otherwise() -> None:
    assert (
        origin_display_label(
            title=None,
            url="https://hnrss.org/frontpage",
            canonical_url="https://hnrss.org/frontpage",
            origin_id="c" * 32,
        )
        == "hnrss.org"
    )


@pytest.mark.unit
def test_display_label_last_resort_is_the_id() -> None:
    assert (
        origin_display_label(
            title=None, url=None, canonical_url=None, origin_id="d" * 32
        )
        == "d" * 32
    )
    assert (
        origin_display_label(
            title="   ", url="", canonical_url=None, origin_id="e" * 32
        )
        == "e" * 32
    )


@pytest.mark.unit
def test_author_comes_from_draft_metadata() -> None:
    """The byline the collector reported survives normalisation.

    Every collector already put it in `metadata["author"]`; normalize() used to
    read only the explicit draft fields and drop metadata entirely, so the
    value was fetched and discarded on every collection.
    """
    result = normalize(draft(metadata={"author": "Ada Example"}), tenant_id=TENANT)
    assert result.item.author == "Ada Example"


@pytest.mark.unit
def test_author_is_empty_when_the_origin_names_nobody() -> None:
    """No metadata, no byline -- and that is not an error.

    A feed without <author> is normal. The empty string is the honest answer;
    inventing the source name would misattribute the byline.
    """
    assert normalize(draft(), tenant_id=TENANT).item.author == ""
    assert normalize(draft(metadata={}), tenant_id=TENANT).item.author == ""


@pytest.mark.unit
def test_author_whitespace_is_collapsed() -> None:
    """Origins wrap long names; a byline with newlines breaks client layout."""
    result = normalize(
        draft(metadata={"author": "  Ada\n\tExample  "}), tenant_id=TENANT
    )
    assert result.item.author == "Ada Example"


@pytest.mark.unit
def test_author_does_not_affect_identity() -> None:
    """Identity is about which item this is, not who wrote it.

    If author fed into guid or content_hash, correcting a byline would mint a
    new item and the reader would show the article twice.
    """
    without = normalize(draft(), tenant_id=TENANT).item
    with_author = normalize(draft(metadata={"author": "Ada"}), tenant_id=TENANT).item
    assert without.id == with_author.id
    assert without.guid == with_author.guid
    assert without.content_hash == with_author.content_hash
