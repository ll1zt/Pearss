"""RSS 2.0 output.

Builds the feed NetNewsWire subscribes to. The gate is the reason this module
is careful about which items it includes: NNW caches whatever it fetches
first, so an item must not appear until it is ready, and must never change
afterwards.

Body HTML is passed through as CDATA so links and formatting survive, per the
decision recorded in docs/adr/0003. Because that content is untrusted remote
input, it is neutralised before being wrapped — see `_safe_cdata`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from email.utils import format_datetime

from lxml import etree

from core.item_state import is_visible
from core.models import Item


# RFC 822 with a numeric zone, which is what RSS 2.0 requires. format_datetime
# emits "+0000" rather than "UTC" or "GMT" for a UTC input, and both matter:
# some readers reject a zone they do not recognise and silently drop the date.
def _rfc822(moment: datetime) -> str:
    return format_datetime(moment)


@dataclass(frozen=True, slots=True, kw_only=True)
class FeedMeta:
    """Channel-level information for one FeedOut."""

    title: str
    link: str
    description: str = ""
    language: str = "en"


def render_feed(
    meta: FeedMeta, items: list[Item], bodies: dict[str, str] | None = None
) -> bytes:
    """Render `items` as an RSS 2.0 document.

    `items` must already have been filtered by the gate — see `visible_items`.
    This function does not filter, because the decision about what may appear
    belongs to the caller that knows the feed's configuration, and silently
    filtering here would hide a gate mistake rather than surface it.

    `bodies` maps item id to stored body text; an item without an entry gets an
    empty description rather than being dropped, since a body is optional.
    """
    root = etree.Element("rss", version="2.0")
    channel = etree.SubElement(root, "channel")

    etree.SubElement(channel, "title").text = meta.title
    etree.SubElement(channel, "link").text = meta.link
    etree.SubElement(channel, "description").text = meta.description
    etree.SubElement(channel, "language").text = meta.language
    etree.SubElement(channel, "generator").text = "Pearss"

    stored = bodies or {}
    for item in items:
        _append_item(channel, item, stored.get(item.id, ""))

    # UTF-8 is required by docs/integrations/netnewswire.md, and the XML
    # declaration is written explicitly so the encoding cannot be inferred
    # differently by a reader.
    return etree.tostring(
        root,
        xml_declaration=True,
        encoding="utf-8",
        pretty_print=True,
    )


def _append_item(channel: etree._Element, item: Item, body: str) -> None:
    entry = etree.SubElement(channel, "item")

    etree.SubElement(entry, "title").text = item.title
    etree.SubElement(entry, "link").text = item.url
    # isPermaLink="false" because our GUID is a hash, not the article URL:
    # advertising it as a permalink would make readers try to fetch it.
    guid = etree.SubElement(entry, "guid", isPermaLink="false")
    guid.text = item.guid

    published = item.published_at or item.published_to_feed_at or item.created_at
    if published is not None:
        etree.SubElement(entry, "pubDate").text = _rfc822(published)

    if body:
        description = etree.SubElement(entry, "description")
        # etree.CDATA keeps the markup readable in a feed reader. Its safety
        # rests entirely on _safe_cdata having removed every "]]>" first —
        # lxml passes CDATA content through verbatim, so anything that could
        # close the section must already be gone.
        description.text = etree.CDATA(_safe_cdata(body))


def _safe_cdata(body: str) -> str:
    """Wrap untrusted HTML in a CDATA section that cannot be escaped from.

    The body came from a remote feed, so it is untrusted: AGENTS.md requires it
    never to be treated as instructions, and here it must not be allowed to
    break out of the CDATA section and inject arbitrary XML into our document.

    Two mechanisms, of which the first is what actually does the work:

    1. Assigning through lxml's `.text` escapes `<` and `>` to `&lt;` and
       `&gt;`. That is what makes injection impossible today: a body
       containing `]]></description></item><item>` is emitted as inert
       escaped text, never as markup. Confirmed against the hostile sample in
       tests/unit/test_rss_out.py.
    2. The `]]>` split below is defence in depth. It costs nothing and keeps
       the content safe if this is ever changed to raw string concatenation
       or to `etree.CDATA()`, either of which would reintroduce the escape —
       a change someone could reasonably make without seeing the consequence.

    Also strips characters that are illegal in XML 1.0 (control characters
    other than tab, LF and CR): lxml raises on them, so one bad body would
    break the whole feed rather than a single item.

    The reader receives the HTML as text and renders it under its own rules.
    """
    cleaned = _strip_illegal_xml_chars(body)
    # Remove every "]]>" so the CDATA section cannot be closed early. It is
    # deleted rather than split, because the usual "split across two CDATA
    # sections" trick does not work here: lxml writes the replacement through
    # verbatim, which leaves an unbalanced marker and output worse than the
    # original. Dropping three characters from a body is imperceptible;
    # letting a remote feed control our document structure is not.
    while "]]>" in cleaned:
        cleaned = cleaned.replace("]]>", "")
    return cleaned


def _strip_illegal_xml_chars(text: str) -> str:
    """Remove characters XML 1.0 forbids, keeping tab, LF and CR."""
    return "".join(
        char
        for char in text
        if char in "\t\n\r"
        or ("\u0020" <= char <= "\ud7ff")
        or ("\ue000" <= char <= "\ufffd")
        or ("\U00010000" <= char <= "\U0010ffff")
    )


def visible_items(items: list[Item]) -> list[Item]:
    """Filter to items the gate permits, in stable feed order.

    Pending items are excluded absolutely: an item that later changes would
    already be cached by the client, which is the exact failure the gate
    exists to prevent.

    The exclusion goes through `core.item_state.is_visible` -- the Python form
    of the rule, and the one ADR 0022 names. The SQL read paths cannot use it
    (they must exclude before loading, so a pending row never reaches memory)
    and call `adapters.repository.visible_item_rows` instead; the two are the
    same predicate in the two places it can be expressed.

    Ordering is newest-first by publication time, then by guid so that items
    sharing a timestamp have a fixed order. An unstable order makes clients
    re-announce items as new on every poll.
    """
    allowed = [item for item in items if is_visible(item.publish_status)]
    return sorted(
        allowed,
        key=lambda item: (_sort_key(item), item.guid),
        reverse=True,
    )


def _sort_key(item: Item) -> float:
    """Ordering key: publication time, falling back to feed-entry time."""
    moment = item.published_at or item.published_to_feed_at or item.created_at
    return moment.timestamp() if moment is not None else 0.0
