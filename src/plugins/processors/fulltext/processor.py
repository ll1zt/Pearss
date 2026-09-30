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
  heuristics, no per-site rules to maintain. It lives in `adapters/article.py`,
  shared with the address-based content fetch (#167), so this processor and
  that path cannot drift apart.
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
from typing import Any, Final

import httpx

from adapters.article import (
    ARTICLE_FETCH_POLICY,
    MIN_ARTICLE_CHARS,
    extract_article_markup,
    plain_length,
)
from adapters.http import fetch
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

# The article-page fetch bounds (timeouts, size cap) live with the extractor,
# in `adapters/article.py`: the rss collector's address path fetches the same
# kind of page, and two copies of "how big is an article" would drift.


@HOOKIMPL
def processor_id() -> str:
    """The stable registration key."""
    return "fulltext"


@HOOKIMPL
def processor_version() -> str:
    """Bumped when the extractor changes: products are re-derived."""
    return "0.1.0"


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
    current = plain_length(item_ctx.get("content", ""))

    if current >= MIN_PLAIN_CHARS:
        # Already reads like an article: no fetch, empty product, gate opens.
        return str(store(""))

    if not url.startswith(("http://", "https://")):
        # Same family as a title-only item: nothing this chain can act on,
        # and inventing a request target would be worse than failing.
        return str(store(""))

    html_text = _fetch_article(url, item_id=str(item_ctx.get("item_id", "?")))
    markup = extract_article_markup(html_text, base_url=url)
    if plain_length(markup) < MIN_ARTICLE_CHARS:
        raise ProcessorError(
            ErrorClass.PARSE,
            f"no extractable article at the item URL ({plain_length(markup)} chars)",
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
    policy = ARTICLE_FETCH_POLICY
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
