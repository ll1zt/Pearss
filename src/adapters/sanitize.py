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

import nh3

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
        "wbr",
    }
)

# Global presentation attributes only. Event handlers (on*), `style`, `srcdoc`,
# `formaction` and every `data-*` are absent, so they are dropped.
ALLOWED_ATTRIBUTES = {
    "a": frozenset({"href", "title"}),
    "img": frozenset({"src", "alt", "title", "width", "height", "loading"}),
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

    return nh3.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        clean_content_tags=DROPPED_TAGS,
        url_schemes=ALLOWED_URL_SCHEMES,
        # CSS can carry url() tracking and behaviour we have no way to audit,
        # so no style declarations are allowed through at all.
        filter_style_properties=frozenset(),
        strip_comments=True,
    )
