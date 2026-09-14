"""X (Twitter) Following-timeline collector.

A **login-type** collector: it reads the authenticated user's own Home
timeline through X's private GraphQL endpoint. That is permitted only under
ADR 0012, whose conditions are load-bearing here — the credentials come from
the operator, the account is theirs, and a missing or expired credential stops
as `auth` rather than being worked around.

This module is the pure half: bytes in, `ItemDraft` out. The request itself
lives in `client.py`, and the paging cursor in `paging.py`. Splitting it this
way is what lets the whole of X's response shape be tested offline against
recorded payloads, with no credential in the fixture.

Three things about the upstream data are not obvious and are the reason this
file is longer than a JSON walk should be:

- **Entity indices cannot be trusted.** X documents them as code-point offsets,
  but a wrong span is not a crash: it silently links the wrong words and the run
  looks healthy. So every span is checked against the `t.co` text X puts at it,
  and an entity that matches nothing is dropped rather than applied.
- **The same fact arrives at several paths.** A quoted tweet sits at the top
  level in current responses and under `legacy` in older ones; `binding_values`
  is a map in some payloads and an array in others. Accepting only one shape
  loses content silently rather than loudly, so both are read and each has a
  fixture.
- **`rest_id` is a string from the live API and an integer in recorded
  fixtures.** Everything that leaves this module is text.
"""

from __future__ import annotations

import html
import json
import re
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import httpx

from adapters.raw_payload import RawPayload
from adapters.sanitize import AVATAR_CLASS
from core.enums import ErrorClass
from core.models import ItemDraft
from core.plugins.hooks import (
    HOOKIMPL,
    BackfillHooks,
    CollectorError,
    ConfigRequirement,
    Manifest,
)

# X's own title budget is 100 characters; longer than that and a feed list
# becomes unreadable rather than more informative.
TITLE_MAX = 100
QUOTED_PREVIEW_MAX = 140

_TCO_RE = re.compile(r"https?://t\.co/\S+", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")
_CREATED_AT_RE = re.compile(
    r"^[A-Za-z]{3} ([A-Za-z]{3}) +(\d{1,2}) "
    r"(\d{2}):(\d{2}):(\d{2}) ([+-]\d{4}) (\d{4})$"
)
_MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}  # fmt: skip


Json = dict[str, Any]


# --------------------------------------------------------------------------
# Public collector interface
# --------------------------------------------------------------------------


@HOOKIMPL
def match_url(url: str) -> bool:
    """Whether this collector handles the URL.

    Only the explicit form. This is a login-type source under ADR 0012, so it
    must be *declared* rather than inferred: silently attaching the operator's
    session to a URL that merely looks like x.com would send credentials
    somewhere nobody authorised.
    """
    return url.strip().lower().startswith(("x.com/home", "https://x.com/home"))


@HOOKIMPL
def parse(raw: RawPayload) -> list[ItemDraft]:
    """Turn one HomeLatestTimeline page into drafts.

    Raises rather than returning an empty list on a structurally empty payload.
    X answers `{"data": null}` when risk control suppresses the response, and
    that looks exactly like "there is nothing new" to a caller that reads an
    empty list as success — which is how a blocked collector can report a
    healthy run for weeks.
    """
    # _tweets validates through _instructions, so a suppressed or malformed
    # payload raises here rather than arriving as an empty list.
    return [
        _draft_from_tweet(tweet, source_id=raw.source_id, raw_fetch_id=raw.raw_fetch_id)
        for tweet in _tweets(raw.body)
    ]


# --------------------------------------------------------------------------
# Page structure
# --------------------------------------------------------------------------


def _instructions(body: bytes | str) -> list[Json]:
    """The instruction list, or raise with the right classification."""
    try:
        text = body.decode("utf-8") if isinstance(body, bytes) else body
        data = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CollectorError(ErrorClass.PARSE, f"undecodable payload: {exc}") from exc

    if not isinstance(data, dict):
        raise CollectorError(ErrorClass.PARSE, "payload is not a JSON object")

    if data.get("errors"):
        # X reports endpoint-level failures alongside a partial body. Loud.
        raise CollectorError(
            ErrorClass.STRUCTURE_CHANGED,
            f"endpoint reported errors: {_describe_errors(data['errors'])}",
        )

    home = (
        (data.get("data") or {}).get("home")
        if isinstance(data.get("data"), dict)
        else None
    )
    if not isinstance(home, dict):
        # `data: null` is X's risk-control stub, not the end of history.
        raise CollectorError(
            ErrorClass.BLOCKED,
            "structurally empty response (data is null); X risk control, "
            "not the end of history",
        )

    urt = home.get("home_timeline_urt")
    if not isinstance(urt, dict):
        raise CollectorError(ErrorClass.STRUCTURE_CHANGED, "home_timeline_urt missing")
    instructions = urt.get("instructions")
    if not isinstance(instructions, list):
        raise CollectorError(
            ErrorClass.STRUCTURE_CHANGED, "instructions missing or not a list"
        )
    return [i for i in instructions if isinstance(i, dict)]


