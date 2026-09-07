"""WordPress fulltext path tests (rss collector's fetch hook).

Fixtures are shaped after a real WordPress summary feed (kogumagoya.com was
the triggering case): truncated descriptions with the `more-link` cut, and a
REST payload whose `content.rendered` carries the full articles.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from adapters.raw_payload import RawPayload
from core.enums import ErrorClass
from plugins.collectors.rss import collector, wordpress
from plugins.collectors.rss.wordpress import CollectorError

FIXTURES = Path(__file__).parent / "fixtures"
SOURCE_ID = "src-wp-1"
FEED_URL = "https://bear.example.com/blog/feed"


def _summary_payload() -> RawPayload:
    return RawPayload(
        url=FEED_URL,
        status_code=200,
        body=(FIXTURES / "wp_summary_feed.xml").read_bytes(),
        source_id=SOURCE_ID,
    )


def _rest_payload() -> RawPayload:
    return RawPayload(
        url=wordpress.rest_url_for(FEED_URL),
        status_code=200,
        body=(FIXTURES / "wp_rest_posts.json").read_bytes(),
        source_id=SOURCE_ID,
    )


# --------------------------------------------------------------------------
# Detection and URL derivation
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_summary_feed_is_detected() -> None:
    assert wordpress.looks_like_summary_feed(_summary_payload()) is True


@pytest.mark.unit
def test_a_fulltext_feed_is_not_detected() -> None:
    payload = RawPayload(
        url=FEED_URL,
        status_code=200,
        body=(FIXTURES / "valid.xml").read_bytes(),
        source_id=SOURCE_ID,
    )
    assert wordpress.looks_like_summary_feed(payload) is False


@pytest.mark.unit
def test_rest_url_survives_a_subdirectory_install() -> None:
    # The real case: WordPress under /blog, so /wp-json alone 404s.
    assert (
        wordpress.rest_url_for("https://bear.example.com/blog/feed")
        == "https://bear.example.com/blog/wp-json/wp/v2/posts"
    )
    assert (
        wordpress.rest_url_for("https://bear.example.com/feed")
        == "https://bear.example.com/wp-json/wp/v2/posts"
    )
    assert (
        wordpress.rest_url_for("https://bear.example.com/blog/feed/atom")
        == "https://bear.example.com/blog/wp-json/wp/v2/posts"
    )


# --------------------------------------------------------------------------
# REST payload parsing and matching
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_rest_posts_parse_and_match_by_permalink() -> None:
    posts = wordpress.parse_rest_posts(_rest_payload())
    assert len(posts) == 2

    by_link = wordpress.fulltext_by_link(posts)
    # The REST link carries a trailing slash the feed link lacks; matching
    # must survive that, because the two surfaces never agree exactly.
    assert "2379" in wordpress._permalink_key(
        "https://bear.example.com/blog/archives/2379"
    )
    matched = by_link.get(
        wordpress._permalink_key("https://bear.example.com/blog/archives/2376")
    )
    assert matched is not None and matched.id == 2376


@pytest.mark.unit
def test_a_non_json_rest_payload_is_a_parse_error() -> None:
    payload = RawPayload(
        url=wordpress.rest_url_for(FEED_URL),
        status_code=200,
        body=b"<html>not json</html>",
        source_id=SOURCE_ID,
    )
    with pytest.raises(CollectorError) as exc_info:
        wordpress.parse_rest_posts(payload)
    assert exc_info.value.error_class is ErrorClass.PARSE


# --------------------------------------------------------------------------
# The merge
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_merge_replaces_truncated_descriptions_with_full_text() -> None:
    posts = wordpress.parse_rest_posts(_rest_payload())
    merged = wordpress.merge_fulltext(
        _summary_payload(), wordpress.fulltext_by_link(posts)
    )

    text = merged.text()
    # The first entry's summary was cut mid-sentence at 眼鏡; the merged body
    # carries the sentence's actual ending instead.
    assert "眼鏡 &#8230;" not in text
    assert "眼鏡屋で視力検査をする必要がある" in text
    assert "more-link" not in text or text.count("more-link") < 2
    # The second entry matched through its trailing-slash REST link.
    assert "あと三箱" in text


@pytest.mark.unit
def test_entries_without_a_rest_counterpart_keep_their_summary() -> None:
    posts = wordpress.parse_rest_posts(_rest_payload())
    by_link = {
        k: v for k, v in wordpress.fulltext_by_link(posts).items() if "2379" in k
    }

    merged = wordpress.merge_fulltext(_summary_payload(), by_link)
    text = merged.text()
    # 2379 got the full text...
    assert "眼鏡屋で視力検査をする必要がある" in text
    # ...and 2376 kept its summary rather than being dropped.
    assert "段ボールが部屋を埋め尽くしている" in text


@pytest.mark.unit
def test_rest_content_shorter_than_summary_is_ignored() -> None:
    posts = wordpress.parse_rest_posts(_rest_payload())
    by_link = wordpress.fulltext_by_link(posts)
    short = wordpress.WpPost(
        id=2379,
        link=by_link[
            wordpress._permalink_key("https://bear.example.com/blog/archives/2379")
        ].link,
        title="t",
        content_html="<p>短い</p>",
        published_at=None,
    )
    by_link[wordpress._permalink_key("https://bear.example.com/blog/archives/2379")] = (
        short
    )

    merged = wordpress.merge_fulltext(_summary_payload(), by_link)
    # A shorter "fulltext" would be a downgrade; the summary stands.
    assert "続きを読む" in merged.text()


# --------------------------------------------------------------------------
# The fetch hook, end to end over a MockTransport
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_fetch_upgrades_a_summary_feed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Plain fetch first, then REST; the merged payload feeds `parse`."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/wp-json/wp/v2/posts"):
            assert request.url.path.startswith("/blog/"), "subdirectory install"
            return httpx.Response(
                200,
                content=(FIXTURES / "wp_rest_posts.json").read_bytes(),
                headers={"content-type": "application/json"},
            )
        return httpx.Response(
            200, content=(FIXTURES / "wp_summary_feed.xml").read_bytes()
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    raw = collector.fetch(FEED_URL, client=client, source_id=SOURCE_ID)

    drafts = collector.parse(raw)
    # External ids come from the feed's own GUIDs, which the merge never
    # touches — identity is stable across the summary/fulltext switch, so a
    # source that starts upgrading produces no duplicate items.
    assert [d.external_id for d in drafts] == [
        "https://bear.example.com/blog/?p=2379",
        "https://bear.example.com/blog/?p=2376",
    ]
    assert "眼鏡屋で視力検査をする必要がある" in (drafts[0].content or "")


@pytest.mark.unit
def test_a_rest_failure_falls_back_to_summaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The upgrade must never turn a working summary feed into a failure."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/wp-json/wp/v2/posts"):
            return httpx.Response(404, text="<html>gone</html>")
        return httpx.Response(
            200, content=(FIXTURES / "wp_summary_feed.xml").read_bytes()
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    raw = collector.fetch(FEED_URL, client=client, source_id=SOURCE_ID)

    drafts = collector.parse(raw)
    assert len(drafts) == 2
    assert "続きを読む" in (drafts[0].content or "")


@pytest.mark.unit
def test_a_fulltext_feed_skips_the_rest_roundtrip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rest_hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/wp-json/wp/v2/posts"):
            rest_hits.append(str(request.url))
            return httpx.Response(200, json=[])
        return httpx.Response(200, content=(FIXTURES / "valid.xml").read_bytes())

    client = httpx.Client(transport=httpx.MockTransport(handler))
    raw = collector.fetch(
        "https://bear.example.com/feed", client=client, source_id=SOURCE_ID
    )

    assert rest_hits == [], "a non-truncated feed must not trigger the upgrade"
    assert collector.parse(raw)  # the feed parses as before
