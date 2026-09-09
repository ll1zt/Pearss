"""Telegram public-channel collector.

A P1 quasi-plugin alongside `rss.py`: it follows the interface in
docs/guides/collector-guide.md and is registered explicitly rather than
discovered. It reads the public web preview at `https://t.me/s/<username>` —
no Bot API token, no login.

The preview is HTML, not a feed, so this is the project's first web collector
and the second of the two-to-three the guide wants before P2 freezes a
hookspec. Two things make it harder than parsing a feed:

- Message text is HTML we did not write, so it goes through the same sanitiser
  as feed bodies before it is ever stored (see adapters/sanitize.py).
- Preview photos are CSS `background-image` on a wrapper element, not
  `<img src>`. A parser that only looked for `<img>` would silently drop every
  photo in a photo-heavy channel.

Pagination via `?before=<id>` is implemented but off by default: one page is
what the origin shows, and walking backwards is opt-in (`max_pages`) because
each page is a request against someone else's server.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit

import httpx
from lxml.html import HtmlElement, fromstring, tostring

from adapters.http import fetch as plain_fetch
from adapters.raw_payload import ConditionalState, RawPayload
from core.enums import ErrorClass
from core.models import ItemDraft
from core.plugins.hooks import HOOKIMPL, BackfillHooks, CollectorError, Manifest

LOGGER = logging.getLogger(__name__)

PREVIEW_HOSTS = ("t.me", "www.t.me", "telegram.me", "www.telegram.me")

# Telegram usernames are 5-32 characters in the API, but preview pages resolve
# shorter legacy names too, so the floor is lower than the API's.
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{4,64}$")

# Where the preview page puts each piece of a message.
_MESSAGE_SELECTOR = "div.tgme_widget_message[data-post]"
_TEXT_SELECTOR = ".tgme_widget_message_text.js-message_text, .tgme_widget_message_text"
_TIME_SELECTOR = "time[datetime]"
_OWNER_SELECTOR = ".tgme_widget_message_owner_name"
_PHOTO_SELECTOR = "a.tgme_widget_message_photo_wrap, div.tgme_widget_message_photo_wrap"
_VIDEO_SELECTOR = (
    "a.tgme_widget_message_video_player, "
    "a.tgme_widget_message_video_wrap, div.tgme_widget_message_video_wrap"
)

# Telegram serves emoji as images from a fixed path; they are decoration, not
# content, and inlining them would put hundreds of identical icons in a body.
_NOISE_URL_PARTS = ("/emoji/", "telegram.org/img/")

TITLE_MAX = 120
MEDIA_ONLY_TITLE = "(photo)"
EMPTY_TITLE = "(media)"


@HOOKIMPL
def match_url(url: str) -> bool:
    """Whether this collector should handle `url`.

    Purely a URL test: no network and no database, because source selection
    runs before a fetch is scheduled.
    """
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if host not in PREVIEW_HOSTS:
        return False
    return _username_from_path(parts.path) is not None


def channel_url(username: str) -> str:
    """The preview URL for `username`."""
    return f"https://t.me/s/{username}"


def username_of(url: str) -> str:
    """The channel username in `url`.

    Raises ValueError when there is none, so a malformed source URL fails at
    creation instead of at every fetch.
    """
    username = _username_from_path(urlsplit(url.strip()).path)
    if username is None:
        raise ValueError(f"not a Telegram channel URL: {url!r}")
    return username


def before_url(url: str, message_id: str) -> str:
    """`url` carrying a `?before=` cursor for one page further back.

    Kept as a URL rather than a page number because that is what the origin
    accepts, and because a caller that wants to resume a backfill can persist
    the cursor itself.
    """
    parts = urlsplit(url)
    query = f"before={message_id}"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


@HOOKIMPL
def fetch(
    url: str,
    *,
    client: httpx.Client,
    source_id: str,
    conditional: ConditionalState | None = None,
) -> RawPayload:
    """Fetch the channel's web preview, not the source URL itself.

    A subscriber writes `https://t.me/<name>`; the readable, scraping-friendly
    page lives at `https://t.me/s/<name>`. Without this hook the pipeline's
    plain fetch requested the bare URL — whose page is the app-install
    interstitial, carrying no messages — and parse classified the result as
    structure_changed. The preview rewrite is collection knowledge, which is
    why it lives in the collector as a fetch hook (the same shape zhihu uses
    to reach its signed API).
    """
    return plain_fetch(
        channel_url(username_of(url)),
        client=client,
        source_id=source_id,
        conditional=conditional,
    )


@HOOKIMPL
def parse(raw: RawPayload) -> list[ItemDraft]:
    """Turn a fetched preview page into drafts.

    Raises CollectorError with a classified reason rather than returning an
    empty list: "Telegram changed its markup" and "this channel has no posts"
    are different situations, and collapsing them is how a broken selector
    goes unnoticed for months.
    """
    if not raw.body:
        raise CollectorError(ErrorClass.PARSE, "empty payload")

    try:
        document = fromstring(raw.text())
    except Exception as exc:  # lxml raises several types for broken markup
        raise CollectorError(ErrorClass.PARSE, f"undecodable HTML: {exc}") from exc

    username = _username_from_url(raw.url)
    nodes = document.cssselect(_MESSAGE_SELECTOR)
    if not nodes:
        # A 200 with no messages is the signature of either a redesigned page
        # or a channel that requires a login to preview. Both need attention,
        # and neither is "no new posts" — an empty channel still renders its
        # shell, so reaching here means the selector missed.
        raise CollectorError(
            ErrorClass.STRUCTURE_CHANGED,
            f"no {_MESSAGE_SELECTOR} elements in the preview page",
        )

    channel_title = _channel_title(document) or username
    drafts: list[ItemDraft] = []
    for node in nodes:
        data_post = node.get("data-post")
        if not data_post:
            # The selector guarantees the attribute; a miss means the markup
            # changed shape underneath us.
            continue
        channel, message_id = _split_post(data_post, username)
        drafts.append(
            _draft_from_node(
                node,
                source_id=raw.source_id,
                channel=channel,
                message_id=message_id,
                channel_title=channel_title,
                raw_fetch_id=raw.raw_fetch_id,
            )
        )

    if not drafts:
        raise CollectorError(
            ErrorClass.STRUCTURE_CHANGED,
            f"{len(nodes)} message nodes lacked a usable data-post",
        )
    return drafts


def oldest_message_id(drafts: list[ItemDraft]) -> str | None:
    """The id to pass as `?before=` to continue a backfill.

    Returns None when there is nothing to continue from, which is how a caller
    knows a backfill is finished rather than merely empty.

    Takes the *first* draft, not the last: the preview page lists messages in
    ascending id order, so the oldest message on the page is at the top. Taking
    the last one instead yields the newest, and `?before=` then returns a page
    overlapping the one just fetched — a backfill that appears to make progress
    while re-reading the same messages.
    """
    if not drafts:
        return None
    return _message_id_of(drafts[0].external_id)


def oldest_cursor(drafts: list[ItemDraft]) -> str | None:
    """Backfill capability, named as the registry probes for it."""
    return oldest_message_id(drafts)


# --------------------------------------------------------------------------
# Message extraction
# --------------------------------------------------------------------------


def _draft_from_node(
    node: HtmlElement,
    *,
    source_id: str,
    channel: str,
    message_id: str,
    channel_title: str,
    raw_fetch_id: str | None,
) -> ItemDraft:
    text_html = _message_html(node)
    text_plain = _plain_text(text_html)
    media_urls = _media_urls(node)
    content = _compose_content(media_urls, text_html)
    title = _title(text_plain, text_html, media_urls)

    metadata: dict[str, str] = {"feed_title": channel_title}
    author = _owner_name(node)
    if author:
        metadata["author"] = author

    return ItemDraft(
        source_id=source_id,
        external_id=f"telegram:{channel}/{message_id}",
        title=title,
        canonical_url=f"https://t.me/{channel}/{message_id}",
        published_at=_published_at(node),
        content=content,
        raw_fetch_id=raw_fetch_id,
        metadata=metadata,
    )


def _attr(element: HtmlElement, name: str) -> str:
    """One attribute as text.

    lxml types attributes as Any, so every `.get()` would otherwise leak Any
    into a function declared to return str. Narrowing here keeps the call
    sites readable instead of sprinkling casts through the parser.
    """
    value = element.get(name)
    return value if isinstance(value, str) else ""


def _text_of(element: HtmlElement) -> str:
    """The element's text content, with lxml's untyped API narrowed once."""
    return str(element.text_content())


def _compose_content(media_urls: list[str], text_html: str | None) -> str:
    """Media first, then text — reading order matches the rendered preview."""
    parts: list[str] = []
    if media_urls:
        parts.append(_media_html(media_urls))
    if text_html and text_html.strip():
        parts.append(text_html)
    return "\n".join(parts)


def _media_html(urls: list[str]) -> str:
    return "\n".join(f'<p><img src="{_escape_attr(url)}" alt=""/></p>' for url in urls)


def _escape_attr(value: str) -> str:
    """Escape for an HTML attribute.

    Only the three characters that can break out of a quoted attribute: the
    markup here is generated, so escaping is about correctness, not about
    sanitising untrusted input — that happens later, in the store path.
    """
    return value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


def _message_html(node: HtmlElement) -> str | None:
    found = node.cssselect(_TEXT_SELECTOR)
    if not found:
        return None
    joined = "\n".join(_inner_html(_strip_decoration(el)) for el in found)
    return joined or None


def _strip_decoration(element: HtmlElement) -> HtmlElement:
    """Drop Telegram's inline emoji wrappers, keeping the character inside.

    An emoji is rendered as `<i class="emoji" style="background-image:url(
    //telegram.org/img/emoji/40/F09F988C.png)"><b>&#128524;</b></i>`: the image
    is decoration and the character is the content. Keeping the wrapper would
    put one background-image per emoji into every stored body — a single
    message can carry dozens — for something the plain text already says.

    The upstream Elixir parser this is ported from filters emoji only from the
    media URLs it extracts itself and leaves these inline wrappers in the text,
    which is why its fixture asserts only about `<img>` tags. In this project
    the body reaches RSS clients and the Reader API, where the noise is not
    merely cosmetic.
    """
    for wrapper in element.cssselect("i.emoji, .emoji"):
        style = _attr(wrapper, "style")
        if "emoji" in style or "/emoji/" in style:
            wrapper.drop_tag()
    return element


def _inner_html(element: HtmlElement) -> str:
    """The element's own markup, without its wrapping tag.

    Keeping the tag would nest every message in a `div` carrying Telegram's
    classes, which then survives into the stored body and the RSS output.
    """
    text = (element.text or "") + "".join(_tostring(child) for child in element)
    return text


def _tostring(element: HtmlElement) -> str:
    return str(tostring(element, encoding="unicode"))


def _plain_text(html: str | None) -> str:
    if not html:
        return ""
    try:
        fragment = fromstring(f"<div>{html}</div>")
    except Exception:
        return ""
    return re.sub(r"\s+", " ", _text_of(fragment)).strip()


def _media_urls(node: HtmlElement) -> list[str]:
    """Photo and video-poster URLs for one message.

    Photos arrive as a `background-image` style on the wrapper, so the URL has
    to be read out of CSS rather than an attribute.
    """
    candidates: list[str] = []
    for element in node.cssselect(_PHOTO_SELECTOR):
        candidates.extend(_urls_from_style(element))
    for element in node.cssselect(_VIDEO_SELECTOR):
        candidates.extend(_urls_from_style(element))
    for element in node.cssselect(".tgme_widget_message_bubble img[src]"):
        candidates.append(_attr(element, "src"))

    seen: set[str] = set()
    result: list[str] = []
    for url in candidates:
        normalised = _normalise_url(url)
        if normalised and normalised not in seen and not _is_noise(normalised):
            seen.add(normalised)
            result.append(normalised)
    return result


def _urls_from_style(element: HtmlElement) -> list[str]:
    style = _attr(element, "style")
    return [
        match.group(2)
        for match in re.finditer(
            r"background-image\s*:\s*url\((['\"]?)([^'\")]+)\1\)", style, re.IGNORECASE
        )
    ]


def _normalise_url(url: str) -> str | None:
    url = url.strip()
    if not url:
        return None
    if url.startswith("//"):
        return "https:" + url
    if url.startswith(("http://", "https://")):
        return url
    return None


def _is_noise(url: str) -> bool:
    return any(part in url for part in _NOISE_URL_PARTS)


def _title(text_plain: str, text_html: str | None, media_urls: list[str]) -> str:
    """A derived title, because a Telegram message has none.

    The first line of the message, truncated. Falls back to a media marker
    when there is no text at all, so a photo-only post still has something
    stable to show in a feed.
    """
    source = text_plain or _plain_text(text_html)
    if source:
        first_line = source.split("\n", 1)[0].strip()
        if first_line:
            return _truncate(first_line, TITLE_MAX)
    if media_urls:
        return MEDIA_ONLY_TITLE
    return EMPTY_TITLE


def _truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _owner_name(node: HtmlElement) -> str:
    found = node.cssselect(_OWNER_SELECTOR)
    if not found:
        return ""
    return _text_of(found[0]).strip()


def _published_at(node: HtmlElement) -> datetime | None:
    found = node.cssselect(_TIME_SELECTOR)
    if not found:
        return None
    return _parse_datetime(_attr(found[0], "datetime"))


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _channel_title(document: HtmlElement) -> str:
    found = document.cssselect("title")
    if not found:
        return ""
    title = _text_of(found[0]).strip()
    if not title:
        return ""
    # Preview titles read "<channel>" then a dash then the product name; the
    # dash is EN DASH or EM DASH because that is what the origin emits. The
    # escapes below are deliberate: substituting an ASCII hyphen would stop
    # matching, and every title would keep the product-name suffix.
    for separator in ("\u2013", "\u2014", " - "):
        if separator in title:
            return title.split(separator)[0].strip()
    return title


def _split_post(data_post: str, fallback: str) -> tuple[str, str]:
    """Split `data-post` into (channel, message id).

    Older previews carry only an id, leaving the channel to be supplied by the
    caller; both shapes have to work or historical fixtures stop parsing.
    """
    if "/" in data_post:
        channel, _, message_id = data_post.partition("/")
        if channel and message_id:
            return channel, message_id
    return fallback or "channel", data_post


def _message_id_of(external_id: str) -> str:
    return external_id.rsplit("/", 1)[-1]


# --------------------------------------------------------------------------
# URL handling
# --------------------------------------------------------------------------


def _username_from_path(path: str) -> str | None:
    """The channel username in a t.me path, or None.

    Accepts `/s/<name>` (preview), `/<name>` (permalink) and `/<name>/<id>`.
    Rejects the paths that are definitely not channels, so that a t.me link to
    something else does not get claimed by this collector.
    """
    segments = [seg for seg in path.split("/") if seg]
    if not segments:
        return None
    if segments[0] == "s":
        segments = segments[1:]
        if not segments:
            return None
    candidate = segments[0].lstrip("@")
    if not _USERNAME_RE.match(candidate):
        return None
    # Reserved paths that are not channels.
    if candidate.lower() in {"login", "joinchat", "addstickers", "share", "proxy"}:
        return None
    return candidate


def _username_from_url(url: str) -> str:
    try:
        return username_of(url)
    except ValueError:
        # The preview page itself identifies the channel in every data-post,
        # so a URL that lost its username during redirect is still parseable.
        return ""


__all__ = [
    "EMPTY_TITLE",
    "MEDIA_ONLY_TITLE",
    "CollectorError",
    "before_url",
    "channel_url",
    "match_url",
    "oldest_message_id",
    "parse",
    "username_of",
]


@HOOKIMPL
def source_config() -> Manifest:
    """The telegram collector's manifest."""
    return Manifest(
        id="telegram",
        version="0.1.0",
        name="Telegram 公开频道",
        description="读取 t.me 公开频道的网页预览。",
        example_url="https://t.me/durov",
        features=frozenset({"backfill"}),
        required_config=(),
    )


@HOOKIMPL
def backfill() -> BackfillHooks | None:
    """Telegram previews carry a `?before=` cursor; declare the walk."""
    return _TelegramBackfill()


class _TelegramBackfill(BackfillHooks):
    """Binds the module-level backfill functions to the declared capability."""

    def before_url(self, url: str, cursor: str) -> str | None:
        return before_url(url, cursor)

    def oldest_cursor(self, drafts: list[ItemDraft]) -> str | None:
        return oldest_cursor(drafts)


@HOOKIMPL
def collector_id() -> str:
    """The stable registration key (ADR 0017)."""
    return "telegram"