def _entries(body: bytes | str) -> list[Json]:
    for instruction in _instructions(body):
        entries = instruction.get("entries")
        if isinstance(entries, list) and instruction.get("type") in (
            None,
            "TimelineAddEntries",
        ):
            return [e for e in entries if isinstance(e, dict)]
    return []


def _tweets(body: bytes | str) -> list[Json]:
    """The tweet results on a page, in the order X sent them.

    A page is a list of *entries*, and not all of them are tweets. Three
    shapes carry content or must be told apart, and the distinction is
    explicit rather than inferred from the entryId prefix:

    - **top-level tweets** (`tweet-<id>`) -- one tweet each;
    - **conversation modules** (`home-conversation-<id>`, a
      `TimelineTimelineModule`) -- a thread whose `content.items[]` each hold
      a full tweet. Measured on a real page these were 13 of 92 tweets, all
      of them replies, and every one was being dropped whole;
    - **promoted slots** (`promoted-tweet-<id>`) -- an advertisement. It
      carries a perfectly parseable `tweet_results`, so it must be *excluded
      on purpose*; keying off "not a `tweet-` prefix" happened to exclude it
      and would stop doing so the moment modules were handled.
    """
    tweets: list[Json] = []
    for entry in _entries(body):
        entry_id = str(entry.get("entryId", ""))
        if entry_id.startswith("promoted-tweet-") or _is_promoted(entry.get("content")):
            continue
        if entry_id.startswith("tweet-"):
            result = _result_of((entry.get("content") or {}).get("itemContent"))
            if isinstance(result, dict):
                tweets.append(_unwrap_visibility(result))
            continue
        if entry_id.startswith("home-conversation-"):
            for item in _module_items(entry.get("content")):
                result = _result_of(item)
                if isinstance(result, dict):
                    tweets.append(_unwrap_visibility(result))
    return tweets


def _is_promoted(content: object) -> bool:
    """Whether a top-level entry is an advertisement.

    Two independent signals, both checked because they answer different
    failure modes. The `promotedMetadata` field is what X's own clients read
    and survives a rename of the entryId prefix; the `promoted-tweet-` prefix
    is checked by the caller as a fast path.

    Measured on two real pages: all 18 promoted entries carried the metadata
    and the prefixed id, and none of the 168 ordinary tweets carried either --
    the signals never disagreed. Guarding on the field is what makes this
    falsifiable: without it, the entry would be ingested by the `tweet-`
    branch the moment X stops prefixing the id, and nothing would notice.
    """
    if not isinstance(content, dict):
        return False
    item_content = content.get("itemContent")
    if isinstance(item_content, dict) and "promotedMetadata" in item_content:
        return True
    # A module can in principle carry a promoted item too.
    return any("promotedMetadata" in item for item in _module_items(content))


def _module_items(content: object) -> list[Json]:
    """The `itemContent` of a `TimelineTimelineModule`'s items.

    A module wraps each of its tweets one level deeper than a top-level entry
    does (`items[].item.itemContent` rather than `content.itemContent`), so
    the unwrapping is named rather than inlined twice.

    A module's own `sortIndex` is a layout position, not the id range of the
    tweets inside it (measured: it differed from the items' ids by ~5e12), so
    callers must read order from the items and never from the module.
    """
    if not isinstance(content, dict):
        return []
    contents: list[Json] = []
    for item in content.get("items") or []:
        if not isinstance(item, dict):
            continue
        inner = item.get("item")
        if isinstance(inner, dict) and isinstance(inner.get("itemContent"), dict):
            contents.append(inner["itemContent"])
    return contents


def _result_of(item_content: object) -> object:
    """The `tweet_results.result` inside an `itemContent`.

    Takes the `itemContent`, not the enclosing `content`: a top-level entry
    keeps it at `content.itemContent` and a module item at
    `item.itemContent`, so resolving that difference belongs to the caller
    that knows which shape it is walking.
    """
    if not isinstance(item_content, dict):
        return None
    return (item_content.get("tweet_results") or {}).get("result")


