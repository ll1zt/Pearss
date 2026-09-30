"""HTML sanitisation for untrusted feed content.

Feed bodies are attacker-controlled input: AGENTS.md says external content is
never to be treated as instructions, and ADR 0003 chose to pass markup through
to the reader rather than flatten it to text. Sanitising at the point the
content enters our storage is what makes those two compatible — the stored body
is already safe, so every output path (RSS, the Reader API, anything added
later) is safe without having to remember to do it again.

The allowlist is explicit rather than a deny-list of known-bad tags. A
deny-list has to anticipate every vector; an allow-list is correct by
construction and simply drops anything not named.
"""

from __future__ import annotations

import logging
from typing import Final, cast
from urllib.parse import urlparse

import nh3
from lxml import html as lxml_html

LOGGER = logging.getLogger(__name__)

# Formatting, structure, and media that a reader legitimately needs. No
# script/style/iframe/object/embed/form/input, and no svg or math.
ALLOWED_TAGS = frozenset(
    {
        "a",
        "abbr",
        "b",
        "blockquote",
        "br",
        "caption",
        "cite",
        "code",
        "col",
        "colgroup",
        "dd",
        "del",
        "details",
        "div",
        "dl",
        "dt",
        "em",
        "figcaption",
        "figure",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "hr",
        "i",
        "img",
        "ins",
        "kbd",
        "li",
        "mark",
        "ol",
        "p",
        "pre",
        "q",
        "rp",
        "rt",
        "ruby",
        "s",
        "samp",
        "section",
        "small",
        "span",
        "strong",
        "sub",
        "summary",
        "sup",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "time",
        "tr",
        "u",
        "ul",
        "var",
        "video",
        "wbr",
    }
)

# Global presentation attributes only. Event handlers (on*), `style`, `srcdoc`,
# `formaction` and every `data-*` are absent, so they are dropped.
#
# img carries `class`, but nothing about it is trusted to the whitelist: nh3
# only invokes `attribute_filter` for attributes the whitelist admits, so
# `class` is listed here and then held to a single constant value by the
# filter below. Anything else a remote body puts in that attribute is dropped
# whole -- the filter is the gatekeeper, the whitelist is only what lets the
# filter see the value.
ALLOWED_ATTRIBUTES = {
    "a": frozenset({"href", "title"}),
    "img": frozenset({"src", "alt", "title", "width", "height", "loading", "class"}),
    # No `autoplay`/`loop`/`muted`: autoplay is harassment and spends the
    # reader's data without asking (ADR 0043). `src` is host-constrained below.
    "video": frozenset({"src", "controls", "poster", "width", "height"}),
    "time": frozenset({"datetime"}),
    "td": frozenset({"colspan", "rowspan"}),
    "th": frozenset({"colspan", "rowspan", "scope"}),
    "col": frozenset({"span"}),
    "colgroup": frozenset({"span"}),
    "ol": frozenset({"start", "type"}),
    "q": frozenset({"cite"}),
    "blockquote": frozenset({"cite"}),
    "del": frozenset({"cite", "datetime"}),
    "ins": frozenset({"cite", "datetime"}),
}

# The one allowed class, and the mechanism that allows it. A class value is a
# styling hook, so the surface granted here is a single fixed constant rather
# than the attribute itself: `attribute_filter` receives every img class value
# and keeps exactly this one, so `class="avatar onerror-x"` survives as
# `class="pearss-avatar"` and anything else is dropped whole. A reader (or a
# FreshRSS theme) can then hang CSS off it -- border-radius is the point --
# and a client that ships no such rule renders the image exactly as before.
AVATAR_CLASS: Final = "pearss-avatar"

# The hosts a `<video src>` may point at (ADR 0043). This is the same move as
# the avatar class: what the allowlist grants is a *source*, not the tag -- an
# enumerable CDN, not "any https URL". A video from anywhere else is not a
# video we know what to do with, and admitting the tag unconditionally would
# turn it into an arbitrary remote-fetch and tracking channel.
VIDEO_HOSTS: Final = frozenset({"video.twimg.com"})


