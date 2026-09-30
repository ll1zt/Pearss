"""The rss collector's item capability: one address in, a full-text draft out.

Scenario register (normative source: `SCENARIOS.md`, AC-169..171).

Two paths, in the order the collector tries them: the WordPress REST answer for
an address whose shape names a post (the acceleration), and the generic article
extractor for everything else (the fallback). The identity each path assigns is
the point of the first scenario — an address fetched this way has to land on
the *same* item the feed would have produced, or a caller re-discovering an old
article would grow a duplicate of it every time.

HTTP is a `MockTransport` over recorded payloads: the shapes are real (the
WordPress fixtures are the ones the summary-feed path already pins), the host
is not, and nothing here reaches the network.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from adapters.raw_payload import RawPayload
from plugins.collectors.rss import collector as rss

FIXTURES = Path(rss.__file__).parent / "fixtures"
SOURCE_ID = "src-wp-1"
SITE = "https://bear.example.com"
ARTICLE_URL = f"{SITE}/blog/archives/2379"
FEED_URL = f"{SITE}/blog/feed"
REST_PATH = "/blog/wp-json/wp/v2/posts"

pytestmark = pytest.mark.unit


def _rest_posts() -> list[dict[str, Any]]:
    posts: list[dict[str, Any]] = json.loads(
        (FIXTURES / "wp_rest_posts.json").read_text(encoding="utf-8")
    )
    return posts


def _rest_content(post_id: int) -> str:
    for post in _rest_posts():
        if post["id"] == post_id:
            return str(post["content"]["rendered"])
    raise AssertionError(f"fixture has no post {post_id}")


def _article_page(*, canonical: str, body: str) -> str:
    return (
        "<html><head>"
        "<title>Page title - Bear Blog</title>"
        f'<link rel="canonical" href="{canonical}">'
        '<meta property="article:published_time" content="2026-07-27T15:00:00+00:00">'
        "</head><body><nav>menu</nav><article><h1>Headline</h1><p>"
        + body
        + "</p></article><footer>copyright</footer></body></html>"
    )


def _transport(
    *,
    seen: list[str],
    rest_posts: list[dict[str, Any]] | None = None,
    rest_status: int = 200,
    page_html: str | None = None,
) -> httpx.MockTransport:
    """Route the two shapes this path fetches, recording every request.

    The REST endpoint is matched on its exact path, so the collector's
    candidate roots are exercised for real: a request to the install root
    answers 404 and the subdirectory candidate is the one that succeeds.
    """

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path.endswith("/wp-json/wp/v2/posts"):
            if request.url.path != REST_PATH:
                return httpx.Response(404, text="no REST API here")
            if rest_status != 200:
                return httpx.Response(rest_status, text="no")
            include = request.url.params.get("include")
            posts = [
                post
                for post in (rest_posts or [])
                if include is None or str(post["id"]) == include
            ]
            return httpx.Response(
                200, json=posts, headers={"content-type": "application/json"}
            )
        if page_html is not None:
            return httpx.Response(
                200, text=page_html, headers={"content-type": "text/html"}
            )
        return httpx.Response(404, text="no page")

    return httpx.MockTransport(handle)


def _collect(url: str, *, transport: httpx.MockTransport) -> list[Any]:
    with httpx.Client(transport=transport) as client:
        return rss.collect_item(source_id=SOURCE_ID, url=url, client=client)


# --------------------------------------------------------------------------
# AC-169: the WordPress address, and the identity the feed assigns it
# --------------------------------------------------------------------------


def test_ac169_a_wordpress_address_keeps_the_feeds_identity() -> None:
    """AC-169: the draft is the full article, under the feed's own identity.

    The identity is asserted against the *feed parse* of the same site, not
    only against a literal: the literal pins the contract, and the comparison
    proves the two paths agree — a coincidence of two constants would keep
    passing while the feed's identity drifted away from this one.
    """
    seen: list[str] = []

    drafts = _collect(
        ARTICLE_URL, transport=_transport(seen=seen, rest_posts=_rest_posts())
    )

    assert len(drafts) == 1
    draft = drafts[0]
    assert draft.canonical_url == ARTICLE_URL
    assert draft.content == _rest_content(2379)
    assert draft.title == "視力検査と昼食"
    assert draft.published_at == datetime(2026, 7, 27, 15, 0, tzinfo=UTC)

    feed = rss.parse(
        RawPayload(
            url=FEED_URL,
            status_code=200,
            body=(FIXTURES / "wp_summary_feed.xml").read_bytes(),
            source_id=SOURCE_ID,
        )
    )
    feed_identity = {d.canonical_url: d.external_id for d in feed}
    assert draft.external_id == feed_identity[ARTICLE_URL]
    # Pinned: this is the dedupe contract, so it changes only with a migration.
    assert draft.external_id == "https://bear.example.com/blog/?p=2379"

    # Only the REST endpoint was fetched: a page fetch here would mean the
    # acceleration did not happen (and would cost a second request per item).
    assert [url.split("?")[0] for url in seen] == [f"{SITE}{REST_PATH}"]
    assert "include=2379" in seen[0]


def test_a_rest_answer_for_another_post_is_not_trusted() -> None:
    """A numeric path segment is a *guess* at a post id, so the answer is checked.

    The REST payload comes back for the wrong post; the collector must fetch
    the page instead of storing someone else's article under this address.
    """
    other = [post for post in _rest_posts() if post["id"] != 2379]
    seen: list[str] = []
    page = _article_page(canonical=ARTICLE_URL, body="Body prose. " * 40)

    drafts = _collect(
        ARTICLE_URL,
        transport=_transport(seen=seen, rest_posts=other, page_html=page),
    )

    assert len(drafts) == 1
    assert "Body prose." in drafts[0].content
    assert seen[-1] == ARTICLE_URL, "the page must be fetched when REST disagrees"


# --------------------------------------------------------------------------
# AC-170: everything else goes through the shared article extractor
# --------------------------------------------------------------------------


def test_ac170_a_non_wordpress_address_falls_back_to_the_article_extractor() -> None:
    """AC-170: the page's own article, under a link-derived identity.

    The address a caller found may carry tracking parameters, so the canonical
    URL comes from the page's `rel=canonical` — and the identity is derived
    from *that*, the same rule the feed parse uses for an entry with no guid.
    """
    canonical = f"{SITE}/blog/posts/hello"
    seen: list[str] = []
    page = _article_page(canonical=canonical, body="Body prose. " * 40)

    drafts = _collect(
        f"{SITE}/blog/hello?utm_source=x",
        transport=_transport(seen=seen, page_html=page),
    )

    assert len(drafts) == 1
    draft = drafts[0]
    assert draft.canonical_url == canonical
    assert draft.external_id == f"link:{canonical}"
    assert "Body prose." in draft.content
    assert draft.title == "Page title - Bear Blog"
    assert draft.published_at == datetime(2026, 7, 27, 15, 0, tzinfo=UTC)
    # No post id in the address means no REST probe at all.
    assert seen == [f"{SITE}/blog/hello?utm_source=x"]


def test_a_rest_post_without_a_guid_uses_the_link_identity() -> None:
    """A payload with no guid is the same situation as a feed with no guid.

    The link rule is `parse`'s fallback for exactly this case, so the two
    paths stay consistent even on an install that does not render one.
    """
    stripped = [
        {key: value for key, value in post.items() if key != "guid"}
        for post in _rest_posts()
    ]

    drafts = _collect(
        ARTICLE_URL,
        transport=_transport(seen=[], rest_posts=stripped),
    )

    assert drafts[0].external_id == f"link:{ARTICLE_URL}"


def test_a_rest_post_with_an_unreadable_date_records_no_time() -> None:
    """A junk `date_gmt` is no date, not a crash and not a guess."""
    broken = [dict(post, date_gmt="sometime in July") for post in _rest_posts()]

    drafts = _collect(
        ARTICLE_URL,
        transport=_transport(seen=[], rest_posts=broken),
    )

    assert drafts[0].published_at is None


def test_a_page_without_an_article_is_a_parse_failure() -> None:
    """A 200 page that is not an article must fail, not store chrome.

    The floor is the extractor's own (`MIN_ARTICLE_CHARS`), so this path and
    the fulltext processor cannot disagree about what "an article" means.
    """
    from core.plugins.hooks import CollectorError

    page = "<html><body><article><p>tiny</p></article></body></html>"

    with pytest.raises(CollectorError) as excinfo:
        _collect(
            f"{SITE}/blog/hello",
            transport=_transport(seen=[], page_html=page),
        )

    assert excinfo.value.error_class.value == "parse"


# --------------------------------------------------------------------------
# AC-171: the chain does not fetch a second time
# --------------------------------------------------------------------------


def test_ac171_the_fetched_article_is_long_enough_for_the_chain_to_skip_it() -> None:
    """AC-171: the fulltext processor takes its "already an article" branch.

    Composed at the component boundary: the draft this capability produces is
    handed to the processor exactly as the chain would hand it, and the
    processor must return an empty artifact without fetching anything. (The
    database-level chain is the pipeline's business; what is pinned here is
    that the two components agree on the threshold, so enabling the chain does
    not buy a second request per article.)
    """
    from plugins.processors.fulltext import processor

    drafts = _collect(
        ARTICLE_URL,
        transport=_transport(seen=[], rest_posts=_rest_posts()),
    )
    draft = drafts[0]

    stored: list[str] = []

    def store(content: str) -> str:
        stored.append(content)
        return "artifact-ref"

    processor.process(
        {
            "item_id": "it-1",
            "title": draft.title,
            "content": draft.content,
            "url": draft.canonical_url,
        },
        target_lang="en",
        store=store,
    )

    assert stored == [""], "a body at or above the floor must produce no artifact"


# --------------------------------------------------------------------------
# The capability is declared, and the declaration has an implementation
# --------------------------------------------------------------------------


def test_the_collector_declares_the_item_capability_it_implements() -> None:
    """Declared and implemented: the startup check reads both halves."""
    from app import collector_registry

    assert "item" in rss.source_config().features
    registration = collector_registry.by_name("rss")
    assert registration is not None
    assert registration.item is True
