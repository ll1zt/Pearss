"""The embedding processor: one item's text, stored as one vector.

Opt-in per source like every other processor, via
`pipeline_chain: [{"plugin_id": "embed", "kind": "embedding"}]`. Joining the
existing chain buys the retry policy, the gate, the backfill and the tool surface
without a second mechanism for any of them.

The artifact is **text**, because that is what the child protocol carries: 384
float32 values are 1536 bytes and 2048 base64 characters, which is the compact
encoding that keeps a vector a product like any other -- a `cas://` reference to
a body. JSON numbers would roughly triple it, and a file written by the processor
itself would need a second storage path for the same payload.

The model identity this artifact is keyed by was fixed when the row was enqueued
(`parse_chain`), from the same manifest this reads: a row cannot be keyed by one
model and filled by another.
"""

from __future__ import annotations

import base64
import logging
import struct
from typing import Any, cast

from lxml import etree as lxml_etree
from lxml import html as lxml_html

from adapters.embedding_model import model_directory
from core.plugins.hooks import HOOKIMPL

LOGGER = logging.getLogger(__name__)

# One per child process. The protocol runs each processor in a fresh process, so
# this is "load the weights once per run" rather than a cache that outlives one.
_ENCODERS: dict[str, Any] = {}


@HOOKIMPL
def processor_id() -> str:
    """The stable registration key."""
    return "embed"


@HOOKIMPL
def processor_version() -> str:
    """Bumped when the encoding changes: products are re-derived."""
    return "0.1.0"


@HOOKIMPL
def process(item_ctx: Any, *, target_lang: str, store: Any) -> str:
    """Embed the item's text; store the vector as base64 text."""
    del target_lang  # one model covers every language; there is nothing to pick
    vector = _encoder().encode(_passage_text(item_ctx))
    packed = struct.pack(f"<{len(vector)}f", *vector)
    return str(store(base64.b64encode(packed).decode("ascii")))


def _encoder() -> Any:
    """This deployment's encoder, loaded once per process."""
    from plugins.processors.embed.encoder import Encoder

    directory = model_directory()
    key = str(directory)
    encoder = _ENCODERS.get(key)
    if encoder is None:
        encoder = Encoder(directory)
        LOGGER.info("embedding with model %s", encoder.identity)
        _ENCODERS[key] = encoder
    return encoder


def _passage_text(item_ctx: Any) -> str:
    """What gets embedded: the title, then the readable body."""
    title = str(item_ctx.get("title") or "")
    body = _visible_text(str(item_ctx.get("content") or ""))
    return f"{title}\n{body}".strip()


def _visible_text(content: str) -> str:
    """The readable text of an HTML body, with the markup gone.

    Markup would spend the model's 512-token window on tags it was never trained
    on. Whitespace is collapsed rather than preserved because the tokenizer
    treats a run of it the same way: the two differ only in how much of the
    window the whitespace wasted.
    """
    if not content:
        return ""
    try:
        # lxml's stubs type `fromstring` as a generic element even for the HTML
        # parser, which is what actually returns the HtmlElement with
        # `text_content`; the cast is the stub's gap, not a runtime unknown.
        node = cast("lxml_html.HtmlElement", lxml_html.fromstring(content))
        return " ".join(node.text_content().split())
    except (ValueError, lxml_etree.ParserError):
        return " ".join(content.split())