def video_url_allowed(url: str) -> bool:
    """Whether `url` is a media URL from an allowed video host (ADR 0043).

    Scheme is pinned to https, and the host is compared exactly: a suffix check
    would admit `video.twimg.com.evil.example`.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme == "https" and (parsed.hostname or "").lower() in VIDEO_HOSTS


def _attribute_filter(tag: str, attribute: str, value: str) -> str | None:
    """Keep an img class only when it is the avatar marker, a video src only
    from an allowed host.

    nh3 calls this for every whitelisted attribute, not just the two above, and
    None means "strip this attribute" -- so a non-target attribute must be
    passed through untouched, or the filter would strip every img's src. Only a
    class value that is not exactly the marker, or a video src on a foreign
    host, is refused.
    """
    if tag == "img" and attribute == "class":
        return AVATAR_CLASS if value.strip() == AVATAR_CLASS else None
    if tag == "video" and attribute == "src":
        return value if video_url_allowed(value) else None
    return value


def _drop_foreign_video(html: str) -> str:
    """Remove `<video>` elements that are not from an allowed host (ADR 0043).

    nh3's attribute filter strips a bad `src` but cannot remove the element, so
    a hostile `<video>` would survive as an inert (or fallback-rendering) shell.
    Removing it here keeps "a video from a foreign host is stripped" literally
    true. This pass only ever *removes* nodes; the nh3 pass afterwards is still
    the security boundary. It runs only when the body mentions `<video`, so an
    ordinary body is not paid for.
    """
    if "<video" not in html.lower():
        return html
    # unreachable-ish: lxml's HTML parser repairs rather than rejects, so the
    # frame always parses. Kept because returning the input unchanged is the
    # safe direction a future parser change would need.
    try:  # pragma: no cover
        wrapper = lxml_html.fromstring(f"<div>{html}</div>")
    except Exception:  # pragma: no cover
        return html
    removed = False
    for element in list(wrapper.iter("video")):
        src = element.get("src")
        if src is None or not video_url_allowed(src):
            # `drop_tree` merges the element's tail into the previous sibling,
            # which plain `remove()` does not; it is typed on HtmlElement, not
            # on the `_Element` `iter()` yields.
            cast(lxml_html.HtmlElement, element).drop_tree()
            removed = True
    if not removed:
        return html
    serialized = str(lxml_html.tostring(wrapper, encoding="unicode"))
    frame = len("<div>")
    return serialized[frame : -len("</div>")]


# Content of these is dropped along with the tag, rather than being unwrapped
# and kept as text — otherwise a script's source would survive as visible text.
DROPPED_TAGS = frozenset({"script", "style", "iframe", "object", "embed", "noscript"})

# javascript:, data: and vbscript: URLs are the classic execution vectors.
ALLOWED_URL_SCHEMES = frozenset({"http", "https", "mailto"})

# A body is already bounded at fetch time; this is a backstop for a stored row.
MAX_INPUT_CHARS = 2_000_000


def sanitize_html(html: str) -> str:
    """Strip anything that could execute, track, or escape its container.

    Links gain rel="noopener noreferrer" (nh3's default) so a remote page
    cannot reach back into the reader's window.
    """
    if not html:
        return ""
    if len(html) > MAX_INPUT_CHARS:
        LOGGER.warning("refusing to sanitise a %d character body", len(html))
        return ""

    html = _drop_foreign_video(html)
    return nh3.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        clean_content_tags=DROPPED_TAGS,
        url_schemes=ALLOWED_URL_SCHEMES,
        # CSS can carry url() tracking and behaviour we have no way to audit,
        # so no style declarations are allowed through at all.
        filter_style_properties=frozenset(),
        attribute_filter=_attribute_filter,
        strip_comments=True,
    )
