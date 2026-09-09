"""The OpenAI-compatible translation processor.

Reference implementation for the P3 processor chain (ADR 0018): takes a
stored item's HTML, asks a chat-completions endpoint for a translation, and
stores the result as a `translation` artifact. Everything transport — the
endpoint, the model, the API key — arrives from the environment at spawn
time, exactly as credentials do for collectors:

    PEARSS_TRANSLATE_BASE_URL   e.g. https://api.openai.com/v1
    PEARSS_TRANSLATE_API_KEY    bearer token
    PEARSS_TRANSLATE_MODEL      model id

The processor never touches the database; the worker owns the row's
lifecycle. Failures are ProcessorError values with the standard taxonomy:
a 401 from the provider is `auth`, a 429 is `blocked`, a malformed reply
is `parse`.
"""

from __future__ import annotations

import html
import logging
import os
import re
from typing import Any, Final

import httpx

from core.enums import ErrorClass
from core.plugins.hooks import HOOKIMPL, ProcessorError

LOGGER = logging.getLogger(__name__)

BASE_URL_ENV: Final = "PEARSS_TRANSLATE_BASE_URL"
API_KEY_ENV: Final = "PEARSS_TRANSLATE_API_KEY"
MODEL_ENV: Final = "PEARSS_TRANSLATE_MODEL"
MAX_CHARS: Final = 24_000

_LANG_NAMES: Final[dict[str, str]] = {
    "en": "English",
    "zh": "Simplified Chinese",
    "ja": "Japanese",
}


def _configured() -> tuple[str, str, str]:
    base = os.environ.get(BASE_URL_ENV, "").strip().rstrip("/")
    key = os.environ.get(API_KEY_ENV, "").strip()
    model = os.environ.get(MODEL_ENV, "").strip()
    if not (base and key and model):
        raise ProcessorError(
            ErrorClass.AUTH,
            "translation provider not configured; set PEARSS_TRANSLATE_BASE_URL, "
            "PEARSS_TRANSLATE_API_KEY and PEARSS_TRANSLATE_MODEL",
        )
    return base, key, model


def _strip_to_text(content_html: str) -> str:
    """The model translates prose, not markup: tags out, entities decoded."""
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", content_html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return html.unescape(re.sub(r"\s+", " ", text)).strip()


@HOOKIMPL
def processor_id() -> str:
    """The stable registration key."""
    return "translate"


@HOOKIMPL
def processor_version() -> str:
    """Bumped when the prompt or parsing changes: products are re-derived."""
    return "0.1.0"


def _parse_reply(payload: dict[str, Any]) -> str:
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ProcessorError(
            ErrorClass.PARSE,
            f"unrecognised chat-completion reply: {type(exc).__name__}",
        ) from exc
    if not isinstance(content, str) or not content.strip():
        raise ProcessorError(ErrorClass.PARSE, "empty completion content")
    return str(content)


@HOOKIMPL
def process(
    item_ctx: Any,
    *,
    target_lang: str,
    store: Any,
) -> str:
    """Translate the item's text into `target_lang`; return the storage ref.

    The item body keeps its HTML; the translation carries the same prose in
    simple paragraphs, so the reader can render both faithfully.
    """
    base, key, model = _configured()
    source_text = _strip_to_text(item_ctx.get("content", ""))
    if not source_text:
        raise ProcessorError(ErrorClass.PARSE, "item carries no text to translate")
    source_text = source_text[:MAX_CHARS]
    language = _LANG_NAMES.get(target_lang, target_lang)

    body = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    f"Translate the user's text into {language}. Reply with "
                    "the translation only: no preamble, no notes, keep the "
                    "original paragraph breaks."
                ),
            },
            {"role": "user", "content": source_text},
        ],
        "temperature": 0,
    }
    try:
        response = httpx.post(
            f"{base}/chat/completions",
            headers={"authorization": f"Bearer {key}"},
            json=body,
            timeout=httpx.Timeout(connect=10.0, read=120.0, write=15.0, pool=150.0),
        )
    except httpx.HTTPError as exc:
        raise ProcessorError(
            ErrorClass.NETWORK, f"translation provider unreachable: {exc!r}"
        ) from exc

    if response.status_code == 401:
        raise ProcessorError(ErrorClass.AUTH, "provider rejected the API key")
    if response.status_code == 429:
        raise ProcessorError(ErrorClass.BLOCKED, "provider rate limit hit")
    if response.status_code != 200:
        raise ProcessorError(
            ErrorClass.NETWORK, f"provider returned HTTP {response.status_code}"
        )
    try:
        reply = _parse_reply(response.json())
    except ValueError as exc:
        raise ProcessorError(
            ErrorClass.PARSE, f"provider reply was not JSON: {exc}"
        ) from exc

    paragraphs = "".join(
        f"<p>{html.escape(p)}</p>" for p in reply.splitlines() if p.strip()
    )
    ref: str = store(paragraphs)
    return ref