def page_ids(body: bytes | str) -> list[str]:
    """Tweet ids on the page, newest first, for the paging frontier."""
    return [_text(_get(t, "rest_id")) for t in _tweets(body)]


def page_cursor(body: bytes | str) -> str | None:
    """The bottom cursor to request the next page, if the page carries one."""
    for entry in _entries(body):
        if str(entry.get("entryId", "")).startswith("cursor-bottom"):
            value = (entry.get("content") or {}).get("value")
            if isinstance(value, str) and value:
                return value
    return None


def page_sort_span(body: bytes | str) -> tuple[int, int] | None:
    """`(max, min)` sortIndex over the page's *tweets*, or None when none carry one.

    The client uses it to detect a gap between pages: X's timeline is live, so
    a tweet can be inserted after a page is taken and the next page can then
    start above where the previous one ended, skipping content that appears in
    no page at all. Continuity means the next page's max is below this page's
    min.

    Only tweets may bound the interval. Cursors carry a `sortIndex` too, and on
    a real page `cursor-top` sits exactly one above the newest tweet while
    `cursor-bottom` sits exactly one below the oldest -- so including them would
    widen the span by two and report a floor below the true one, which is the
    value gap detection compares against. A page with no sortable tweet returns
    None: continuity cannot be *proven* from it, which is a different claim from
    proving it holds.
    """
    sorts: list[int] = []
    for entry in _entries(body):
        entry_id = str(entry.get("entryId", ""))
        if entry_id.startswith("tweet-"):
            numeric = _sort_index(entry.get("sortIndex"))
            if numeric is not None:
                sorts.append(numeric)
            continue
        if entry_id.startswith("home-conversation-"):
            # The module's own sortIndex is a layout position, not the items'
            # range; each tweet inside carries its own id as its order.
            for item in _module_items(entry.get("content")):
                numeric = _id_sort_index(item)
                if numeric is not None:
                    sorts.append(numeric)
    if not sorts:
        return None
    return max(sorts), min(sorts)


def _sort_index(raw: object) -> int | None:
    if isinstance(raw, str) and raw.strip().isdigit():
        return int(raw)
    return None


def _id_sort_index(item_content: object) -> int | None:
    """A module item's own `rest_id`, which is its position in the timeline.

    Module items carry no `sortIndex` of their own, so the id is the order --
    tweet ids grow with time, which is the same property the paging frontier
    already relies on.
    """
    result = _result_of(item_content)
    if isinstance(result, dict):
        return _sort_index(_text(_get(_unwrap_visibility(result), "rest_id")))
    return None


# --------------------------------------------------------------------------
# Tweet -> ItemDraft
# --------------------------------------------------------------------------


def _draft_from_tweet(
    tweet: Json, *, source_id: str, raw_fetch_id: str | None
) -> ItemDraft:
    retweet = _retweet_parts(tweet)

    if retweet is not None:
        wrapper, original = retweet
        # Body, title and time come from the original; the author stays the
        # retweeter, and a banner says so. Without that a reader sees original
        # text with no indication of where it came from.
        full_text = _resolve_full_text(original)
        published = _parse_created_at(_text(_dig(original, "legacy", "created_at")))
        content = _build_content(
            original, full_text, prefix=_retweet_banner(wrapper), published=published
        )
    else:
        full_text = _resolve_full_text(tweet)
        published = _parse_created_at(_text(_dig(tweet, "legacy", "created_at")))
        content = _build_content(tweet, full_text, prefix=None, published=published)

    title = _title_from_text(
        full_text,
        marker=_type_marker(
            retweet=retweet is not None, quoted=_quoted_result(tweet) is not None
        ),
    )
    author = _author_label(tweet)

    metadata: dict[str, str] = {}
    if author:
        metadata["author"] = author
    if retweet is not None:
        metadata["retweet"] = "true"

    return ItemDraft(
        source_id=source_id,
        external_id=f"twitter:{_text(_get(tweet, 'rest_id'))}",
        title=title,
        canonical_url=_tweet_link(tweet),
        published_at=published,
        content=content,
        raw_fetch_id=raw_fetch_id,
        metadata=metadata,
    )


