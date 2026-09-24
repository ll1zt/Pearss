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
from urllib.parse import urljoin

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


def process(
    item_ctx: Any,
    *,
    target_lang: str,
    store: Any,
) -> str:
    """Fill in the item's body from its article page; return the storage ref.

    The artifact is the article's own markup (headings, images, code, lists,
    quotes, tables, links), sanitised here before `store` — the reader gets
    the article as the author laid it out, not a flattened transcript.
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
    markup = _extract_article_markup(html_text, base_url=url)
    if _plain_length(markup) < MIN_ARTICLE_CHARS:
        raise ProcessorError(
            ErrorClass.PARSE,
            f"no extractable article at the item URL ({_plain_length(markup)} chars)",
        )
    return str(store(sanitize_html(markup)))


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


def _extract_article_markup(html_text: str, base_url: str) -> str:
    """The readable article as MARKUP: chrome dropped, structure kept.

    Three steps: parse; remove every non-content subtree; among the remaining
    container-level blocks keep the one with the most visible text (the
    article is always the densest block on a blog page). The winner's
    children are returned as HTML — headings, images, code, lists, quotes,
    tables and links intact — with relative URLs resolved against `base_url`,
    because the product is rendered somewhere else entirely.

    Returning plain text was the original mistake: it flattened every block
    into a `<p>`, so a fetched article lost its headings, images, code
    formatting, lists, quotes, tables and links, and nested blocks printed
    twice (#32).
    """
    document = _parse_document(html_text)

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
        (element, _plain_length(_serialize(element)), _depth(element))
        for element in candidates
    ]
    top = max(score for _, score, _ in scored)
    if top == 0:
        return ""
    best = max(
        (entry for entry in scored if entry[1] >= int(top * 0.9)),
        key=lambda entry: entry[2],
    )
    article = best[0]
    # The article is rendered elsewhere (a reader, a phone), so a relative
    # href/src is a broken link there.
    for element in article.iter():
        if element.tag == "a" and element.get("href"):
            element.set("href", urljoin(base_url, element.get("href")))
        elif element.tag == "img" and element.get("src"):
            element.set("src", urljoin(base_url, element.get("src")))
    # Children only: the container's own tag (article/main/section) is
    # packaging, and the sanitiser's allowlist is about content.
    return "".join(
        lxml_html.tostring(child, encoding="unicode")
        for child in article.iterchildren()
        if isinstance(child.tag, str)
    )


def _depth(element: lxml_html.HtmlElement) -> int:
    """How deep the element sits — the tiebreaker among near-equal blocks."""
    return sum(1 for _ in element.iterancestors())


def _parse_document(html_text: str) -> lxml_html.HtmlElement:
    """Parse a page into a tree, or fail with the processor's taxonomy."""
    try:
        return lxml_html.fromstring(html_text)
    except ValueError:
        # lxml refuses an XML declaration with a stray encoding hint; the
        # content is the same document without it.
        head, _, tail = html_text.partition("?>")
        if not tail:
            head, _, tail = html_text.partition(">")
            head = head + ">" if head.startswith("<") else head
        try:
            return lxml_html.fromstring(tail if tail.strip() else head)
        except Exception as exc:
            raise ProcessorError(
                ErrorClass.PARSE, f"article page is not parseable HTML: {exc}"
            ) from exc
    except Exception as exc:
        raise ProcessorError(
            ErrorClass.PARSE, f"article page is not parseable HTML: {exc}"
        ) from exc


def _serialize(element: lxml_html.HtmlElement) -> str:
    """The element's subtree as HTML (used for measuring and for the product)."""
    return cast("str", lxml_html.tostring(element, encoding="unicode"))


def _visible_text(markup: str) -> str:
    """The text a reader sees in a markup fragment, whitespace collapsed.

    Tag-wise on purpose: this is the density/threshold measurement, and
    markup weight must not make a short description look like an article.
    """
    try:
        tree = lxml_html.fromstring(markup)
    except Exception:
        return " ".join(markup.split())
    # The stubs type fromstring() as _Element, which lacks text_content.
    content = cast("lxml_html.HtmlElement", tree).text_content() or ""
    return " ".join(content.split())


def _plain_length(content: str | None) -> int:
    """The visible-text length of a markup fragment (or plain string)."""
    if not content:
        return 0
    return len(_visible_text(content))
