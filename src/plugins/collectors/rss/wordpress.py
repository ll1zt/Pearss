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
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit, urlunsplit

import httpx

from adapters.article import plain_length
from adapters.http import FetchError, FetchPolicy, fetch
from adapters.raw_payload import RawPayload
from core.enums import ErrorClass
from core.models import ItemDraft

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
    # The post's own guid: the identity the *feed* uses for the same article
    # (`?p=<id>` on most installs). It is what lets an article reached by
    # address land on the item the feed already produced instead of beside it.
    guid: str | None = None


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
        guid = (item.get("guid") or {}).get("rendered")
        posts.append(
            WpPost(
                id=post_id,
                link=link,
                title=title if isinstance(title, str) else "",
                content_html=content if isinstance(content, str) else "",
                published_at=date_gmt if isinstance(date_gmt, str) else None,
                guid=guid if isinstance(guid, str) and guid else None,
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
        # equals adds a network dependency for nothing. The measurement is the
        # repository's one notion of visible length (`adapters/article.py`),
        # which decodes HTML entities -- a summary cut with `&#8230;` is seven
        # characters of markup for one visible character, and markup weight
        # must not fake a length win in either direction (issue #174).
        if plain_length(post.content_html) <= plain_length(description):
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
        origin_id=raw.origin_id,
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


# --------------------------------------------------------------------------
# Single-post path: one address in, one draft out (the item capability)
# --------------------------------------------------------------------------

_POST_FIELDS = "id,link,title,content,date_gmt,guid"


def post_id_from_url(url: str) -> int | None:
    """The WordPress post id this address names, or None.

    Two shapes, both common: the `?p=<id>` query form (what WordPress's own
    canonical guid looks like) and a trailing numeric path segment
    (`/archives/2391`). A numeric segment is only a *candidate* --
    `fetch_post_by_url` checks the answer against the address before trusting
    it, because a numeric segment could be a category index.
    """
    parts = urlsplit(url)
    query_id = parse_qs(parts.query).get("p", [None])[0]
    if query_id is not None and query_id.isdigit():
        return int(query_id)
    last = parts.path.rstrip("/").rsplit("/", 1)[-1]
    return int(last) if last.isdigit() else None


def rest_candidates(url: str) -> tuple[str, ...]:
    """The REST post endpoints this address could belong to, likeliest first.

    A WordPress install sits at the domain root or one directory down, so two
    candidates cover virtually every site. The address's own leading path
    segment is tried first when there is one -- `/blog/archives/2391` names a
    `/blog` install -- and the domain root second, which keeps the common case
    to a single request. The caller treats a miss as "not WordPress" rather
    than as an error.
    """
    parts = urlsplit(url)
    origin = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
    trimmed = parts.path.strip("/")
    first_segment = trimmed.split("/", 1)[0] if trimmed else ""
    bases: list[str] = []
    if first_segment:
        bases.append(
            urlunsplit((parts.scheme, parts.netloc, f"/{first_segment}", "", ""))
        )
    bases.append(origin)
    return tuple(f"{base}/{_WP_API_SEGMENT}" for base in bases)


def fetch_post_by_url(
    url: str, *, client: httpx.Client, source_id: str
) -> WpPost | None:
    """The REST post for this address, or None when it cannot be confirmed.

    Guessing the id and then *checking the answer* is the point: `include=<id>`
    returning a post is not proof that it is the post this address names, so
    the returned post must carry that id or link back to the address. A miss
    tries the next candidate and finally returns None -- never an exception,
    because the caller has a working fallback and a site without a REST API
    must not fail the address.
    """
    post_id = post_id_from_url(url)
    if post_id is None:
        return None
    policy = FetchPolicy(
        accept="application/json", extra_headers={"accept": "application/json"}
    )
    for base in rest_candidates(url):
        endpoint = f"{base}?include={post_id}&_fields={_POST_FIELDS}"
        try:
            raw = fetch(endpoint, client=client, source_id=source_id, policy=policy)
            posts = parse_rest_posts(raw)
        except (FetchError, CollectorError) as exc:
            LOGGER.info("wordpress REST single post unavailable at %s (%s)", base, exc)
            continue
        for post in posts:
            if post.id == post_id or _permalink_key(post.link) == _permalink_key(url):
                return post
    return None


def draft_from_post(post: WpPost, *, source_id: str) -> ItemDraft:
    """The draft for a REST post: the full article, under the feed's identity.

    The identity is the post's own `guid` -- the same string the feed's
    `<guid>` carries -- so an article reached by address lands on the item the
    feed already produced instead of a duplicate beside it. A payload with no
    guid falls back to the link rule `parse` uses for an entry with none.
    """
    return ItemDraft(
        origin_id=source_id,
        external_id=post.guid or f"link:{post.link}",
        title=post.title or post.link,
        canonical_url=post.link,
        published_at=_parse_gmt(post.published_at),
        content=post.content_html,
    )


def _parse_gmt(value: str | None) -> datetime | None:
    """`date_gmt` as an aware datetime: the field's own name states the zone."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
