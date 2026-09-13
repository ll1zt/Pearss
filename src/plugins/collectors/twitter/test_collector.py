"""Regression tests for the X (Twitter) timeline collector.

`valid.json` is the payload recorded for the upstream Elixir implementation, so
the shared assertions double as a port-equivalence check. The other fixtures
cover shapes the upstream code comments call out as necessary in production but
never pinned with a test -- a quoted tweet at the top level, `binding_values`
arriving as an array, the visibility wrapper, and X's risk-control stub. Those
shims are exactly the code that rots when nothing fails after it is removed.

No fixture contains a credential: the parser never sees one.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from adapters.raw_payload import RawPayload
from core.enums import ErrorClass
from core.models import ItemDraft
from plugins.collectors.twitter.collector import (
    CollectorError,
    match_url,
    page_cursor,
    page_ids,
    page_sort_span,
    parse,
)

FIXTURES = Path(__file__).parent / "fixtures"
URL = "https://x.com/home"


def payload(name: str) -> RawPayload:
    return RawPayload(
        url=URL,
        status_code=200,
        body=(FIXTURES / name).read_bytes(),
        source_id="src-x",
        raw_fetch_id="raw-1",
    )


def drafts(name: str) -> list[ItemDraft]:
    return parse(payload(name))


def by_suffix(items: list[ItemDraft], suffix: str) -> ItemDraft | None:
    return next((d for d in items if d.external_id.endswith(suffix)), None)


# --------------------------------------------------------------------------
# URL handling
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_only_the_declared_home_route_is_matched() -> None:
    """Login-type sources are declared, never inferred from a URL shape.

    A matcher that accepted any x.com URL would attach the operator's session
    to a request nobody authorised it for.
    """
    assert match_url("https://x.com/home")
    assert not match_url("https://x.com/user1/status/1")
    assert not match_url("https://twitter.com/home")
    assert not match_url("https://example.com/home")


# --------------------------------------------------------------------------
# The shared fixture
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_parses_every_tweet_on_the_page() -> None:
    items = drafts("valid.json")
    assert len(items) == 4


@pytest.mark.unit
def test_ids_and_links_are_text_and_point_at_the_tweet() -> None:
    for item in drafts("valid.json"):
        assert item.external_id.startswith("twitter:")
        # `rest_id` is an integer in the recorded fixture and a string from the
        # live API; either way it must leave as text.
        assert item.external_id.split(":")[1].isdigit()
        assert item.canonical_url.startswith("https://x.com/")
        assert item.canonical_url.endswith(item.external_id.split(":")[1])


@pytest.mark.unit
def test_timestamps_are_utc_and_aware() -> None:
    for item in drafts("valid.json"):
        assert item.published_at is not None
        assert item.published_at.tzinfo is not None
        assert item.published_at.astimezone(UTC) == item.published_at


@pytest.mark.unit
def test_retweet_keeps_the_retweeter_and_uses_the_original_body() -> None:
    """Author is who re-posted; text, title and time are the original's.

    Rendering the wrapper's own `RT @user2: ...` text would show the prefix
    that X already represents structurally, and the reader could not tell
    whose words they were reading.
    """
    items = drafts("valid.json")
    retweet = by_suffix(items, "1")
    assert retweet is not None
    assert "User One (@user1)" in retweet.metadata["author"]
    assert "↻" in retweet.content
    assert "转推了" in retweet.content
    assert not retweet.title.startswith("RT @")
    assert retweet.metadata.get("retweet") == "true"


@pytest.mark.unit
def test_a_self_referencing_retweet_entry_renders_as_an_ordinary_tweet() -> None:
    """HomeLatestTimeline's common shape: the wrapper *is* the original.

    X puts `retweeted_status_result` on a plain tweet pointing back at itself.
    Treating that as a re-post would stamp every ordinary tweet with a "retweeted
    by" banner naming its own author, and the identity comparison is the only
    thing preventing it -- which is why this shape needs its own fixture.
    """
    items = drafts("self_retweet.json")
    assert len(items) == 1
    item = items[0]
    assert "↻" not in item.content
    assert item.metadata.get("retweet") is None
    assert item.title == "A self-referencing retweet entry from HomeLatestTimeline."


@pytest.mark.unit
def test_photo_becomes_an_img_tag() -> None:
    items = drafts("valid.json")
    photo = by_suffix(items, "2")
    assert photo is not None
    assert "media/photo1.png" in photo.content


@pytest.mark.unit
def test_long_tweet_reads_the_note_not_the_truncated_preview() -> None:
    """Past 280 chars `legacy.full_text` is a preview ending in an ellipsis.

    Reading only the legacy field publishes a truncated post and looks like a
    clean run.
    """
    items = drafts("valid.json")
    note = by_suffix(items, "4")
    assert note is not None
    assert "第四段" in note.content
    assert "<br/>" in note.content
    # The legacy preview is truncated with an ellipsis; the note is not.
    assert "…" not in note.content


@pytest.mark.unit
def test_title_is_truncated_at_a_word_boundary() -> None:
    items = drafts("valid.json")
    long_one = by_suffix(items, "3")
    assert long_one is not None
    assert len(long_one.title) <= 101  # 100 plus the ellipsis
    assert long_one.title.endswith("…")
    assert not long_one.title.rstrip("…").endswith("dow")  # not cut mid-word


@pytest.mark.unit
def test_urls_become_links_and_text_is_escaped() -> None:
    items = drafts("valid.json")
    photo = by_suffix(items, "2")
    assert photo is not None
    assert 'href="https://example.com/photo/1"' in photo.content


# --------------------------------------------------------------------------
# Paging structure
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_page_reports_ids_cursor_and_sort_span() -> None:
    body = payload("valid.json").body
    assert len(page_ids(body)) == 4
    assert page_cursor(body) == "CURSOR_PLACEHOLDER_1"
    high, low = page_sort_span(body) or (0, 0)
    assert high > low


@pytest.mark.unit
def test_cursor_only_page_is_valid_and_empty() -> None:
    """A page with no tweets is not an error: it is the end of what was sent."""
    assert drafts("no_posts.json") == []
    body = payload("no_posts.json").body
    assert page_ids(body) == []
    assert page_cursor(body) == "CURSOR_ONLY"


# --------------------------------------------------------------------------
# Shapes the upstream comments call necessary but never tested
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_quoted_tweet_at_the_top_level_is_rendered() -> None:
    """Current X puts `quoted_status_result` at the tweet root.

    Reading only the legacy path drops quoted tweets silently -- the tweet
    parses, it just loses its quote.
    """
    items = drafts("quoted.json")
    top = by_suffix(items, "2000000000000000001")
    assert top is not None
    assert "<blockquote>" in top.content
    assert "Quoted Guy" in top.content
    assert "The original statement being quoted here." in top.content


@pytest.mark.unit
def test_quoted_tweet_nested_under_legacy_is_also_rendered() -> None:
    items = drafts("quoted.json")
    nested = by_suffix(items, "2000000000000000003")
    assert nested is not None
    assert "<blockquote>" in nested.content


@pytest.mark.unit
def test_poll_card_reads_binding_values_as_a_map_and_as_an_array() -> None:
    """X emits both shapes; assuming one turns a card into a failed run."""
    items = drafts("cards.json")
    from_map = by_suffix(items, "3000000000000000001")
    from_list = by_suffix(items, "3000000000000000002")
    assert from_map is not None and from_list is not None
    assert "Best editor?" in from_map.content
    assert "Vim" in from_map.content and "Emacs" in from_map.content
    assert "Tabs or spaces?" in from_list.content
    assert "Tabs" in from_list.content and "Spaces" in from_list.content


@pytest.mark.unit
def test_link_card_renders_title_description_and_domain() -> None:
    items = drafts("cards.json")
    card = by_suffix(items, "3000000000000000003")
    assert card is not None
    assert "A linked article" in card.content
    assert "Its description" in card.content
    assert "example.com" in card.content


@pytest.mark.unit
def test_visibility_wrapper_is_unwrapped_not_published_empty() -> None:
    """`TweetWithVisibilityResults` nests the tweet one level deeper.

    Not unwrapping it yields a tweet with no text, no author and no time, which
    is worse than an error because it stores successfully.
    """
    items = drafts("visibility.json")
    assert len(items) == 1
    item = items[0]
    assert item.title == "The real tweet inside the visibility wrapper."
    assert item.published_at is not None
    assert "Visible User" in item.content


# --------------------------------------------------------------------------
# Failure classification
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_risk_control_stub_is_blocked_not_a_quiet_success() -> None:
    """`{"data": null}` means X suppressed the response.

    Returning an empty list here reads as "nothing new", so a permanently
    blocked source would report healthy runs indefinitely.
    """
    with pytest.raises(CollectorError) as excinfo:
        drafts("empty_response.json")
    assert excinfo.value.error_class is ErrorClass.BLOCKED


@pytest.mark.unit
def test_endpoint_errors_are_structure_changed() -> None:
    with pytest.raises(CollectorError) as excinfo:
        drafts("errors.json")
    assert excinfo.value.error_class is ErrorClass.STRUCTURE_CHANGED
    assert "page doesn't exist" in excinfo.value.detail


@pytest.mark.unit
def test_truncated_json_is_parse_error() -> None:
    with pytest.raises(CollectorError) as excinfo:
        drafts("malformed.json")
    assert excinfo.value.error_class is ErrorClass.PARSE


@pytest.mark.unit
def test_empty_body_is_parse_error() -> None:
    with pytest.raises(CollectorError) as excinfo:
        parse(RawPayload(url=URL, status_code=200, body=b"", source_id="s"))
    assert excinfo.value.error_class is ErrorClass.PARSE


# --------------------------------------------------------------------------
# Entity index interpretation: the ambiguity settled by evidence
# --------------------------------------------------------------------------


def _page_with(text: str, indices: list[int], short: str) -> bytes:
    import json

    tweet = {
        "__typename": "Tweet",
        "rest_id": "9001",
        "core": {
            "user_results": {"result": {"core": {"name": "N", "screen_name": "s"}}}
        },
        "legacy": {
            "full_text": text,
            "created_at": "Sun Aug 09 16:00:00 +0000 2026",
            "entities": {
                "urls": [
                    {
                        "url": short,
                        "expanded_url": "https://example.com/x",
                        "display_url": "example.com/x",
                        "indices": indices,
                    }
                ]
            },
        },
    }
    return json.dumps(
        {
            "data": {
                "home": {
                    "home_timeline_urt": {
                        "instructions": [
                            {
                                "type": "TimelineAddEntries",
                                "entries": [
                                    {
                                        "entryId": "tweet-9001",
                                        "sortIndex": "9001",
                                        "content": {
                                            "itemContent": {
                                                "tweet_results": {"result": tweet}
                                            }
                                        },
                                    }
                                ],
                            }
                        ]
                    }
                }
            }
        }
    ).encode()


def _parse_raw(body: bytes) -> list[ItemDraft]:
    return parse(RawPayload(url=URL, status_code=200, body=body, source_id="s"))


@pytest.mark.unit
def test_code_point_indices_are_used_when_they_match() -> None:
    """No emoji: both interpretations agree, code points win by being first."""
    short = "https://t.co/aa"
    text = f"read {short} now"
    start = text.index(short)
    item = _parse_raw(_page_with(text, [start, start + len(short)], short))[0]
    assert 'href="https://example.com/x"' in item.content


@pytest.mark.unit
def test_a_span_that_does_not_reproduce_the_short_url_is_not_trusted() -> None:
    """An emoji before the URL shifts code-point offsets, so the given span
    points at the wrong words. The literal search is what saves it.

    This is the case the upstream comments describe as crashing their Elixir
    port; here it must degrade to a located literal, never a mis-slice.
    """
    short = "https://t.co/bb"
    text = f"🔗 {short} check"
    # Deliberately wrong: shifted by the astral character.
    item = _parse_raw(_page_with(text, [1, 1 + len(short)], short))[0]
    assert 'href="https://example.com/x"' in item.content
    assert "🔗" in item.content


@pytest.mark.unit
def test_a_keycap_emoji_does_not_shift_the_span() -> None:
    """1️⃣ is three code points but one grapheme -- the classic off-by-two."""
    short = "https://t.co/dd"
    text = f"1️⃣ {short}"
    item = _parse_raw(_page_with(text, [2, 2 + len(short)], short))[0]
    assert 'href="https://example.com/x"' in item.content


@pytest.mark.unit
def test_the_correct_span_is_used_without_relocating_when_it_already_matches() -> None:
    """When a short URL appears twice, the span decides which occurrence.

    The literal search alone would always take the first; the span check is
    what keeps the second one linked.
    """
    short = "https://t.co/ee"
    text = f"{short} then {short}"
    second = text.rindex(short)
    body = _parse_raw(_page_with(text, [second, second + len(short)], short))[0].content
    # Exactly one link, and it is the second occurrence: the first stays text.
    assert body.count("<a ") == 1
    assert f"then {short}" not in body.replace("<a ", "")
    assert re.search(r"then <a href=", body), body


@pytest.mark.unit
def test_an_unmatchable_index_falls_back_to_finding_the_literal() -> None:
    """Neither interpretation lands on the short URL; locate it directly."""
    short = "https://t.co/cc"
    text = f"prefix {short} suffix"
    item = _parse_raw(_page_with(text, [0, 5], short))[0]
    assert 'href="https://example.com/x"' in item.content
    assert "prefix" in item.content


@pytest.mark.unit
def test_overlapping_entities_do_not_slice_backwards() -> None:
    """Out-of-order indices are clamped rather than trusted."""
    import json

    body = json.loads(
        _page_with("hello https://t.co/ee world", [20, 35], "https://t.co/ee")
    )
    urls = body["data"]["home"]["home_timeline_urt"]["instructions"][0]["entries"][0][
        "content"
    ]["itemContent"]["tweet_results"]["result"]["legacy"]["entities"]["urls"]
    urls.append(
        {
            "url": "https://t.co/ff",
            "expanded_url": "https://example.com/f",
            "display_url": "f",
            "indices": [0, 99],
        }
    )
    item = _parse_raw(json.dumps(body).encode())[0]
    assert "hello" in item.content


# --------------------------------------------------------------------------
# Time handling
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_a_non_utc_offset_is_converted_not_discarded() -> None:
    """The upstream implementation drops anything not `+0000`.

    X sends UTC in practice, but losing a real timestamp because it arrived in
    a zone we did not expect is worse than converting it.
    """
    import json

    body = json.loads(_page_with("timed https://t.co/gg", [5, 20], "https://t.co/gg"))
    result = body["data"]["home"]["home_timeline_urt"]["instructions"][0]["entries"][0][
        "content"
    ]["itemContent"]["tweet_results"]["result"]
    result["legacy"]["created_at"] = "Sun Aug 09 18:00:00 +0200 2026"
    item = _parse_raw(json.dumps(body).encode())[0]
    assert item.published_at == datetime(2026, 8, 9, 16, 0, tzinfo=UTC)


@pytest.mark.unit
def test_an_unparseable_time_is_none_rather_than_a_guess() -> None:
    import json

    body = json.loads(_page_with("no time here", [0, 1], "https://t.co/hh"))
    body["data"]["home"]["home_timeline_urt"]["instructions"][0]["entries"][0][
        "content"
    ]["itemContent"]["tweet_results"]["result"]["legacy"]["created_at"] = "yesterday"
    item = _parse_raw(json.dumps(body).encode())[0]
    assert item.published_at is None


# --------------------------------------------------------------------------
# Draft hygiene
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_drafts_stamp_source_and_raw_fetch() -> None:
    items = drafts("valid.json")
    assert all(d.source_id == "src-x" for d in items)
    assert all(d.raw_fetch_id == "raw-1" for d in items)


@pytest.mark.unit
def test_no_credential_appears_in_any_draft() -> None:
    """Cookies must not leak into stored content by way of a URL field."""
    for item in drafts("valid.json"):
        blob = item.canonical_url + item.content + item.title
        assert "auth_token" not in blob
        assert "ct0" not in blob


# --------------------------------------------------------------------------
# The avatar
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_avatar_is_a_small_square_with_the_author_as_alt_text() -> None:
    """The avatar's whole presentation budget, since styling is unavailable.

    The store-time sanitiser drops `style` and `class` (ADR 0007), so the
    square 24px box and the alt text are the only things that can make this
    read as a portrait rather than as a picture in the body. The alt text
    matters twice over: a blocked avatar URL degrades to the author's name
    instead of to nothing, and screen readers announce whose face it is.
    """
    items = drafts("valid.json")
    photo = by_suffix(items, "2")
    assert photo is not None
    assert photo.metadata["author"] == "User One (@user1)"

    images = re.findall(r"<img[^>]*>", photo.content)
    # The first image is the byline portrait; any others are post media.
    avatar = next(i for i in images if "avatar/" in i)
    assert 'alt="User One"' in avatar
    assert 'width="24"' in avatar
    assert 'height="24"' in avatar
    assert 'loading="lazy"' in avatar


@pytest.mark.unit
def test_avatar_alt_is_escaped() -> None:
    """The name is remote data and lands inside an attribute.

    A display name containing a quote would otherwise close the attribute and
    let the rest of the name become markup.
    """
    from plugins.collectors.twitter.collector import _author_html

    tweet = {
        "core": {
            "user_results": {
                "result": {
                    "core": {"name": 'Ada "The Quote" Lovelace', "screen_name": "ada"},
                    "avatar": {"image_url": "https://example.com/avatar/a.png"},
                }
            }
        }
    }
    html_out = _author_html(tweet)
    assert html_out is not None
    assert "&quot;" in html_out
    assert 'alt="Ada &quot;The Quote&quot; Lovelace"' in html_out


@pytest.mark.unit
def test_the_avatar_attributes_survive_the_sanitiser() -> None:
    """Unasserted through the sanitiser, the markup above proves nothing.

    `class` and `style` are dropped by design, so if `alt`/`width`/`height`/
    `loading` were also dropped the avatar would silently render full size with
    no accessible name -- the exact failure the collector comments warn about.
    """
    from adapters.sanitize import sanitize_html

    items = drafts("valid.json")
    photo = by_suffix(items, "2")
    assert photo is not None
    cleaned = sanitize_html(photo.content)

    avatar = next(i for i in re.findall(r"<img[^>]*>", cleaned) if "avatar/" in i)
    assert 'alt="User One"' in avatar
    assert 'width="24"' in avatar
    assert 'height="24"' in avatar
    assert 'loading="lazy"' in avatar


@pytest.mark.unit
def test_an_entry_without_an_avatar_still_renders_the_byline() -> None:
    """No portrait is not a reason to lose the author."""
    from plugins.collectors.twitter.collector import _author_html

    tweet = {
        "core": {
            "user_results": {
                "result": {
                    "core": {"name": "Ada", "screen_name": "ada"},
                }
            }
        }
    }
    html_out = _author_html(tweet)
    assert html_out is not None
    assert "<img" not in html_out
    assert "Ada" in html_out and "@ada" in html_out