def _retweet_parts(tweet: Json) -> tuple[Json, Json] | None:
    """`(wrapper, original)` when this tweet is genuinely a re-post.

    HomeLatestTimeline often puts the original tweet at the top level with a
    `retweeted_status_result` that is the *same* tweet. Treating that as a
    retweet would render every ordinary tweet with a "retweeted by" banner
    pointing at itself, so the identity comparison is required, not optional.
    """
    legacy = tweet.get("legacy") or {}
    candidate = (legacy.get("retweeted_status_result") or {}).get("result")
    if not isinstance(candidate, dict):
        return None
    original = _unwrap_visibility(candidate)
    same_id = _text(_get(tweet, "rest_id")) == _text(_get(original, "rest_id"))
    same_author = _screen_name(tweet) == _screen_name(original)
    if same_id and same_author:
        return None
    return tweet, original


def _tweet_link(tweet: Json) -> str:
    return f"https://x.com/{_screen_name(tweet)}/status/{_text(_get(tweet, 'rest_id'))}"


def _retweet_banner(retweeter: Json) -> str:
    label = _author_label(retweeter) or f"@{_screen_name(retweeter)}"
    return f"<p>↻ <strong>{html.escape(label)}</strong> 转推了</p>"


def _resolve_full_text(tweet: Json) -> str:
    """The whole body, preferring the NoteTweet over the truncated preview.

    Past 280 characters X puts the full text in `note_tweet` and leaves
    `legacy.full_text` as a preview ending in an ellipsis. Reading only the
    legacy field publishes a truncated post and looks entirely successful.
    """
    note = _dig(tweet, "note_tweet", "note_tweet_results", "result", "text")
    if isinstance(note, str) and note.strip():
        return note
    legacy = _dig(tweet, "legacy", "full_text")
    return legacy if isinstance(legacy, str) else ""


def _unwrap_visibility(tweet: Json) -> Json:
    """Unwrap `TweetWithVisibilityResults`, which nests the real tweet."""
    if tweet.get("__typename") == "TweetWithVisibilityResults":
        inner = tweet.get("tweet")
        if isinstance(inner, dict):
            return inner
    return tweet


def _screen_name(tweet: Json) -> str:
    return (
        _text(_dig(tweet, "core", "user_results", "result", "core", "screen_name"))
        or _text(_dig(tweet, "core", "user_results", "result", "legacy", "screen_name"))
        or "unknown"
    )


def _user_core(tweet: Json) -> Json:
    core = _dig(tweet, "core", "user_results", "result", "core")
    return core if isinstance(core, dict) else {}


def _author_label(tweet: Json) -> str:
    core = _user_core(tweet)
    name = _text(core.get("name"))
    screen = _text(core.get("screen_name"))
    if name and screen:
        return f"{name} (@{screen})"
    if screen:
        return f"@{screen}"
    return name


# --------------------------------------------------------------------------
# Content assembly
# --------------------------------------------------------------------------


def _build_content(
    tweet: Json,
    full_text: str,
    *,
    prefix: str | None,
    published: datetime | None = None,
) -> str:
    parts = [
        prefix,
        _author_html(tweet, published=published),
        _text_to_html(full_text, _content_urls(tweet)),
        _media_html(_media_items(tweet)),
        _quoted_html(tweet),
        _card_html(tweet),
    ]
    return "\n".join(p for p in parts if p and p.strip())


def _content_urls(tweet: Json) -> list[Json]:
    """URL entities, keyed to whichever text is being rendered.

    NoteTweet entities index into the note text and legacy entities into
    `full_text`; pairing note text with legacy indices would link the wrong
    spans.
    """
    note = _dig(
        tweet, "note_tweet", "note_tweet_results", "result", "entity_set", "urls"
    )
    if isinstance(note, list):
        return [u for u in note if isinstance(u, dict)]
    legacy = _dig(tweet, "legacy", "entities", "urls")
    return (
        [u for u in legacy if isinstance(u, dict)] if isinstance(legacy, list) else []
    )


def _time_html(published: datetime | None) -> str | None:
    """The post's time as a `<time>` element, or None when there is no date.

    `published_at` was already parsed but appeared nowhere in the body, so a
    reader had no date at all -- the reader's list view shows titles and
    ordering, not timestamps.

    The `datetime` attribute carries the machine-readable instant and the text
    carries the visible one; the sanitiser admits `time[datetime]` (see
    `ALLOWED_ATTRIBUTES`), so both survive storage. A missing date yields
    nothing rather than a fabricated one.
    """
    if published is None:
        return None
    stamp = published.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    machine = published.astimezone(UTC).isoformat()
    return f'<time datetime="{html.escape(machine, quote=True)}">{stamp}</time>'


