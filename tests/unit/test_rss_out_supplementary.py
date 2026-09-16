"""User text survives the retained RSS renderer-to-XML-parser boundary."""

from __future__ import annotations

import pytest
from lxml import etree

from adapters.rss_out import FeedMeta, render_feed
from core.enums import PublishStatus
from core.models import Item

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("A\U0001f600\U00020000B\x00\x0b", "A\U0001f600\U00020000B"),
        ("\U00010000\U0010ffff", "\U00010000\U0010ffff"),
        ("A\x00\x07\x1f\ud800\udfff\ufffe\uffffB", "AB"),
        # XML permits DEL and normalizes CR to LF when parsing.
        ("A\t\n\r\x7fB", "A\t\n\n\x7fB"),
        ("\ud7ff\ue000\ufffd", "\ud7ff\ue000\ufffd"),
    ],
)
def test_user_feed_body_keeps_valid_text_and_drops_forbidden_chars(
    body: str, expected: str
) -> None:
    """Given mixed Unicode, when RSS is parsed, then valid text survives."""
    item = Item(
        id="id-supplementary",
        source_id="s",
        guid="supplementary",
        content_hash="h-supplementary",
        title="Supplementary",
        url="https://example.com/supplementary",
        published_at=None,
        publish_status=PublishStatus.READY,
    )
    rendered = render_feed(
        FeedMeta(title="Pearss", link="https://example.com"),
        [item],
        {item.id: body},
    )
    root = etree.fromstring(rendered)
    assert root.findtext("./channel/item/description") == expected
    assert len(root.findall("./channel/item")) == 1
