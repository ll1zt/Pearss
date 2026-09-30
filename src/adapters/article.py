"""The article extractor: one implementation of "page HTML -> the article".

Two callers need the same step. The fulltext processor fetches a summary item's
own page and fills the body from it; the address-based content fetch (issue
#170) is handed article addresses for an existing source and fetches them the
same way. The logic used to live inside
`plugins/processors/fulltext/processor.py`, where a second caller would have had
to copy it -- and two density-scored extractors agree only on the day they are
written (issue #167).

Scope: **pure functions over HTML.** Fetching stays with the caller, which owns
the timeout/size/SSRF policy (`adapters.http`) and its own retry semantics, so
this module has no network code and does not import `httpx`.

Failures are classified: a document that cannot be reduced to an article raises
`ProcessorError(ErrorClass.PARSE)`. That is the processor contract's error type,
which this module inherited along with the code; it carries `error_class`, and
every plugin boundary in this repository translates such errors by duck-typing
that attribute (`collector_registry.parse_with` does exactly this for each
collector's own error type).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final, cast
from urllib.parse import urljoin

from lxml import html as lxml_html

from adapters.http import FetchPolicy
from core.enums import ErrorClass
from core.plugins.hooks import ProcessorError

# An extracted article below this is a detector miss (a paywall stub, a JS
# shell), not an article — report it as parse instead of storing chrome. Shared
# by both callers so "too short to be the article" means one thing.
MIN_ARTICLE_CHARS: Final = 120

# How an article page is fetched. Bounded, and generous enough for a long post:
# the extractor is only as good as the bytes it is handed, and both callers
# (the fulltext processor, the rss collector's address path) want the same
# bounds, so the numbers live with the extractor rather than twice beside it.
# Tighter than the feed defaults because this is one page per item, often
# inside an enrichment lease or a caller's batch.
ARTICLE_FETCH_POLICY: Final = FetchPolicy(
    connect_timeout=10.0,
    read_timeout=20.0,
    total_timeout=45.0,
    max_bytes=2_500_000,
)

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


def extract_article_markup(html_text: str, base_url: str) -> str:
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
        (element, plain_length(_serialize(element)), _depth(element))
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


def plain_length(content: str | None) -> int:
    """The visible-text length of a markup fragment (or plain string)."""
    if not content:
        return 0
    return len(_visible_text(content))


# --------------------------------------------------------------------------
# Page metadata: what an address-only fetch needs to build a draft
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class PageMetadata:
    """What a page says about itself, for a caller that only has an address.

    Every field is optional on purpose: a page that declares nothing is still
    a usable article (the caller has the address it asked for), so a missing
    field is None rather than a failure.
    """

    title: str | None
    canonical_url: str | None
    published_at: datetime | None


def page_metadata(html_text: str) -> PageMetadata:
    """Title, canonical URL and publication time, as the page declares them.

    `og:title` wins over `<title>` (which usually carries the site name too),
    `rel=canonical` wins over `og:url`, and the time comes from
    `article:published_time` then from the first `<time datetime=...>`.

    The time is returned exactly as the page expressed it: an offset makes it
    aware, no offset leaves it naive. Guessing a zone is how a timestamp
    silently shifts, which is the rule `parse` follows for feed dates too.
    """
    try:
        document = lxml_html.fromstring(html_text)
    except Exception:
        return PageMetadata(title=None, canonical_url=None, published_at=None)

    return PageMetadata(
        title=_meta_title(document),
        canonical_url=_meta_canonical(document),
        published_at=_meta_published(document),
    )


def _meta_content(document: lxml_html.HtmlElement, *, name: str) -> str | None:
    """The content of the first `<meta>` naming `name` (property or name)."""
    for element in document.iter("meta"):
        if (element.get("property") or element.get("name")) != name:
            continue
        content = (element.get("content") or "").strip()
        if content:
            return content
    return None


def _meta_title(document: lxml_html.HtmlElement) -> str | None:
    open_graph = _meta_content(document, name="og:title")
    if open_graph is not None:
        return open_graph
    title = document.findtext(".//title")
    return " ".join(title.split()) if title else None


def _meta_canonical(document: lxml_html.HtmlElement) -> str | None:
    for element in document.iter("link"):
        if (element.get("rel") or "").lower() != "canonical":
            continue
        href = (element.get("href") or "").strip()
        if href:
            return href
    return _meta_content(document, name="og:url")


def _meta_published(document: lxml_html.HtmlElement) -> datetime | None:
    raw = _meta_content(document, name="article:published_time")
    if raw is None:
        for element in document.iter("time"):
            raw = (element.get("datetime") or "").strip() or None
            if raw is not None:
                break
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        # A page whose date is unparseable is still a usable article; the
        # caller records no time rather than failing the fetch.
        return None