def _author_html(tweet: Json, *, published: datetime | None = None) -> str | None:
    core = _user_core(tweet)
    name = _text(core.get("name"))
    screen = _text(core.get("screen_name"))
    avatar = _text(_dig(tweet, "core", "user_results", "result", "avatar", "image_url"))

    if name and screen:
        author = f"<strong>{html.escape(name)}</strong> @{html.escape(screen)}"
    elif screen:
        author = f"@{html.escape(screen)}"
    elif name:
        author = html.escape(name)
    else:
        author = ""

    # `width`/`height` rather than the upstream inline `style`: the store-time
    # sanitiser (ADR 0007) drops `style`, so a style attribute would be
    # silently discarded and the avatar would render full size. The square
    # 24px box plus `alt` is what makes this read as a portrait when the
    # reader styles nothing.
    #
    # `class="pearss-avatar"` is the one class the sanitiser admits (a fixed
    # constant, checked by value in adapters.sanitize), so a reader or theme
    # that wants round avatars can hang `border-radius: 50%` off it. The class
    # is an invitation, not an instruction: a client that ships no such rule
    # renders the image exactly as before.
    #
    # `alt` carries the display name rather than being empty: a broken or
    # blocked avatar URL then degrades to the author's name instead of to
    # nothing, and screen readers announce who the picture belongs to. It is
    # the same text the byline already shows, which is what makes the image
    # and the name recognisably one author.
    image = (
        f'<img src="{html.escape(avatar, quote=True)}"'
        f' alt="{html.escape(name or screen, quote=True)}"'
        f' class="{AVATAR_CLASS}"'
        f' width="24" height="24" loading="lazy"/>'
        if avatar
        else ""
    )
    time_html = _time_html(published)
    # The time rides on the byline rather than opening its own line: it belongs
    # to the same piece of context (who posted, and when) and a separate
    # paragraph would read as a second author line.
    separator = " · " if (author and time_html) else ""
    if not (image or author or time_html):
        return None
    return f"<p>{image}{author}{separator}{time_html or ''}</p>"


def _media_items(tweet: Json) -> list[Json]:
    legacy = tweet.get("legacy") or {}
    extended = (legacy.get("extended_entities") or {}).get("media")
    if isinstance(extended, list) and extended:
        return [m for m in extended if isinstance(m, dict)]
    plain = (legacy.get("entities") or {}).get("media")
    return [m for m in plain if isinstance(m, dict)] if isinstance(plain, list) else []


def _media_html(media: list[Json], *, quote: bool = False) -> str | None:
    """Media items as markup, optionally wrapped for a quote block.

    `quote=True` drops the enclosing `<p>` because a `<blockquote>` holds the
    quote's own content; nesting block elements inside one is what the
    quoted-tweet tests pin.
    """
    out: list[str] = []
    for item in media:
        kind = _text(item.get("type"))
        url = _text(item.get("media_url_https"))
        expanded = _text(item.get("expanded_url"))
        size = _dimensions(item)
        if kind == "photo" and url:
            out.append(f'<img src="{html.escape(url, quote=True)}"{size} alt=""/>')
        elif kind in {"video", "animated_gif"} and expanded:
            label = "▶ 视频" if kind == "video" else "🎞 动图"
            href = html.escape(expanded, quote=True)
            if url:
                img = (
                    f'<img src="{html.escape(url, quote=True)}"{size} alt="{label}" '
                    f'title="{label}"/>'
                )
                out.append(f'<a href="{href}">{img}</a>')
            else:
                out.append(f'<a href="{href}">{label}</a>')
    if not out:
        return None
    if quote:
        return "".join(out)
    return "\n".join(f"<p>{chunk}</p>" for chunk in out)


def _dimensions(item: Json) -> str:
    """` width="N" height="N"` from the media's stated size, or "".

    Reserving the box before the image loads is what stops a timeline from
    reflowing under the reader as pictures arrive. The payload carries the true
    size in `original_info` (every photo on a measured page had it), so nothing
    is guessed here: when it is absent the attributes are simply omitted, and
    an unsized image still renders.
    """
    info = item.get("original_info")
    if not isinstance(info, dict):
        return ""
    width = info.get("width")
    height = info.get("height")
    # `bool` is an `int` subclass, so `isinstance(True, int)` holds and a
    # boolean would render as `width="True"`. X sends integers, but the
    # guard should not depend on that: reject bools explicitly.
    for value in (width, height):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            return ""
    return f' width="{width}" height="{height}"'


