"""The fulltext processor: summary-only feeds get the article body anyway.

Companion to the RSS collector's WordPress path (`plugins/collectors/rss/
wordpress.py`): that one upgrades feeds whose WordPress install leaks full
text over REST. This processor covers every other summary feed — Hugo, Halo,
Astro, Quartz and hand-rolled sites all publish 0-250 character descriptions
by choice — by fetching the item's own article page and extracting the
readable body.

Opt-in per source, never a default: a source joins via
`pipeline_chain: [{"plugin_id": "fulltext", "kind": "fulltext"}]`, the same
way translations are enabled. A source without the chain behaves exactly as
before, and the operator's per-source decision is what bounds how many
article pages get fetched.

Behavior, mirroring the WordPress path's fallback semantics:

- An item whose visible text already meets the threshold is left untouched —
  an empty artifact, a SUCCESS — so the gate opens without a redundant fetch.
  The threshold is plain-text length, so markup weight cannot fake a pass.
- A short item triggers one article-page fetch under `adapters.http`'s policy
  (timeouts, size cap, SSRF guard). The extracted text passes ADR 0007's
  store-time sanitiser before it is returned as the artifact.
- The extractor is structural (lxml over HTML): drop non-content containers,
  score candidate blocks by text density, keep the winner. No network
  heuristics, no per-site rules to maintain.
- A 200 page with no extractable article is `parse`, not success — a fake
  product would hold the gate open on nothing. Unreachable pages are
  `network`; guard refusals are `blocked`; all retried or failed by the
  worker's existing semantics with the summary never lost.

Trust boundary: an article page is as untrusted as a feed body. The artifact
is sanitised here, in-process, before it is stored — the same boundary the
pipeline applies to feed content — so every reader serves the product without
remembering to sanitise again. Title-only items produce an empty artifact
(the translate processor's precedent): failing them would deadlock the gate
on content shapes the chain can never act on.

Runs in the enrichment child like any processor; egress shares the sandbox's
proxy and the host's SSRF guard (ADR 0016, 0027).
"""

from __future__ import annotations

import logging
from typing import Any, Final, cast

import httpx
from lxml import html as lxml_html

from adapters.http import FetchPolicy, fetch
from adapters.sanitize import sanitize_html
from core.enums import ErrorClass
from core.plugins.hooks import HOOKIMPL, ProcessorError

LOGGER = logging.getLogger(__name__)

# Plain-text floor and ceiling, in visible characters (markup excluded).
# Below the floor a feed description is a summary in the sense that matters
# (NNW shows a stub); items at or above it already read like an article.
# 200 is deliberately low: the cost of a spurious skip (reader keeps a stub)
# is smaller than the cost of a spurious fetch (a request to someone's blog).
MIN_PLAIN_CHARS: Final = 200

# An extracted article below this is a detector miss (a paywall stub, a JS
# shell), not an article — report it as parse instead of storing chrome.
MIN_ARTICLE_CHARS: Final = 120

# Articles are big; feeds are not. Half the host's feed cap is already
# generous for a single page.
MAX_ARTICLE_BYTES: Final = 2_500_000

# Timeouts tighter than the collector defaults: this fetch happens inside the
# enrichment lease, one per item, and a slow origin should not hold a worker
# slot for the feed-sized total budget.
CONNECT_TIMEOUT: Final = 10.0
READ_TIMEOUT: Final = 20.0
TOTAL_TIMEOUT: Final = 45.0


@HOOKIMPL
def processor_id() -> str:
    """The stable registration key."""
    return "fulltext"


@HOOKIMPL
def processor_version() -> str:
    """Bumped when the extractor changes: products are re-derived."""
    return "0.1.0"


# Containers whose whole subtree is never article content. Dropped before any
# scoring, so a long navigation list cannot win by density alone.
_NON_CONTENT: Final[frozenset[str]] = frozenset(
    {
        "script",
        "style",
        "noscript",
        "template",
        "nav",
        "aside",
        "header",
        "footer",
        "form",
        "button",
        "svg",
        "iframe",
    }
)

# Tags whose text becomes a paragraph in the product; everything else in the
# winning subtree is unwrapped to inline text.
_PARAGRAPH_TAGS: Final[frozenset[str]] = frozenset(
    {
        "p",
        "li",
        "blockquote",
        "pre",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "figcaption",
        "dd",
        "dt",
    }
)


def process(
    item_ctx: Any,
    *,
    target_lang: str,
    store: Any,
) -> str:
    """Fill in the item's body from its article page; return the storage ref.

    The artifact is a bare sequence of block-level fragments ("<p>…", the
    translate processor's product shape). Sanitised here, before `store`.
    """
    url = (item_ctx.get("url") or "").strip()
    current = _plain_length(item_ctx.get("content", ""))

    if current >= MIN_PLAIN_CHARS:
        # Already reads like an article: no fetch, empty product, gate opens.
        return str(store(""))

    if not url.startswith(("http://", "https://")):
        # Same family as a title-only item: nothing this chain can act on,
        # and inventing a request target would be worse than failing.
        return str(store(""))

    html_text = _fetch_article(url, item_id=str(item_ctx.get("item_id", "?")))
    text = _extract_article_text(html_text)
    if _plain_length(text) < MIN_ARTICLE_CHARS:
        raise ProcessorError(
            ErrorClass.PARSE,
            f"no extractable article at the item URL ({_plain_length(text)} chars)",
        )
    fragments = _fragment(text)
    product = sanitize_html("".join(fragments))
    return str(store(product))


