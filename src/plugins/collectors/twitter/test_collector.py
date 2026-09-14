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
from typing import Any

import pytest

from adapters.raw_payload import RawPayload
from adapters.sanitize import AVATAR_CLASS
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

    This is *not* the same claim as the bare `RT ` marker the title carries --
    see `test_a_retweet_title_says_rt_without_naming_the_author`. What is
    refused here is the `RT @handle:` form, because it duplicates the author
    metadata and obscures whose words these are; what is wanted there is a
    two-letter type marker that names nobody.
    """
    items = drafts("valid.json")
    retweet = by_suffix(items, "1")
    assert retweet is not None
    assert "User One (@user1)" in retweet.metadata["author"]
    assert "↻" in retweet.content
    assert "转推了" in retweet.content
    assert "RT @" not in retweet.title, "the upstream RT @handle: form is refused"
    assert retweet.metadata.get("retweet") == "true"


# --------------------------------------------------------------------------
# Type markers in the title
#
# A reader's list view shows titles and nothing else, so a type that is only
# marked in the body is invisible there. Measured on a real page: 10 retweets
# and 32 quotes out of 95 tweets -- 44% -- carried no title-level signal at all.
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_a_retweet_title_says_rt_without_naming_the_author() -> None:
    """The marker is a type, not a byline: `RT ` and nothing more.

    Marking the type is what makes a retweet recognisable in a list view;
    naming the author there would repeat `metadata["author"]` and reintroduce
    the confusion the refused `RT @handle:` form causes.
    """
    retweet = by_suffix(drafts("valid.json"), "1")
    assert retweet is not None
    assert retweet.title.startswith("RT ")
    # The original's text still follows the marker, so the reader sees what
    # was said, not who said it.
    assert "Breaking sample story text" in retweet.title
    # The marker itself carries no handle. Asserted on the marker rather than
    # the whole title, because a tweet's own text may legitimately mention
    # someone -- live titles DO contain "@handle" from the body.
    assert _marker_of(retweet.title) == "RT "
    assert not retweet.title.startswith("RT @")


def _marker_of(title: str) -> str:
    """The type marker a title carries, or "" when it has none."""
    for marker in ("RT ", "QT "):
        if title.startswith(marker):
            return marker
    return ""


@pytest.mark.unit
def test_a_quoted_tweet_title_says_qt() -> None:
    """A quote is marked `QT ` in both shapes X has used for it.

    `quoted.json` carries the same fact on two paths -- top-level
    `quoted_status_result` and the legacy nesting -- and both must be marked,
    or one of the two shapes silently loses its type.
    """
    items = drafts("quoted.json")
    assert len(items) == 2, "the fixture holds two quote shapes"
    for item in items:
        assert item.title.startswith("QT "), f"{item.external_id} lost its marker"
        assert _marker_of(item.title) == "QT ", "the marker is the type, not a byline"


@pytest.mark.unit
def test_a_quote_has_no_marker_when_it_is_itself_original_text() -> None:
    """Marking is about the entry's type, not about containing a link.

    A tweet that merely links somewhere is not a quote, and marking it `QT `
    would put the marker on ordinary posts -- which is how a type signal stops
    meaning anything.
    """
    from plugins.collectors.twitter.collector import _type_marker

    assert _type_marker(retweet=False, quoted=False) == ""
    assert _type_marker(retweet=True, quoted=False) == "RT "
    assert _type_marker(retweet=False, quoted=True) == "QT "


@pytest.mark.unit
def test_a_retweeted_quote_is_marked_rt_not_qt() -> None:
    """Both flags true resolves to `RT `: the entry is a re-post at top level.

    A retweeted quote is a re-post whose payload happens to contain a quote.
    The quote is already rendered in the body's blockquote, whereas the
    re-posting is not visible anywhere else in the title -- so `QT ` here
    would bury the more useful fact.
    """
    from plugins.collectors.twitter.collector import _type_marker

    assert _type_marker(retweet=True, quoted=True) == "RT "


@pytest.mark.unit
def test_an_ordinary_tweet_has_no_type_marker() -> None:
    """Only the two derived types are marked; a plain tweet starts with its text."""
    plain = by_suffix(drafts("valid.json"), "2")
    assert plain is not None
    assert plain.title.startswith("A sample tweet with a photo attached")
    assert not plain.title.startswith(("RT ", "QT "))


@pytest.mark.unit
def test_a_marker_is_counted_within_the_title_budget() -> None:
    """The marker must not push the title past its own limit.

    `TITLE_MAX` bounds the whole title, marker included -- otherwise every
    marked title would overrun by the marker's length, which is exactly the
    kind of off-by-a-prefix that only shows up in a reader's list view.

    The shared fixtures cannot show this: their only title near the limit is
    an *unmarked* one, where reserving the marker's width and not reserving it
    are the same thing. A long title that is also marked is what makes the
    reservation load-bearing, so this builds one inline.
    """
    from plugins.collectors.twitter.collector import TITLE_MAX, _title_from_text

    long_text = "word " * 60  # 300 chars, far past any marker-adjusted budget
    marked = _title_from_text(long_text, marker="RT ")
    unmarked = _title_from_text(long_text)

    assert marked.startswith("RT "), "the marker survives truncation"
    assert len(marked) <= TITLE_MAX + 1, f"marked title is {len(marked)} chars"
    assert len(unmarked) <= TITLE_MAX + 1, f"unmarked title is {len(unmarked)} chars"

    # Whitespace-delimited text cannot show the reservation, because dropping
    # a partial word absorbs the marker's width either way -- measured: both
    # land inside the budget whatever the limit. A single unbroken token has
    # no partial word to drop, so here the reservation is the only thing
    # keeping a marked title inside the limit. This is the case that fails if
    # the marker is added on top of the budget instead of spent from it.
    one_token = "x" * 200
    assert len(_title_from_text(one_token, marker="RT ")) <= TITLE_MAX + 1
    assert len(_title_from_text(one_token, marker="QT ")) <= TITLE_MAX + 1

    # And through the whole pipeline, so a title carrying a marker cannot
    # exceed the limit either.
    for name in ("valid.json", "quoted.json"):
        for item in drafts(name):
            assert len(item.title) <= TITLE_MAX + 1, f"{name}: {item.title!r}"


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
def test_a_photo_carries_its_real_dimensions() -> None:
    """`width`/`height` from `original_info` reserve the box before it loads.

    Without them the reader reflows as each image arrives, so a timeline jumps
    under the cursor. The payload states the true size (all 29 photos on a
    measured page carried it), so guessing is unnecessary.
    """
    photo = by_suffix(drafts("valid.json"), "2")
    assert photo is not None
    img = next(
        i for i in re.findall(r"<img[^>]*>", photo.content) if "media/photo1" in i
    )
    assert 'width="500"' in img, img
    assert 'height="500"' in img, img
    # The full-size URL is kept: the plan chose real images over thumbnails.
    assert "media/photo1.png" in img


@pytest.mark.unit
def test_a_photo_without_dimensions_still_renders() -> None:
    """A missing `original_info` must not drop the image or invent a size.

    Sizing is an improvement, not a requirement: an image with no stated
    dimensions is still worth showing, just without the reserved box.
    """
    from plugins.collectors.twitter.collector import _media_html

    out = _media_html(
        [
            {
                "type": "photo",
                "media_url_https": "https://example.com/media/nosize.png",
            }
        ]
    )
    assert out is not None
    assert "media/nosize.png" in out
    assert "width=" not in out
    assert "height=" not in out


@pytest.mark.unit
def test_only_positive_integer_dimensions_are_rendered() -> None:
    """Malformed sizes are omitted rather than written into the markup.

    `bool` is an `int` subclass in Python, so a naive `isinstance(x, int)`
    accepts `True` and emits `width="True"` -- invalid markup that no reader
    can interpret. Negative and zero sizes are equally meaningless.
    """
    from plugins.collectors.twitter.collector import _dimensions

    assert _dimensions({"original_info": {"width": 500, "height": 400}}) == (
        ' width="500" height="400"'
    )
    for bad in (
        {"width": True, "height": True},
        {"width": "500", "height": "400"},
        {"width": 0, "height": 400},
        {"width": -5, "height": 400},
        {"width": None, "height": None},
    ):
        assert _dimensions({"original_info": bad}) == "", f"{bad} produced markup"
    assert _dimensions({}) == ""
    assert _dimensions({"original_info": "not a dict"}) == ""


@pytest.mark.unit
def test_quoted_tweets_show_the_author_avatar() -> None:
    """The quote block names its author; the avatar makes that name a face.

    Every quoted tweet on a measured page carried an avatar, and the block
    previously rendered text only -- so a quote read as an unattributed aside.
    """
    quoted = next(d for d in drafts("quoted.json") if d.external_id.endswith("1"))
    block = quoted.content.split("<blockquote>", 1)[1]
    assert "avatar/2002.png" in block, block
    assert f'class="{AVATAR_CLASS}"' in block


@pytest.mark.unit
def test_quoted_media_is_shown_in_the_block() -> None:
    """A quote's own image belongs in the quote, not dropped.

    Ten of 29 quoted tweets on a measured page carried media. Rendering only
    the text made those read as a one-line aside.
    """
    quoted = next(d for d in drafts("quoted.json") if d.external_id.endswith("1"))
    block = quoted.content.split("<blockquote>", 1)[1]
    assert "media/quoted1.png" in block, block
    assert 'width="1024"' in block and 'height="768"' in block


@pytest.mark.unit
def test_a_quote_without_media_renders_no_image() -> None:
    """The second fixture shape has no media; nothing must be invented."""
    nested = next(d for d in drafts("quoted.json") if d.external_id.endswith("3"))
    block = nested.content.split("<blockquote>", 1)[1]
    assert "media/" not in block, block
    assert "The original statement being quoted" in block


@pytest.mark.unit
def test_the_post_time_is_visible_in_the_body() -> None:
    """`published_at` was parsed but never shown, so a reader had no date.

    The `<time>` element carries a machine-readable `datetime` alongside the
    visible text; the sanitiser admits `time[datetime]`, so this survives
    storage.
    """
    from plugins.collectors.twitter.collector import _time_html

    item = drafts("valid.json")[0]
    out = _time_html(item.published_at)
    assert out is not None
    assert "<time " in out and "datetime=" in out
    assert "2026-08-08" in out, out


@pytest.mark.unit
def test_a_tweet_with_no_time_renders_no_time_element() -> None:
    """A missing date must not become a fabricated one or an empty element."""
    from plugins.collectors.twitter.collector import _time_html

    assert _time_html(None) is None


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
# Entry shapes: what a page contains besides plain top-level tweets
#
# Measured on a real HomeLatestTimeline page (2026-09-14): of 95 entries, 79
# were top-level tweets, 5 were `home-conversation` modules holding 13 *more*
# tweets, 9 were promoted slots, and 2 were cursors. The 13 conversation
# tweets were being dropped whole -- about 14% of the page.
# --------------------------------------------------------------------------


def _entries_of(body: bytes | str) -> list[dict[str, Any]]:
    """The fixture page's entries, as raw JSON.

    The tests below assert on the page's *shape* rather than on parser output,
    so they read the fixture directly; parsing it through `_entries` would
    make them agree with the parser by construction.
    """
    import json

    page: dict[str, Any] = json.loads(body)
    entries: list[dict[str, Any]] = page["data"]["home"]["home_timeline_urt"][
        "instructions"
    ][0]["entries"]
    return entries


def _entry_ids(body: bytes | str) -> list[str]:
    """Every `tweet-` entryId anywhere on the page, modules included."""
    found: list[str] = []
    for entry in _entries_of(body):
        if entry["entryId"].startswith("tweet-"):
            found.append(entry["entryId"])
        for item in (entry.get("content") or {}).get("items") or []:
            inner = item.get("entryId", "")
            if inner.startswith("tweet-"):
                found.append(inner)
    return found


def _top_level_sort_min(body: bytes | str) -> int:
    return min(
        int(e["sortIndex"])
        for e in _entries_of(body)
        if e["entryId"].startswith("tweet-")
    )


def _tweet_sorts(body: bytes | str) -> list[int]:
    """Every tweet's position on the page, modules included.

    A module's own sortIndex is a layout position rather than the id range of
    the tweets inside it, so an item contributes its own id here.
    """
    tops = [
        int(e["sortIndex"])
        for e in _entries_of(body)
        if e["entryId"].startswith("tweet-")
    ]
    return tops + _module_sort_sorts(body)


def _module_sort_sorts(body: bytes | str) -> list[int]:
    return [
        int(item["entryId"].split("-", 1)[1])
        for entry in _entries_of(body)
        for item in (entry.get("content") or {}).get("items") or []
        if item.get("entryId", "").startswith("tweet-")
    ]


def _cursor_sorts(body: bytes | str) -> list[int]:
    return [
        int(e["sortIndex"])
        for e in _entries_of(body)
        if e["entryId"].startswith("cursor")
    ]


@pytest.mark.unit
def test_tweets_inside_a_conversation_module_are_parsed() -> None:
    """`home-conversation` is a TimelineTimelineModule, not a tweet.

    Its `content.items[].item.itemContent` carries full tweets (the thread's
    replies), so a parser that only looks at top-level `tweet-` entries loses
    them silently. This asserts the count, not merely that parsing succeeds:
    a test that only checked "no exception" would pass on the broken version.
    """
    body = payload("conversation.json").body
    assert len(_entry_ids(body)) == 7, "the fixture holds 7 tweets in total"
    assert len(page_ids(body)) == 7, "every tweet on the page must be seen"
    assert len(drafts("conversation.json")) == 7


@pytest.mark.unit
def test_a_conversation_module_keeps_its_replies_in_order() -> None:
    """Module items are ordered as X laid them out, and keep their fields."""
    items = drafts("conversation.json")
    by_id = {d.external_id.removeprefix("twitter:"): d for d in items}
    parent = next(v for v in by_id.values() if "Parent post" in v.title)
    first_reply = next(v for v in by_id.values() if "First reply" in v.title)
    second_reply = next(v for v in by_id.values() if "Second reply" in v.title)

    assert parent.metadata["author"] == "Gamma (@gamma)"
    assert first_reply.metadata["author"] == "Delta (@delta)"
    # The reply link must point at the replier, not the thread parent.
    assert "/delta/status/" in first_reply.canonical_url
    assert second_reply.published_at is not None
    # Order within the module is the page's order: parent, then replies.
    ids = [d.external_id for d in items]
    assert ids.index(parent.external_id) < ids.index(first_reply.external_id)
    assert ids.index(first_reply.external_id) < ids.index(second_reply.external_id)


@pytest.mark.unit
def test_a_promoted_item_inside_a_module_drops_only_itself() -> None:
    """Promotion is an item-level property; it must not condemn the module.

    `_is_promoted` recursed into a module's items and returned True if *any*
    item was promoted -- and `_tweets` then skipped the whole module, losing
    the thread's legitimate replies along with the ad. That is the same class
    of silent content loss this branch set out to fix, moved one level down.

    Found by review, not by a failing run: no real module on the measured
    pages carried a promoted item, so the shape was never observed. It is
    guarded here rather than assumed away.
    """
    entry = {
        "entryId": "home-conversation-300",
        "sortIndex": "300",
        "content": {
            "__typename": "TimelineTimelineModule",
            "items": [
                _module_item(101, "legitimate reply A"),
                _module_item(102, "an ad inside the module", promoted=True),
                _module_item(103, "legitimate reply B"),
            ],
        },
    }
    body = _page_of([entry])

    ids = page_ids(body)
    assert "101" in ids and "103" in ids, f"the ad took its siblings down: {ids}"
    assert "102" not in ids, "the promoted item itself must still be excluded"
    assert len(drafts_of(body)) == 2


@pytest.mark.unit
def test_the_span_ignores_a_promoted_item_inside_a_module() -> None:
    """The span must describe the same tweets `_tweets` returns.

    A promoted tweet's id comes from the advertiser's own timeline, so it is
    unrelated in magnitude to the surrounding ids. Counting it would report a
    floor far below the real one -- and that floor is what gap detection
    compares the next page against.
    """
    entry = {
        "entryId": "home-conversation-90",
        "sortIndex": "90",
        "content": {
            "__typename": "TimelineTimelineModule",
            "items": [
                _module_item(95, "a real reply"),
                _module_item(11, "an ad whose id is far older", promoted=True),
            ],
        },
    }
    body = _page_of(
        [
            {
                "entryId": "tweet-100",
                "sortIndex": "100",
                "content": _top_item(100, "the newest tweet"),
            },
            entry,
        ]
    )

    span = page_sort_span(body)
    assert span is not None
    high, low = span
    assert low == 95, f"the ad's id reached the span: {span}"
    assert high == 100
    # And the two views agree about what is on the page.
    assert set(page_ids(body)) == {"100", "95"}


def _page_of(entries: list[dict[str, Any]]) -> bytes:
    """A minimal page carrying exactly these entries."""
    import json

    return json.dumps(
        {
            "data": {
                "home": {
                    "home_timeline_urt": {
                        "instructions": [
                            {"type": "TimelineAddEntries", "entries": entries}
                        ]
                    }
                }
            }
        }
    ).encode()


def _tweet_result(rid: int, text: str) -> dict[str, Any]:
    return {
        "__typename": "Tweet",
        "rest_id": str(rid),
        "core": {
            "user_results": {"result": {"core": {"name": "N", "screen_name": "s"}}}
        },
        "legacy": {
            "full_text": text,
            "created_at": "Sun Aug 09 16:00:00 +0000 2026",
            "id_str": str(rid),
            "conversation_id_str": str(rid),
            "entities": {},
        },
    }


def _top_item(rid: int, text: str) -> dict[str, Any]:
    return {"itemContent": {"tweet_results": {"result": _tweet_result(rid, text)}}}


def _module_item(rid: int, text: str, *, promoted: bool = False) -> dict[str, Any]:
    content: dict[str, Any] = {"tweet_results": {"result": _tweet_result(rid, text)}}
    if promoted:
        content["promotedMetadata"] = {"advertiser_results": {}}
    return {"entryId": f"tweet-{rid}", "item": {"itemContent": content}}


def drafts_of(body: bytes) -> list[ItemDraft]:
    return parse(RawPayload(url=URL, status_code=200, body=body, source_id="s"))


@pytest.mark.unit
def test_promoted_tweets_are_never_ingested() -> None:
    """A promoted slot carries a real tweet_results and must be excluded.

    This is the trap in fixing the conversation case: once the parser walks
    `itemContent` instead of keying off the `tweet-` prefix, an ad would be
    ingested unless it is excluded *explicitly*. The fixture's promoted entry
    is designed to be parsed successfully if it is ever let through.
    """
    body = payload("conversation.json").body
    assert all("promoted" not in i for i in page_ids(body))
    for draft in drafts("conversation.json"):
        assert "PROMOTED" not in draft.title
        assert "adco" not in draft.canonical_url
        assert "PROMOTED" not in draft.content


@pytest.mark.unit
def test_a_promoted_slot_is_excluded_even_when_its_id_looks_like_a_tweet() -> None:
    """The exclusion hinges on the `promotedMetadata` field, not the id's shape.

    Measured on real pages the two signals always agreed, so a test that only
    used X's current `promoted-tweet-` prefix would keep passing even if the
    field guard were deleted -- the id would simply be skipped by the
    `tweet-` branch never matching. Renaming the id to the ordinary shape is
    what makes the field guard load-bearing and therefore falsifiable.
    """
    import json

    body = json.loads(payload("conversation.json").body)
    entries = body["data"]["home"]["home_timeline_urt"]["instructions"][0]["entries"]
    renamed = 0
    for entry in entries:
        if entry["entryId"].startswith("promoted-tweet-"):
            entry["entryId"] = "tweet-9990000000000000001"
            renamed += 1
    assert renamed == 1, "the fixture must contain exactly one promoted entry"

    raw = json.dumps(body).encode()
    ids = page_ids(raw)
    assert "9990000000000000001" not in ids, "an ad slipped in on a tweet-shaped id"
    for item in parse(RawPayload(url=URL, status_code=200, body=raw, source_id="s")):
        assert "PROMOTED" not in item.content
        assert "adco" not in item.canonical_url


@pytest.mark.unit
def test_page_sort_span_ignores_cursor_entries() -> None:
    """Only tweets bound the interval; a cursor's sortIndex is not a tweet's.

    Measured on real pages: `cursor-top` sits exactly one above the newest
    tweet and `cursor-bottom` one below the oldest, so including them widens
    the span by two units and shifts `previous_min_sort` below the true tweet
    floor. Gap detection compares against that value, so the interval must be
    built from tweets alone.
    """
    body = payload("conversation.json").body
    span = page_sort_span(body)
    assert span is not None
    high, low = span
    tweets = _tweet_sorts(body)
    assert high == max(tweets), "the newest tweet bounds the top"
    assert low == min(tweets), "the oldest tweet bounds the bottom"

    cursors = _cursor_sorts(body)
    assert cursors, "the fixture has cursors, or this proves nothing"
    assert low > min(cursors), "a cursor must not set the bottom of the span"
    assert high < max(cursors), "a cursor must not set the top of the span"

    # The fixture puts the conversation module's oldest reply *below* every
    # top-level tweet, as a real page does when a thread sits at the bottom.
    # Without that, dropping module items from the span would be undetectable
    # -- they would all fall inside the top-level range and change neither end.
    assert low < _top_level_sort_min(body), "a module tweet must set the bottom"
    assert min(_module_sort_sorts(body)) == low, "the module reply is the oldest"


@pytest.mark.unit
def test_a_cursor_only_page_has_no_span() -> None:
    """No tweets means continuity cannot be proven -- not that it holds.

    Returning a span here is worse than returning nothing: `(5, 5)`-style
    values (as the old fixture implied) make every following page look
    discontinuous, and even a correct-looking value would claim a continuity
    the page has no evidence for.
    """
    body = payload("no_posts.json").body
    assert page_ids(body) == []
    assert page_sort_span(body) is None


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
    assert 'class="pearss-avatar"' in avatar
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
    assert 'class="pearss-avatar"' in avatar
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