def _quoted_result(tweet: Json) -> Json | None:
    """The quoted tweet, from any of the three paths X has used.

    Current responses put `quoted_status_result` at the top level; older ones
    nest it, or the whole quoted object, under `legacy`. Reading only one path
    drops quoted tweets silently -- they parse fine, they just lose the quote.
    """
    for path in (
        ("quoted_status_result", "result"),
        ("legacy", "quoted_status_result", "result"),
        ("legacy", "quoted_status"),
    ):
        found = _dig(tweet, *path)
        if isinstance(found, dict) and found:
            return found
    return None


def _quoted_html(tweet: Json) -> str | None:
    """The quoted tweet as a `<blockquote>`, with its author's face and media.

    A quote names someone else's post, so the byline and the avatar are what
    make it read as theirs rather than as the quoter's own aside -- every
    quoted tweet on a measured page carried an avatar, and 10 of 29 carried
    media of their own, which a text-only block silently dropped.
    """
    quoted = _quoted_result(tweet)
    if quoted is None:
        return None
    quoted = _unwrap_visibility(quoted)
    text = _WS_RE.sub(" ", _resolve_full_text(quoted)).strip()
    author = _author_label(quoted)
    screen = _screen_name(quoted)
    rest = _text(_get(quoted, "rest_id"))

    pieces: list[str] = []
    byline = _quoted_byline(quoted, author)
    if byline:
        pieces.append(byline)
    if text:
        preview = text[:QUOTED_PREVIEW_MAX]
        if len(text) > QUOTED_PREVIEW_MAX:
            preview += "…"
        pieces.append(html.escape(preview))
    media = _media_html(_media_items(quoted), quote=True)
    if media:
        pieces.append(media)
    if rest and screen and screen != "unknown":
        pieces.append(
            f'<a href="https://x.com/{html.escape(screen, quote=True)}'
            f'/status/{html.escape(rest, quote=True)}">查看被引推文</a>'
        )
    if not pieces:
        return None
    return "<blockquote>" + " ".join(pieces) + "</blockquote>"


def _quoted_byline(quoted: Json, author: str) -> str:
    """The quoted author's avatar (when there is one) followed by their name."""
    avatar = _text(
        _dig(quoted, "core", "user_results", "result", "avatar", "image_url")
    )
    name = _text(_user_core(quoted).get("name"))
    screen = _text(_user_core(quoted).get("screen_name"))
    if not author and not avatar:
        return ""
    image = (
        f'<img src="{html.escape(avatar, quote=True)}"'
        f' alt="{html.escape(name or screen, quote=True)}"'
        f' class="{AVATAR_CLASS}"'
        f' width="24" height="24" loading="lazy"/>'
        if avatar
        else ""
    )
    label = f"<strong>{html.escape(author)}</strong>" if author else ""
    return f"{image}{label}"


def _binding(bindings: object, key: str) -> str:
    """One card binding value, from either shape X emits.

    `binding_values` arrives as a map in some payloads and as an array of
    key/value pairs in others. A caller that assumes one raises on the other,
    which turns a card into a failed collection.
    """
    if isinstance(bindings, dict):
        value = bindings.get(key)
        if isinstance(value, dict):
            return _text(value.get("string_value"))
        return ""
    if isinstance(bindings, list):
        for pair in bindings:
            if isinstance(pair, dict) and pair.get("key") == key:
                value = pair.get("value")
                if isinstance(value, dict):
                    return _text(value.get("string_value"))
    return ""


def _card_html(tweet: Json) -> str | None:
    card = _dig(tweet, "card", "legacy")
    if not isinstance(card, dict):
        return None
    name = _text(card.get("name"))
    if not name:
        return None
    bindings = card.get("binding_values")
    if name.startswith("poll"):
        return _poll_html(bindings)
    if name.startswith("summary") or name in {"app", "player"}:
        return _link_card_html(bindings)
    return None


def _poll_html(bindings: object) -> str | None:
    title = _binding(bindings, "title") or "投票"
    choices = [
        value
        for key in ("choice1_label", "choice2_label", "choice3_label", "choice4_label")
        if (value := _binding(bindings, key))
    ]
    if choices:
        joined = html.escape(" / ".join(choices))
        return f"<p>🗳 {html.escape(title)} ({joined})</p>"
    return f"<p>🗳 {html.escape(title)}</p>"


def _link_card_html(bindings: object) -> str | None:
    title = _binding(bindings, "title") or "链接预览"
    description = _binding(bindings, "description")
    domain = _binding(bindings, "domain")
    url = _binding(bindings, "url")

    head = (
        f'<a href="{html.escape(url, quote=True)}">{html.escape(title)}</a>'
        if url
        else html.escape(title)
    )
    tail = " — ".join(html.escape(part) for part in (description, domain) if part)
    if not tail:
        return f"<p>{head}</p>"
    return f"<p>{head} — {tail}</p>"