# --------------------------------------------------------------------------
# Fetch
# --------------------------------------------------------------------------


def _fetch_article(url: str, *, item_id: str) -> str:
    """One bounded GET of the article page; FetchError maps to ProcessorError.

    `adapters.http` owns the timeouts, the size cap and the SSRF guard; this
    wrapper only translates the failure into the processor contract so the
    worker's retry semantics see one exception type.
    """
    policy = FetchPolicy(
        connect_timeout=CONNECT_TIMEOUT,
        read_timeout=READ_TIMEOUT,
        total_timeout=TOTAL_TIMEOUT,
        max_bytes=MAX_ARTICLE_BYTES,
    )
    try:
        with httpx.Client(follow_redirects=True) as client:
            payload = fetch(url, client=client, policy=policy)
    except Exception as exc:
        error_class = getattr(exc, "error_class", None)
        if isinstance(error_class, ErrorClass):
            raise ProcessorError(
                error_class, f"article page unreachable: {exc}"
            ) from exc
        raise ProcessorError(
            ErrorClass.NETWORK, f"article page unreachable: {exc!r}"
        ) from exc
    if not payload.body:
        raise ProcessorError(ErrorClass.NETWORK, "article page returned no body")
    return payload.text()


# --------------------------------------------------------------------------
# Extraction (pure; pinned by the unit tests)
# --------------------------------------------------------------------------


def _extract_article_text(html_text: str) -> str:
    """The readable text of an HTML page: chrome dropped, article kept.

    Structural, in three steps: parse; remove every non-content subtree;
    among the remaining container-level blocks keep the one with the most
    visible text (the article is always the densest block on a blog page).
    Returns plain text with paragraphs separated by newlines.
    """
    try:
        document = lxml_html.fromstring(html_text)
    except ValueError:
        # lxml refuses an XML declaration with a stray encoding hint; the
        # content is the same document without it.
        head, _, tail = html_text.partition("?>")
        if not tail:
            head, _, tail = html_text.partition(">")
            head = head + ">" if head.startswith("<") else head
        try:
            document = lxml_html.fromstring(tail if tail.strip() else head)
        except Exception as exc:
            raise ProcessorError(
                ErrorClass.PARSE, f"article page is not parseable HTML: {exc}"
            ) from exc
    except Exception as exc:
        raise ProcessorError(
            ErrorClass.PARSE, f"article page is not parseable HTML: {exc}"
        ) from exc

    for element in document.iter():
        if element.tag in _NON_CONTENT:
            # The HTML parser yields HtmlElement instances here at runtime;
            # the stubs type iter() as _Element, which lacks drop_tree.
            cast("lxml_html.HtmlElement", element).drop_tree()

    candidates = [
        element
        for element in document.iter()
        # Comment/PI nodes carry a non-string tag and fall out naturally.
        if element.tag in ("article", "main", "div", "section", "td", "body")
    ]
    if not candidates:
        candidates = [document]

    # The article is the text-densest container — always, on a blog page.
    # Chrome has already been dropped, so nav links and sidebars cannot win
    # by density. Among near-equal candidates prefer the deepest: an outer
    # wrapper that merely contains the article (plus little else) scores
    # almost the same, and its paragraphs are the same paragraphs, but a
    # comments section inside it would leak in. 90% keeps the real article
    # over wrappers that add nothing, while still preferring a container
    # that genuinely holds more text.
    scored = [
        (element, _plain_length(_text_of(element)), _depth(element))
        for element in candidates
    ]
    top = max(score for _, score, _ in scored)
    if top == 0:
        return ""
    best = max(
        (entry for entry in scored if entry[1] >= int(top * 0.9)),
        key=lambda entry: entry[2],
    )
    return _text_of(best[0])


def _depth(element: lxml_html.HtmlElement) -> int:
    """How deep the element sits — the tiebreaker among near-equal blocks."""
    return sum(1 for _ in element.iterancestors())


def _text_of(element: lxml_html.HtmlElement) -> str:
    """Visible text of a subtree, one paragraph per block-level child."""
    parts: list[str] = []
    if isinstance(element.tag, str) and element.tag in _PARAGRAPH_TAGS:
        parts.append(_inline_text(element))
    else:
        for child in element.iterdescendants():
            if not isinstance(child.tag, str):
                continue
            if child.tag in _PARAGRAPH_TAGS:
                parts.append(_inline_text(child))
    return "\n\n".join(part for part in parts if part.strip())


def _inline_text(element: lxml_html.HtmlElement) -> str:
    """The element's text with tags unwrapped and whitespace collapsed."""
    return " ".join((element.text_content() or "").split())


def _plain_length(content: str | None) -> int:
    """The visible-text length of an HTML fragment (or plain string)."""
    if not content:
        return 0
    try:
        tree = lxml_html.fromstring(content)
    except Exception:
        return len(content.split())
    return len(_inline_text(tree))


def _fragment(text: str) -> list[str]:
    """Escape and wrap each paragraph; the product shape readers render."""
    from html import escape

    return [
        f"<p>{escape(paragraph)}</p>"
        for paragraph in text.split("\n\n")
        if paragraph.strip()
    ]
