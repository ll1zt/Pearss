"""WordPress REST fulltext path for the RSS collector.

WordPress sites whose owner configured "summary" feed output ship a feed with
~200-500 character truncated descriptions (the classic `&#8230;` +
`more-link` cut) and no `content:encoded`. The feed's output format is a site
setting, but the WordPress REST API's `content.rendered` field carries the
full rendered article regardless of that setting, and is enabled by default
on virtually every install.

Flow, mirroring how RSSHub solves the same problem:

1. `looks_like_summary_feed(raw)` — cheap, in-memory detection from the raw
   feed bytes (no network): entries carry the truncation markers.
2. `fetch_fulltext(...)` — swap the data source: the REST endpoint is derived
   from the feed URL (`{feed_prefix}/wp-json/wp/v2/posts`), and posts are
   matched back to feed entries **by permalink**, because REST ids are a
   different identity than the feed's GUIDs and the drafts must keep stable,
   link-derived identities.
3. On any failure (no REST, disabled, moved site) the caller falls back to
   the plain feed payload — a summary is strictly better than no collection.

Everything here is HTTP + parsing; the drafts that come out still flow
through the same normalize -> dedupe -> gate pipeline, and bodies are
sanitised at the store boundary (ADR 0007) as before.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx

from adapters.http import FetchPolicy, fetch
from adapters.raw_payload import RawPayload
from core.enums import ErrorClass

LOGGER = logging.getLogger(__name__)


class CollectorError(Exception):
    """A parse that failed, carrying its ErrorClass.

    Same shape as the rss collector's own error so `parse_with` in the
    registry translates it without knowing this module exists.
    """

    def __init__(self, error_class: ErrorClass, detail: str) -> None:
        super().__init__(f"{error_class.value}: {detail}")
        self.error_class = error_class
        self.detail = detail


# Truncation fingerprints of a WordPress "summary" feed. The `&#8230;`
# ellipsis and the `more-link` anchor are what WordPress/SEO plugins inject
# at the cut; `<!--more-->` never survives to the feed, but the read-more
# wrapper does. One marker alone isn't proof (an article can legitimately
# contain an ellipsis), so the detector requires the read-more anchor.
_SUMMARY_MARKERS: tuple[re.Pattern[str], ...] = (
    re.compile(r'class="more-link"'),
    re.compile(r'class="screen-reader-text"'),
)

_MAX_FULLTEXT_POSTS = 100

_WP_API_SEGMENT = "wp-json/wp/v2/posts"


@dataclass(frozen=True, slots=True, kw_only=True)
class WpPost:
    """The fields of a REST post this path needs."""

    id: int
    link: str
    title: str
    content_html: str
    published_at: str | None  # date_gmt, as the API renders it


def looks_like_summary_feed(raw: RawPayload) -> bool:
    """Whether the fetched feed carries WordPress summary truncation.

    Pure bytes-in, bool-out: no network, no parsing beyond a regex over the
    payload, because this runs on every collection and must stay cheap.
    """
    if not raw.body:
        return False
    text = raw.body[:200_000].decode("utf-8", errors="replace")
    return any(marker.search(text) for marker in _SUMMARY_MARKERS)


def _content_base(url: str) -> str:
    """The WordPress install root for a feed URL.

    WordPress may live at the domain root or in a subdirectory; the feed URL
    shares the install prefix, so everything from the scheme to the last
    `/feed` (or `/comments/feed`, `/feed/atom`, ...) segment is the base.
    """
    parts = urlsplit(url)
    path = parts.path
    # Cut at the last /feed segment; everything before it is the install/blog
    # prefix ("/blog", "/blog/", "" ...).
    match = re.search(r"/feed(?:/[a-z0-9_-]+)?/?$", path, re.IGNORECASE)
    prefix = path[: match.start()] if match else path.rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc, prefix, "", ""))


def rest_url_for(feed_url: str) -> str:
    """The REST posts endpoint for a feed URL, subdirectory-aware."""
    return f"{_content_base(feed_url)}/{_WP_API_SEGMENT}"


def _query_for(per_page: int) -> str:
    # `orderby=date&order=desc` mirrors the feed's newest-first ordering so a
    # prefix of the REST list covers the feed window without paging far.
    return (
        f"?per_page={per_page}&orderby=date&order=desc"
        "&_fields=id,link,title,content,date_gmt"
    )


def fetch_fulltext(
    feed_url: str,
    *,
    client: httpx.Client,
    source_id: str,
    feed_entries: int,
) -> RawPayload:
    """Fetch the REST posts payload covering this feed's window.

    Raises FetchError on any failure; the caller falls back to the plain feed
    payload, because a summary feed is strictly better than a failed source.
    """
    per_page = min(max(feed_entries, 1), _MAX_FULLTEXT_POSTS)
    url = rest_url_for(feed_url) + _query_for(per_page)
    policy = FetchPolicy(
        accept="application/json",
        extra_headers={"accept": "application/json"},
    )
    return fetch(url, client=client, source_id=source_id, policy=policy)


def parse_rest_posts(raw: RawPayload) -> list[WpPost]:
    """Decode a REST posts payload into WpPost values.

    Raises CollectorError on a moved or disabled API: the response is HTML or
    garbage, and treating that as zero posts would mask the fallback.
    """
    try:
        data = json.loads(raw.text())
    except ValueError as exc:
        raise CollectorError(
            ErrorClass.PARSE, f"wordpress REST payload is not JSON: {exc}"
        ) from exc
    if not isinstance(data, list):
        raise CollectorError(
            ErrorClass.STRUCTURE_CHANGED,
            "wordpress REST payload is not a post list",
        )
    posts: list[WpPost] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        post_id = item.get("id")
        link = item.get("link")
        if not isinstance(post_id, int) or not isinstance(link, str) or not link:
            continue
        content = (item.get("content") or {}).get("rendered") or ""
        title = (item.get("title") or {}).get("rendered") or ""
        date_gmt = item.get("date_gmt")
        posts.append(
            WpPost(
                id=post_id,
                link=link,
                title=title if isinstance(title, str) else "",
                content_html=content if isinstance(content, str) else "",
                published_at=date_gmt if isinstance(date_gmt, str) else None,
            )
        )
    return posts


def _permalink_key(link: str) -> str:
    """The comparable form of a permalink: scheme-insensitive, query-free.

    Feed `<link>` values and REST `link` values describe the same article but
    routinely differ in trailing slashes, scheme case and query noise.
    """
    parts = urlsplit(link.strip())
    path = parts.path.rstrip("/")
    return f"{(parts.netloc or '').lower()}{path}"


def fulltext_by_link(posts: list[WpPost]) -> dict[str, WpPost]:
    """Index REST posts by their permalink key for feed-entry matching."""
    return {_permalink_key(post.link): post for post in posts}


def merge_fulltext(
    raw: RawPayload,
    posts_by_link: dict[str, WpPost],
) -> RawPayload:
    """Rewrite a summary feed's entries with their REST full text, in place
    of the truncated descriptions.

    Entries with no REST counterpart (deleted posts, pagination gaps) keep
    their summary — the merge never drops a feed entry. Entries whose REST
    content is *not* longer than the summary (an odd site where REST is also
    truncated) keep the summary too: swapping equals for equals adds a
    network dependency for nothing.
    """
    if not raw.body:
        return raw
    text = raw.body.decode("utf-8", errors="replace")
    replaced = 0

    def substitute(match: re.Match[str]) -> str:
        nonlocal replaced
        description = match.group(1)
        post = _post_for_description(description, posts_by_link)
        if post is None or not post.content_html:
            return match.group(0)
        # Only an upgrade counts: REST content shorter than (or equal to) the
        # summary means the API is truncated too, and swapping equals for
        # equals adds a network dependency for nothing. Compare plain text so
        # markup weight cannot fake a length win.
        if _plain_length(post.content_html) <= _plain_length(description):
            return match.group(0)
        replaced += 1
        return _cdata(post.content_html)

    # RSS: <description><![CDATA[ … ]]></description> (the common WordPress
    # shape). CDATA keeps the HTML intact; the store-time sanitiser is the
    # trust boundary either way.
    text = re.sub(
        r"<description><!\[CDATA\[(.*?)\]\]></description>",
        substitute,
        text,
        flags=re.DOTALL,
    )
    LOGGER.info("wordpress fulltext merged for %d entr(ies)", replaced)
    return RawPayload(
        url=raw.url,
        status_code=raw.status_code,
        body=text.encode("utf-8"),
        source_id=raw.source_id,
        raw_fetch_id=raw.raw_fetch_id,
        headers=raw.headers,
        etag=raw.etag,
        last_modified=raw.last_modified,
        correlation_id=raw.correlation_id,
    )


# The article link inside a truncated description: WordPress points the
# read-more anchor at the permalink, and the trailing "The post … first
# appeared" block repeats it — either resolves the entry's identity.
_PERMALINK_IN_DESCRIPTION = re.compile(r'<a[^>]+href="(https?://[^"]+)"')

_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _plain_length(html: str) -> int:
    """The visible-text length of an HTML fragment."""
    return len(_WS.sub(" ", _TAGS.sub("", html)).strip())


def _post_for_description(
    description: str, posts_by_link: dict[str, WpPost]
) -> WpPost | None:
    """The REST post this truncated description belongs to, if any.

    The match is by permalink because REST post ids and feed GUIDs are
    different identities; the read-more anchor (or the first link, when a
    site styles its cut differently) names the same article the description
    was cut from.
    """
    for href in _PERMALINK_IN_DESCRIPTION.findall(description):
        post = posts_by_link.get(_permalink_key(href))
        if post is not None:
            return post
    return None


def _cdata(html: str) -> str:
    """The replacement <description> element for one entry.

    CDATA keeps the HTML intact; a literal `]]>` inside the full text would
    terminate the section early, so it is dropped — the same guard the RSS
    output path applies (rss_out._safe_cdata).
    """
    return f"<description><![CDATA[{html.replace(']]>', '')}]]></description>"