# --------------------------------------------------------------------------
# Text handling
# --------------------------------------------------------------------------


def _text_to_html(text: str, urls: list[Json]) -> str | None:
    """Body text with URL spans turned into links.

    Escaping happens per segment, never over the whole string: escaping first
    would rewrite the generated `<a href>` markup into visible text.
    """
    if not text:
        return None
    rendered = "".join(
        html.escape(body).replace("\n", "<br/>")
        if kind == "text"
        else _link_html(href, display)
        for kind, body, href, display in _split_segments(text, urls)
    )
    return f"<p>{rendered}</p>" if rendered.strip() else None


def _link_html(href: str, display: str) -> str:
    if not href:
        return ""
    return (
        f'<a href="{html.escape(href, quote=True)}">{html.escape(display or href)}</a>'
    )


def _split_segments(text: str, urls: list[Json]) -> list[tuple[str, str, str, str]]:
    """Split into `("text", body, "", "")` and `("url", "", href, display)`.

    Only the plain segments are escaped, so a URL becomes a link instead of
    escaped text.
    """
    spans: list[tuple[int, int, str, str]] = []
    for entry in urls:
        indices = entry.get("indices")
        if not (isinstance(indices, list) and len(indices) == 2):
            continue
        start, end = indices
        if not (isinstance(start, int) and isinstance(end, int)) or end <= start:
            continue
        href = _text(entry.get("expanded_url")) or _text(entry.get("url"))
        if not href:
            continue
        resolved = _resolve_span(text, start, end, _text(entry.get("url")))
        if resolved is None:
            continue
        low, high = resolved
        spans.append((low, high, href, _text(entry.get("display_url")) or href))

    spans.sort()
    segments: list[tuple[str, str, str, str]] = []
    position = 0
    for low, high, href, display in spans:
        # Overlapping or out-of-order indices are tolerated rather than
        # trusted: clamping keeps a malformed entity from slicing backwards.
        low = max(low, position)
        high = max(high, low)
        if low > position:
            segments.append(("text", text[position:low], "", ""))
        segments.append(("url", "", href, display))
        position = high
    if position < len(text):
        segments.append(("text", text[position:], "", ""))
    return segments


def _resolve_span(
    text: str, start: int, end: int, expected: str
) -> tuple[int, int] | None:
    """Where the entity actually sits, validated against X's own text.

    X documents these indices as code-point offsets, and Python indexes strings
    by code point, so the documented reading is used directly. The implementation
    this is ported from carries a comment claiming UTF-16 units in one place
    while counting code points in its code; a UTF-16 reading was tried here too
    and then removed, because nothing in the recorded data needs it and no test
    could reach it -- the literal search below covers every case it would have.
    A speculative branch that no test can fail is worse than no branch, since it
    reads as handled while never being exercised.

    The span is still checked against the `t.co` text X puts at it, because a
    wrong span is not a crash: it silently links the wrong words and the run
    looks entirely healthy. If the indices do not reproduce that text, the
    literal is located instead; if that fails too the entity is dropped, which
    leaves the words intact and unlinked rather than garbled.
    """
    low, high = max(0, min(start, len(text))), max(0, min(end, len(text)))

    if not expected:
        # Nothing to validate against; trust the span only if it is well formed.
        return (low, high) if high > low else None

    if text[low:high] == expected:
        return low, high

    found = text.find(expected)
    if found != -1:
        return found, found + len(expected)

    return None


def _title_from_text(text: str, *, marker: str = "") -> str:
    """First line, t.co links removed, cut at a word boundary.

    A tweet has no title, so the opening line stands in. Truncating mid-word
    reads as a defect; dropping the partial word does not.

    `marker` is a short type prefix (`RT ` / `QT `) that the reader's list view
    uses to tell a re-post or a quote from an original post. It is spent out of
    the same `TITLE_MAX` budget rather than added on top, so a marked title is
    never longer than an unmarked one -- the limit bounds the title a reader
    actually sees.
    """
    body = _plain_title(text)
    # Reserve the marker's width, then clip the body to what is left: the two
    # together are what TITLE_MAX bounds. With no marker this is the whole
    # budget, which is why the truncation cannot be skipped in that case.
    trimmed = _truncate_at_word_boundary(body, TITLE_MAX - len(marker))
    return f"{marker}{trimmed}"


