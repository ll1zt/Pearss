"""RSS/Atom collector.

A P1 quasi-plugin: it follows the collector interface from
docs/guides/collector-guide.md but is registered explicitly rather than
discovered. It produces ItemDraft and nothing else — no database access, no
RSS output, no scheduling. Every entry, whatever its origin, converges on
ItemDraft -> normalize -> Item.

The optional fetch hook (registry `fetches=True`) exists for one upgrade:
WordPress summary feeds. A site that configured "summary" output ships a
truncated feed, while its REST API still serves the full rendered article to
everyone — `wordpress.py` detects the truncation, swaps the data source and
merges the full text back into the payload `parse` already understands. Any
failure on that path falls back to the plain feed: a summary is strictly
better than a failed collection.

The optional item hook (registry `item=True`) runs the other way round: the
caller supplies an *address* it discovered outside the feed window, and the
collector turns that one address into a draft — the same WordPress REST path
first, then the shared article extractor (`adapters/article.py`) as the
fallback, so both directions agree on what a full text is.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from time import struct_time
from typing import Any

import feedparser
import httpx

from adapters.article import (
    ARTICLE_FETCH_POLICY,
    MIN_ARTICLE_CHARS,
    extract_article_markup,
    page_metadata,
    plain_length,
)
from adapters.http import FetchError
from adapters.http import fetch as plain_fetch
from adapters.raw_payload import ConditionalState, RawPayload
from core.enums import ErrorClass
from core.models import ItemDraft
from core.plugins.hooks import HOOKIMPL, BackfillHooks, CollectorError, Manifest
from plugins.collectors.rss import wordpress

LOGGER = logging.getLogger(__name__)

# feedparser's parsed result is a feedparser.FeedParserDict, which is typed
# loosely; this alias documents that we treat it as an opaque mapping.
FeedData = Any

# Date fields feedparser may populate, in the order we trust them.
_DATE_FIELDS = ("published_parsed", "updated_parsed", "created_parsed")


@HOOKIMPL
def match_url(url: str) -> bool:
    """Whether this collector should handle `url`.

    URL-matching only: no network access and no database lookups, because it
    runs during source selection and discovery.
    """
    lowered = url.lower()
    return any(
        token in lowered
        for token in ("/feed", "/rss", "/atom", "feed.xml", "rss.xml", "atom.xml")
    )


def fetch(
    url: str,
    *,
    client: httpx.Client,
    source_id: str,
    conditional: ConditionalState | None = None,
) -> RawPayload:
    """Fetch the feed, upgrading WordPress summary feeds to full text.

    The plain fetch runs first: its ETag/Last-Modified replay is what makes
    unchanged feeds cheap, and the summary detection is memory-only over its
    bytes. Only a detected truncation triggers the REST round-trip — and a
    failure there logs and falls back, because this path must never turn a
    working summary feed into a failing source.
    """
    raw = plain_fetch(url, client=client, source_id=source_id, conditional=conditional)
    if raw.not_modified or not wordpress.looks_like_summary_feed(raw):
        return raw

    try:
        rest_raw = wordpress.fetch_fulltext(
            url, client=client, source_id=source_id, feed_entries=_feed_entry_count(raw)
        )
        posts = wordpress.parse_rest_posts(rest_raw)
    except FetchError as exc:
        LOGGER.info(
            "wordpress fulltext unavailable for %s (%s); keeping summaries",
            url.split("?", 1)[0],
            exc.detail,
        )
        return raw

    merged = wordpress.merge_fulltext(raw, wordpress.fulltext_by_link(posts))
    # The plain fetch's ETag describes the *summary* payload; serving it on a
    # fulltext body would replay stale state. Drop the conditional state so
    # the next fetch revalidates against the origin.
    return RawPayload(
        url=merged.url,
        status_code=merged.status_code,
        body=merged.body,
        source_id=merged.source_id,
        raw_fetch_id=merged.raw_fetch_id,
        headers=merged.headers,
        correlation_id=merged.correlation_id,
    )


def _feed_entry_count(raw: RawPayload) -> int:
    """How many entries the feed carries, to size the REST query window."""
    parsed = feedparser.parse(raw.body)
    return max(1, len(parsed.get("entries") or []))


@HOOKIMPL
def parse(raw: RawPayload) -> list[ItemDraft]:
    """Turn a fetched feed into drafts.

    Raises CollectorError with a classified reason rather than returning an
    empty list: "the feed changed shape" and "the feed has no entries" are very
    different situations, and collapsing them into an empty list is how a
    broken selector goes unnoticed.
    """
    if not raw.body:
        raise CollectorError(ErrorClass.PARSE, "empty payload")

    parsed = feedparser.parse(raw.body)

    # feedparser is deliberately lenient: it salvages partial entries from
    # truncated documents, so `bozo` is set even when entries exist. Keying
    # only on "bozo and no entries" would therefore publish half-parsed
    # content from a corrupt feed as if it were a real article.
    #
    # The distinguishing signal is whether the XML is well-formed at all. A
    # truncated document leaves the parser inside an unclosed element, which
    # is what we test for; salvageable feeds with minor issues parse cleanly.
    if parsed.get("bozo") and _is_truncated(raw.body):
        detail = str(parsed.get("bozo_exception", "malformed feed"))
        raise CollectorError(ErrorClass.PARSE, f"malformed feed: {detail}")

    if parsed.get("bozo") and not parsed.get("entries"):
        detail = str(parsed.get("bozo_exception", "malformed feed"))
        raise CollectorError(ErrorClass.PARSE, f"malformed feed: {detail}")

    version = parsed.get("version", "")
    if not version:
        # Recognisable XML that is not a feed: the structure contract failed.
        raise CollectorError(
            ErrorClass.STRUCTURE_CHANGED,
            "document parsed but has no recognised feed version",
        )

    entries = parsed.get("entries") or []
    if not entries:
        # A structurally valid feed with zero entries is legitimate — a new
        # feed before its first post. Not an error.
        LOGGER.info("feed has no entries: %s", raw.url.split("?", 1)[0])
        return []

    feed_title = _text(parsed.get("feed", {}).get("title"))
    drafts: list[ItemDraft] = []
    skipped = 0

    for entry in entries:
        link = _entry_link(entry)
        if not link:
            # Without a canonical URL an item cannot be identified or linked
            # from a feed, and the model forbids an empty canonical_url.
            skipped += 1
            continue

        title = _text(entry.get("title"))
        content = _entry_content(entry)
        external_id = _external_id(entry, fallback=link)

        drafts.append(
            ItemDraft(
                source_id=raw.source_id,
                external_id=external_id,
                title=title,
                canonical_url=link,
                published_at=_entry_published(entry),
                content=content,
                raw_fetch_id=raw.raw_fetch_id,
                metadata=_metadata(entry, feed_title=feed_title),
            )
        )

    if not drafts and skipped:
        # Every entry failed the structure contract. That is a changed shape,
        # not an empty feed.
        raise CollectorError(
            ErrorClass.STRUCTURE_CHANGED,
            f"all {skipped} entries lacked a usable link",
        )

    return drafts


# --------------------------------------------------------------------------
# Field extraction
# --------------------------------------------------------------------------


def _is_truncated(body: bytes) -> bool:
    """Whether the XML has unbalanced tags, i.e. the document is cut short.

    feedparser recovers what it can from a truncated feed, which is useful for
    reading but dangerous for ingestion: the recovered entries look complete
    enough to publish. Counting tag balance is the cheapest way to tell a
    corrupt document from a valid one that merely has quirks.
    """
    text = body.decode("utf-8", errors="replace")
    # Strip the constructs that do not affect tag balance.
    text = re.sub(r"<\?.*?\?>", "", text, flags=re.DOTALL)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"<!\[CDATA\[.*?\]\]>", "", text, flags=re.DOTALL)

    # Split on tag boundaries: every remaining "<...>" is one element. This
    # avoids trying to model attribute quoting inside a regex, which is where
    # the obvious one-liner breaks down.
    tags = re.findall(r"<[^>]*>", text)
    depth = 0
    for tag in tags:
        stripped = tag[1:-1].strip()
        if not stripped:
            continue
        if stripped.startswith("/"):
            depth -= 1
        elif stripped.endswith("/"):
            continue  # self-closing: opens and closes at once
        elif stripped.startswith(("!", "?")):
            continue  # doctype or processing instruction
        else:
            depth += 1
    # A well-formed document returns to zero. Truncation leaves depth > 0.
    return depth != 0


def _entry_link(entry: FeedData) -> str | None:
    """The entry's canonical link, or None if no HTTP(S) link exists.

    Only http/https are accepted. A `urn:uuid:` or `tag:` guid is a perfectly
    valid *identifier* but is not a fetchable URL, and accepting one here would
    let a non-URL become an item's canonical_url — which then reaches a feed
    as a dead <link>.
    """
    link = entry.get("link")
    if not link:
        # Atom puts the primary link in links[] with rel="alternate".
        for candidate in entry.get("links") or []:
            if candidate.get("rel") == "alternate" and candidate.get("href"):
                link = candidate["href"]
                break
    if not link and entry.get("guid"):
        # Only a self-describing permalink guid is usable as a link. Checking
        # isPermaLink alone is not enough: feedparser reports it as False for
        # a plain URL in some feeds, and True for non-URLs in others. Testing
        # the scheme is what actually matters.
        guid = str(entry["guid"]).strip()
        if guid.startswith(("http://", "https://")):
            link = guid
    if not link:
        return None
    candidate = str(link).strip()
    if not candidate.startswith(("http://", "https://")):
        return None
    return candidate


def _external_id(entry: FeedData, *, fallback: str) -> str:
    """A stable id from the origin, or one derived from the link.

    The guid is preferred because it survives a URL change at the origin. When
    it is absent the link stands in, which is stable for a given source even
    though it is not proof against a permalink change.
    """
    guid = entry.get("guid") or entry.get("id")
    if guid:
        return str(guid).strip()
    return f"link:{fallback}"


def _entry_published(entry: FeedData) -> datetime | None:
    """The entry's publication time, or None if the feed does not say.

    Returns a naive-or-aware datetime exactly as the feed expressed it,
    leaving the UTC decision to normalize(): the collector guide requires
    recording the evidence rather than guessing a zone.
    """
    for field in _DATE_FIELDS:
        value = entry.get(field)
        if isinstance(value, struct_time):
            return datetime(*value[:6]).replace(tzinfo=UTC)
    return None


def _entry_content(entry: FeedData) -> str:
    """The richest body available for an entry."""
    if entry.get("content"):
        # Atom content[] and RSS content:encoded both land here.
        for part in entry["content"]:
            value = part.get("value")
            if value:
                return str(value)
    summary = entry.get("summary")
    if summary:
        return str(summary)
    description = entry.get("description")
    return str(description) if description else ""


def _metadata(entry: FeedData, *, feed_title: str) -> dict[str, str]:
    """Structured, safe-to-store metadata.

    Only fields we deliberately keep, and all values are strings: this ends up
    in a JSON column and later in fixtures, so nothing untrusted and no
    arbitrary nesting should get through.
    """
    metadata: dict[str, str] = {}
    if feed_title:
        metadata["feed_title"] = feed_title
    author = _text(entry.get("author"))
    if author:
        metadata["author"] = author
    tags = [
        str(tag.get("term", "")).strip()
        for tag in (entry.get("tags") or [])
        if tag.get("term")
    ]
    if tags:
        metadata["tags"] = ",".join(tags)
    return metadata


def _text(value: object) -> str:
    """Coerce a feed field to a trimmed string."""
    if value is None:
        return ""
    return str(value).strip()


@HOOKIMPL
def collect_item(*, source_id: str, url: str, client: httpx.Client) -> list[ItemDraft]:
    """Fetch one already-known address of this source into a draft.

    Two paths, in order. WordPress first: an address whose shape names a post
    is answered from the REST API, which carries the full rendered article
    *and* the post's own guid -- the identity the feed uses, so the item is the
    one the feed already produced. Anything else (no REST API, a disabled
    endpoint, an id guess the API does not confirm) falls back to fetching the
    page and extracting the article with the shared extractor, the same one
    the fulltext processor uses.

    Only FetchError and CollectorError escape, like `fetch` and `parse`.
    """
    post = wordpress.fetch_post_by_url(url, client=client, source_id=source_id)
    if post is not None:
        return [wordpress.draft_from_post(post, source_id=source_id)]
    return [_article_draft(url, source_id=source_id, client=client)]


def _article_draft(url: str, *, source_id: str, client: httpx.Client) -> ItemDraft:
    """The generic path: fetch the page, extract the article, derive identity.

    The canonical URL is the page's own `rel=canonical` when it declares one
    (the address a caller found routinely carries tracking parameters), and
    the identity then follows `parse`'s rule for an entry with no guid. A page
    whose extracted text falls below the extractor's floor is a parse failure
    rather than a stored shell.
    """
    payload = plain_fetch(
        url, client=client, source_id=source_id, policy=ARTICLE_FETCH_POLICY
    )
    html_text = payload.text()
    markup = extract_article_markup(html_text, base_url=url)
    if plain_length(markup) < MIN_ARTICLE_CHARS:
        raise CollectorError(
            ErrorClass.PARSE,
            f"no extractable article at {url} ({plain_length(markup)} chars)",
        )
    meta = page_metadata(html_text)
    canonical = meta.canonical_url or url
    return ItemDraft(
        source_id=source_id,
        external_id=f"link:{canonical}",
        title=meta.title or canonical,
        canonical_url=canonical,
        published_at=meta.published_at,
        content=markup,
    )


@HOOKIMPL
def source_config() -> Manifest:
    """The rss collector's manifest."""
    return Manifest(
        id="rss",
        version="0.1.0",
        name="RSS/Atom 采集",
        description="标准 RSS/Atom feed；WordPress 摘要 feed 自动升级全文。",
        example_url="https://hnrss.org/frontpage",
        features=frozenset({"fetch", "wordpress-upgrade", "item"}),
        required_config=(),
    )


@HOOKIMPL
def collector_id() -> str:
    """The stable registration key (ADR 0017)."""
    return "rss"


@HOOKIMPL
def backfill() -> BackfillHooks | None:
    """This origin has no backward walk."""
    return None
