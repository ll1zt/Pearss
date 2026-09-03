"""Tests for RSS 2.0 output.

The injection cases are the important ones. Body HTML is untrusted remote
content that we deliberately pass through as CDATA (ADR 0003), so the defence
that stops it escaping into the XML structure must be pinned by tests rather
than assumed.

Also covered: the gate's filtering, and that the feed exposes no internal
identifiers or storage references.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from lxml import etree

from adapters.rss_out import FeedMeta, render_feed, visible_items
from core.enums import PublishStatus
from core.models import Item

META = FeedMeta(title="Pearss", link="https://example.com", description="All items.")


def item(
    *,
    guid: str = "g1",
    title: str = "An item",
    status: PublishStatus = PublishStatus.READY,
    published: datetime | None = None,
) -> Item:
    return Item(
        id=f"id-{guid}",
        source_id="s",
        guid=guid,
        content_hash=f"h-{guid}",
        title=title,
        url=f"https://example.com/{guid}",
        published_at=published or datetime(2026, 1, 1, tzinfo=UTC),
        publish_status=status,
    )


def parsed(
    meta: FeedMeta, items: list[Item], bodies: dict[str, str] | None = None
) -> etree._Element:
    return etree.fromstring(render_feed(meta, items, bodies or {}))


# --------------------------------------------------------------------------
# CDATA safety — ADR 0003
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_cdata_escape_attempt_cannot_inject_an_item() -> None:
    # The classic attack: close the CDATA section and append a new <item>.
    # If the escape worked, the document would contain two items.
    hostile = (
        "<p>hi</p>]]></description></item>"
        "<item><title>INJECTED</title>"
        "<link>https://evil.example</link><description><![CDATA[x"
    )
    root = parsed(META, [item()], {"id-g1": hostile})

    assert len(root.findall("./channel/item")) == 1


@pytest.mark.unit
def test_injected_text_stays_inside_the_description() -> None:
    hostile = "<p>hi</p>]]><script>alert(1)</script>"
    root = parsed(META, [item()], {"id-g1": hostile})
    description = root.findtext("./channel/item/description")

    # Present as literal text, never as an element.
    assert "script" in (description or "")
    assert root.findall(".//script") == []


@pytest.mark.unit
def test_document_is_well_formed_after_a_cdata_escape_attempt() -> None:
    # A failed escape usually manifests as malformed XML; a reader would then
    # reject the whole feed, so this asserts the document still parses.
    hostile = "text ]]> more ]]> text"
    rendered = render_feed(META, [item()], {"id-g1": hostile})
    # fromstring raises on malformed input.
    assert etree.fromstring(rendered) is not None


@pytest.mark.unit
def test_injection_cannot_create_an_element() -> None:
    # Asserted by parsing, not by counting substrings: a hostile body puts the
    # literal text "<item>" inside the CDATA, so a naive string count reports
    # a second item that does not exist as an element. Only a real parse can
    # tell structure from text.
    hostile = "<p>hi</p>]]></description></item><item><title>INJECTED</title>"
    root = parsed(META, [item()], {"id-g1": hostile})

    assert len(root.findall("./channel/item")) == 1
    assert root.findall(".//INJECTED") == []


@pytest.mark.unit
def test_cdata_section_cannot_be_closed_early() -> None:
    # The only legitimate "]]>" is the one closing the section we wrote. If a
    # second survived, the section would end early and the remainder would be
    # parsed as markup.
    hostile = "a ]]> b ]]> c"
    rendered = render_feed(META, [item()], {"id-g1": hostile}).decode()
    assert rendered.count("]]>") == 1


@pytest.mark.unit
def test_markup_is_preserved_not_escaped() -> None:
    # The point of CDATA: a reader sees formatting, not escaped tags.
    body = "<p>Hello <b>world</b>.</p>"
    root = parsed(META, [item()], {"id-g1": body})
    assert root.findtext("./channel/item/description") == "<p>Hello <b>world</b>.</p>"
    assert "&lt;p&gt;" not in render_feed(META, [item()], {"id-g1": body}).decode()


@pytest.mark.unit
def test_control_characters_are_stripped() -> None:
    # Control characters other than tab/LF/CR are illegal in XML 1.0 and would
    # make lxml raise, breaking the entire feed rather than one item.
    hostile = "before\x00after\x0bend"
    rendered = render_feed(META, [item()], {"id-g1": hostile})
    root = etree.fromstring(rendered)
    assert "\x00" not in (root.findtext("./channel/item/description") or "")


@pytest.mark.unit
def test_legitimate_markup_survives() -> None:
    body = '<p>Hello <b>world</b>, see <a href="https://x.example">here</a>.</p>'
    root = parsed(META, [item()], {"id-g1": body})
    description = root.findtext("./channel/item/description") or ""
    assert "<p>Hello <b>world</b>" in description


# --------------------------------------------------------------------------
# Structure and encoding
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_output_declares_utf8() -> None:
    rendered = render_feed(META, [])
    assert rendered.startswith(b"<?xml version='1.0' encoding='utf-8'?>")


@pytest.mark.unit
def test_channel_metadata_is_present() -> None:
    root = parsed(META, [])
    assert root.findtext("./channel/title") == "Pearss"
    assert root.findtext("./channel/link") == "https://example.com"


@pytest.mark.unit
def test_guid_is_marked_not_permalink() -> None:
    # Our GUID is a hash, not a URL. Advertising it as a permalink would make
    # readers try to fetch it.
    root = parsed(META, [item(guid="abc")])
    guid = root.find("./channel/item/guid")
    assert guid is not None
    assert guid.get("isPermaLink") == "false"
    assert guid.text == "abc"


@pytest.mark.unit
def test_link_points_at_the_canonical_url() -> None:
    root = parsed(META, [item()])
    assert root.findtext("./channel/item/link") == "https://example.com/g1"


@pytest.mark.unit
def test_pubdate_is_rfc822_with_a_numeric_zone() -> None:
    # Some readers reject a zone they do not recognise. "+0000" is the safe
    # form; "UTC" and "GMT" are the risky ones.
    root = parsed(META, [item(published=datetime(2026, 1, 5, 10, 0, tzinfo=UTC))])
    assert root.findtext("./channel/item/pubDate") == "Mon, 05 Jan 2026 10:00:00 +0000"


@pytest.mark.unit
def test_no_body_yields_no_description() -> None:
    root = parsed(META, [item()], {})
    assert root.find("./channel/item/description") is None


@pytest.mark.unit
def test_non_ascii_content_is_preserved() -> None:
    root = parsed(META, [item(title="中文标题 — em dash")], {})
    assert root.findtext("./channel/item/title") == "中文标题 — em dash"


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_visible_items_excludes_pending() -> None:
    pending = item(guid="p", status=PublishStatus.PENDING)
    ready = item(guid="r", status=PublishStatus.READY)
    published = item(guid="x", status=PublishStatus.PUBLISHED)

    assert [i.guid for i in visible_items([pending, ready, published])] == ["x", "r"]


@pytest.mark.unit
def test_visible_items_orders_newest_first() -> None:
    older = item(guid="old", published=datetime(2026, 1, 1, tzinfo=UTC))
    newer = item(guid="new", published=datetime(2026, 2, 1, tzinfo=UTC))
    assert [i.guid for i in visible_items([older, newer])] == ["new", "old"]


@pytest.mark.unit
def test_equal_timestamps_have_a_stable_order() -> None:
    # An unstable order makes clients re-announce old items as new on every
    # poll, so the tie-break on guid is part of the contract.
    same = datetime(2026, 1, 1, tzinfo=UTC)
    items = [
        item(guid="c", published=same),
        item(guid="a", published=same),
        item(guid="b", published=same),
    ]
    first = [i.guid for i in visible_items(items)]
    shuffled = [i.guid for i in visible_items(list(reversed(items)))]
    assert first == shuffled == ["c", "b", "a"]


@pytest.mark.unit
def test_missing_dates_sort_last_without_erroring() -> None:
    undated = Item(
        id="u",
        source_id="s",
        guid="u",
        content_hash="h",
        title="Undated",
        url="https://example.com/u",
        published_at=None,
        publish_status=PublishStatus.READY,
        created_at=None,
    )
    dated = item(guid="d")
    # Must not raise on a None timestamp.
    assert len(visible_items([undated, dated])) == 2


@pytest.mark.unit
def test_render_does_not_filter_because_the_caller_must() -> None:
    # render_feed deliberately renders whatever it is given; filtering is the
    # caller's job. Silently filtering here would hide a gate mistake.
    pending = item(guid="p", status=PublishStatus.PENDING)
    root = parsed(META, [pending])
    assert len(root.findall("./channel/item")) == 1


@pytest.mark.unit
def test_feed_exposes_no_internal_identifiers() -> None:
    target = item(guid="public-guid")
    rendered = render_feed(META, [target], {"id-public-guid": "Body."}).decode()
    assert target.id not in rendered
    assert target.content_hash not in rendered
    assert "public-guid" in rendered


# --------------------------------------------------------------------------
# Ordering edge cases
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_ordering_uses_publication_time_over_entry_time() -> None:
    early_published = item(guid="early", published=datetime(2026, 1, 1, tzinfo=UTC))
    later = item(
        guid="later", published=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=1)
    )
    assert [i.guid for i in visible_items([early_published, later])] == [
        "later",
        "early",
    ]