def _plain_title(text: str) -> str:
    """The unmarked title body: first line, links removed, no length limit yet."""
    stripped = _TCO_RE.sub("", text)
    first = stripped.split("\n", 1)[0]
    collapsed = _WS_RE.sub(" ", first).strip()
    if not collapsed:
        return _plain_text(text)
    return collapsed


def _type_marker(*, retweet: bool, quoted: bool) -> str:
    """The title prefix for a tweet's type, or "" for an original post.

    Retweet wins when both are true -- a retweeted quote is, at the top level,
    a re-post, and the quote is already rendered in the body's blockquote, so
    marking it `QT ` would hide the more useful fact.

    Deliberately bare: the handle is not part of the marker. X's own
    `RT @user: text` form duplicates the author that `metadata` already
    carries and leaves the reader guessing whose words are shown.
    """
    if retweet:
        return "RT "
    if quoted:
        return "QT "
    return ""


def _truncate_at_word_boundary(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    cut = value[:limit].rstrip()
    rest = value[limit:]
    if not rest[:1].isspace():
        # The cut landed inside a word; keep only whole words.
        without_last = " ".join(cut.split()[:-1])
        cut = without_last or cut
    return f"{cut}…"


def _plain_text(text: str) -> str:
    return _WS_RE.sub(" ", _TCO_RE.sub("", text)).strip()


# --------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------


def _parse_created_at(value: str) -> datetime | None:
    """X's `created_at`, e.g. `Sat Aug 08 22:16:06 +0000 2026`.

    The offset is honoured rather than assumed. The upstream implementation
    rejects anything that is not `+0000` and returns no timestamp; X does send
    UTC in practice, but discarding a real time because it arrived with a zone
    we did not expect is worse than converting it, and the collector guide asks
    for the offset to be recorded rather than guessed away.
    """
    match = _CREATED_AT_RE.match(value.strip()) if value else None
    if not match:
        return None
    month_name, day, hour, minute, second, offset, year = match.groups()
    month = _MONTHS.get(month_name)
    if month is None:
        return None
    try:
        sign = 1 if offset[0] == "+" else -1
        hours, minutes = int(offset[1:3]), int(offset[3:5])
        zone = timezone(sign * timedelta(hours=hours, minutes=minutes))
        moment = datetime(
            int(year), month, int(day), int(hour), int(minute), int(second), tzinfo=zone
        )
    except ValueError:
        return None
    return moment.astimezone(UTC)


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _dig(data: object, *path: str) -> Any:
    """Walk a path of dict keys without caring where it stops existing."""
    current: Any = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _get(tweet: Json, key: str) -> Any:
    return tweet.get(key)


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value)


def _describe_errors(errors: object) -> str:
    if not isinstance(errors, list):
        return _text(errors)[:120]
    parts = [_text(item.get("message")) for item in errors if isinstance(item, dict)]
    return "; ".join(p for p in parts if p)[:200] or "unspecified"


__all__ = [
    "CollectorError",
    "match_url",
    "page_cursor",
    "page_ids",
    "page_sort_span",
    "parse",
]


def collect_pages(
    *,
    source_id: str,
    source_url: str,
    client: httpx.Client,
    state: Any,
    max_pages: int = 20,
) -> Any:
    """The paged-collection capability (registry `paged=True`).

    Thin binding: the walk logic lives in `walk.py`, which the pipeline used
    to inline. Exposed here so the registry's paged dispatch finds it on the
    collector module the registration points at.
    """
    from plugins.collectors.twitter import walk

    return walk.collect_pages(
        source_id=source_id,
        source_url=source_url,
        client=client,
        state=state,
        max_pages=max_pages,
    )


@HOOKIMPL
def source_config() -> Manifest:
    """The twitter collector's manifest."""
    return Manifest(
        id="twitter",
        version="0.1.0",
        name="X Following 时间线",
        display_title="X Following",
        description="读取你自己的 X Following 时间线（登录型，ADR 0012）。",
        example_url="https://x.com/home",
        features=frozenset({"paged"}),
        required_config=(
            ConfigRequirement(
                env_name="PEARSS_X_AUTH_TOKEN",
                description="X 会话 auth_token cookie（或经 CookieCloud）",
            ),
            ConfigRequirement(
                env_name="PEARSS_X_CT0",
                description="X 会话 ct0 cookie（或经 CookieCloud）",
            ),
        ),
    )


@HOOKIMPL
def collector_id() -> str:
    """The stable registration key (ADR 0017)."""
    return "twitter"


@HOOKIMPL
def backfill() -> BackfillHooks | None:
    """This origin has no backward walk."""
    return None
