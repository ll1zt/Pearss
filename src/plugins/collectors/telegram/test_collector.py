"""Regression tests for the Telegram preview collector.

The `valid.html` fixture is the same page the upstream Elixir implementation
tests against, so the shared assertions there act as a port-equivalence check:
same message count, same id and link shapes, same photo inlining.

The smaller fixtures cover the cases the upstream parser does not pin: an
empty channel, a redesigned page, an id-only `data-post`, and a post whose
only content is a photo. Those are the ones that decide whether a real
Telegram change is reported as `structure_changed` or silently ingested as
nothing.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from adapters.raw_payload import RawPayload
from core.enums import ErrorClass
from core.models import ItemDraft
from plugins.collectors.telegram.collector import (
    CollectorError,
    before_url,
    channel_url,
    match_url,
    oldest_message_id,
    parse,
    username_of,
)

FIXTURES = Path(__file__).parent / "fixtures"
CHANNEL = "journey_of_someone"
PREVIEW_URL = f"https://t.me/s/{CHANNEL}"
PHOTOS_URL = "https://t.me/s/photochannel"


def payload(name: str, *, url: str = PREVIEW_URL) -> RawPayload:
    return RawPayload(
        url=url,
        status_code=200,
        body=(FIXTURES / name).read_bytes(),
        origin_id="src-telegram",
        raw_fetch_id="raw-1",
    )


def draft_by_id(
    drafts: list[ItemDraft], message_id: str, *, channel: str = CHANNEL
) -> ItemDraft | None:
    return next(
        (d for d in drafts if d.external_id == f"telegram:{channel}/{message_id}"),
        None,
    )


# --------------------------------------------------------------------------
# URL handling
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_match_url_accepts_preview_and_permalink() -> None:
    assert match_url("https://t.me/s/journey_of_someone")
    assert match_url("https://t.me/journey_of_someone")
    assert match_url("https://t.me/journey_of_someone/241")
    assert match_url("https://telegram.me/s/other")


@pytest.mark.unit
def test_match_url_rejects_other_hosts_and_reserved_paths() -> None:
    assert not match_url("https://example.com/s/someone")
    assert not match_url("https://t.me/login")
    assert not match_url("https://t.me/joinchat/AAAAA")
    assert not match_url("https://t.me/")


@pytest.mark.unit
def test_username_of_rejects_non_channel_urls() -> None:
    assert username_of("https://t.me/s/journey_of_someone") == CHANNEL
    assert username_of("https://t.me/@journey_of_someone") == CHANNEL
    with pytest.raises(ValueError):
        username_of("https://t.me/login")


@pytest.mark.unit
def test_before_url_sets_the_cursor() -> None:
    assert before_url(PREVIEW_URL, "241") == f"{PREVIEW_URL}?before=241"
    assert channel_url(CHANNEL) == PREVIEW_URL


# --------------------------------------------------------------------------
# Parsing the shared fixture
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_parses_every_message_in_the_preview() -> None:
    drafts = parse(payload("valid.html"))
    assert len(drafts) >= 10


@pytest.mark.unit
def test_ids_and_links_follow_the_upstream_shape() -> None:
    drafts = parse(payload("valid.html"))
    for draft in drafts:
        assert re.match(rf"^telegram:{CHANNEL}/\d+$", draft.external_id), (
            draft.external_id
        )
        assert re.match(rf"^https://t\.me/{CHANNEL}/\d+$", draft.canonical_url), (
            draft.canonical_url
        )


@pytest.mark.unit
def test_messages_carry_title_content_and_time() -> None:
    drafts = parse(payload("valid.html"))
    for draft in drafts:
        assert draft.title and draft.title.strip()
        assert draft.content and draft.content.strip()
        assert draft.published_at is not None


@pytest.mark.unit
def test_photos_are_inlined_from_css_backgrounds() -> None:
    """Preview photos live in `background-image`, not in an `src` attribute."""
    drafts = parse(payload("valid.html"))
    with_photos = [
        d for d in drafts if "<img" in d.content and "telesco.pe" in d.content
    ]
    assert with_photos, "no photo was inlined; the CSS extraction regressed"

    sample = draft_by_id(drafts, "224")
    assert sample is not None
    assert re.search(r'<img src="https://cdn\d*\.telesco\.pe/', sample.content)


@pytest.mark.unit
def test_inline_emoji_wrappers_are_dropped_but_characters_survive() -> None:
    """Decoration goes, content stays.

    The upstream parser leaves inline emoji wrappers in the body. Here the body
    reaches RSS and the Reader API, so one background-image per emoji would be
    stored and served for nothing.
    """
    drafts = parse(payload("valid.html"))
    assert not any("telegram.org/img/emoji" in d.content for d in drafts)

    sample = draft_by_id(drafts, "229")
    assert sample is not None
    assert "\U0001f60c" in sample.content


@pytest.mark.unit
def test_channel_title_loses_the_product_suffix() -> None:
    drafts = parse(payload("valid.html"))
    assert drafts[0].metadata["feed_title"] == "投机之路"


@pytest.mark.unit
def test_backfill_cursor_is_the_oldest_message_on_the_page() -> None:
    """The preview lists messages oldest-first, so the cursor is the first.

    Taking the last draft instead would produce the newest id, and `?before=`
    with that value returns a page overlapping the one just fetched: the
    backfill would appear to advance while re-reading the same messages.
    """
    drafts = parse(payload("valid.html"))
    assert oldest_message_id(drafts) == "221"
    assert drafts[0].external_id == f"telegram:{CHANNEL}/221"
    assert oldest_message_id([]) is None


@pytest.mark.unit
def test_cursor_agrees_with_the_pagination_url() -> None:
    drafts = parse(payload("valid.html"))
    cursor = oldest_message_id(drafts)
    assert cursor is not None
    assert before_url(PREVIEW_URL, cursor) == f"{PREVIEW_URL}?before=221"


# --------------------------------------------------------------------------
# Boundary fixtures
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_empty_channel_raises_structure_changed() -> None:
    """A channel with no posts still renders, so no nodes means new markup."""
    with pytest.raises(CollectorError) as excinfo:
        parse(payload("empty.html"))
    assert excinfo.value.error_class is ErrorClass.STRUCTURE_CHANGED


@pytest.mark.unit
def test_redesigned_page_raises_structure_changed() -> None:
    with pytest.raises(CollectorError) as excinfo:
        parse(payload("changed.html"))
    assert excinfo.value.error_class is ErrorClass.STRUCTURE_CHANGED


@pytest.mark.unit
def test_empty_body_raises_parse() -> None:
    with pytest.raises(CollectorError) as excinfo:
        parse(RawPayload(url=PREVIEW_URL, status_code=200, body=b"", origin_id="s"))
    assert excinfo.value.error_class is ErrorClass.PARSE


@pytest.mark.unit
def test_media_only_post_gets_a_media_title() -> None:
    drafts = parse(payload("media_only.html", url=PHOTOS_URL))
    first = draft_by_id(drafts, "10", channel="photochannel")
    assert first is not None
    assert first.title == "(photo)"
    assert "<img" in first.content
    assert "cdn4.telesco.pe" in first.content


@pytest.mark.unit
def test_emoji_only_media_is_not_treated_as_a_photo() -> None:
    """An emoji background is not an article image."""
    drafts = parse(payload("media_only.html", url=PHOTOS_URL))
    second = draft_by_id(drafts, "11", channel="photochannel")
    assert second is not None
    assert "<img" not in second.content
    assert second.title == "(media)"


@pytest.mark.unit
def test_legacy_id_only_data_post_uses_the_caller_channel() -> None:
    drafts = parse(payload("legacy_ids.html", url=PREVIEW_URL))
    assert len(drafts) == 1
    assert drafts[0].external_id == f"telegram:{CHANNEL}/500"
    assert drafts[0].canonical_url == f"https://t.me/{CHANNEL}/500"


@pytest.mark.unit
def test_published_at_is_timezone_aware_utc() -> None:
    drafts = parse(payload("legacy_ids.html"))
    moment = drafts[0].published_at
    assert isinstance(moment, datetime)
    assert moment.tzinfo is not None
    assert moment.astimezone(UTC) == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


@pytest.mark.unit
def test_drafts_stamp_source_and_raw_fetch() -> None:
    drafts = parse(payload("legacy_ids.html"))
    assert drafts[0].origin_id == "src-telegram"
    assert drafts[0].raw_fetch_id == "raw-1"
