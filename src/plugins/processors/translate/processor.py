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
is `parse`, an unconfigured provider is `parse` (a configuration gap, not
a session failure).

Prompt-injection trade-off, recorded: item content is untrusted and goes
into the user message as-is. The injection surface is bounded by the
system prompt's narrow task (translate only, no instructions followed)
and by the output being stored as a derived artifact, never executed —
the same trust decision as ADR 0007's store-time sanitising, applied to
a different boundary.
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
# Optional comma-separated fallback chain, tried in order when a model hits
# a rate limit or a server error: PEARSS_TRANSLATE_MODELS="m1,m2,m3".
MODELS_ENV: Final = "PEARSS_TRANSLATE_MODELS"
MAX_CHARS: Final = 24_000

_LANG_NAMES: Final[dict[str, str]] = {
    "en": "English",
    "zh": "Simplified Chinese",
    "ja": "Japanese",
}


_CONFIG_CACHE: tuple[str, str, list[str]] | None = None


def _reset_config_cache() -> None:
    """Forget the cached provider config (tests; env never changes in-process)."""
    global _CONFIG_CACHE
    _CONFIG_CACHE = None


def _configured() -> tuple[str, str, list[str]]:
    """Provider config, cached per-process.

    The environment is stable for the worker's lifetime (the sandbox's env
    fingerprint rotation handles changes by respawning); re-reading os.environ
    per item was a fixed per-translation cost for a value that never changes.
    """
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE
    base = os.environ.get(BASE_URL_ENV, "").strip().rstrip("/")
    key = os.environ.get(API_KEY_ENV, "").strip()
    models_env = os.environ.get(MODELS_ENV, "").strip()
    if models_env:
        models = [m.strip() for m in models_env.split(",") if m.strip()]
    else:
        single = os.environ.get(MODEL_ENV, "").strip()
        models = [single] if single else []
    if not (base and key and models):
        # Configuration gap, not a session failure: it classifies as parse
        # (the "this cannot be processed as things stand" family) rather than
        # auth, so an operator debugging a held item reads "set the env vars",
        # not "rotate the key".
        raise ProcessorError(
            ErrorClass.PARSE,
            "translation provider not configured; set PEARSS_TRANSLATE_BASE_URL, "
            "PEARSS_TRANSLATE_API_KEY and PEARSS_TRANSLATE_MODEL",
        )
    _CONFIG_CACHE = (base, key, models)
    return _CONFIG_CACHE


def _strip_to_text(content_html: str) -> str:
    """The model translates prose, not markup: tags out, entities decoded."""
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", content_html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return html.unescape(re.sub(r"\s+", " ", text)).strip()


# Unicode script ranges, enough to answer "is this already the target
# language?" without a detector dependency. Characters outside these ranges
# (punctuation, digits, emoji, Cyrillic, Arabic) count as "other" and never
# decide the outcome on their own.
#
# The CJK ideograph block is shared: the same characters are ordinary Chinese
# and ordinary Japanese. Script ranges alone therefore cannot separate the two,
# and classifying a kanji-heavy Japanese sentence (26 kanji, 2 kana) by first
# match tagged it 93% Chinese -- skipping text the reader cannot read.
#
# The workable rule is that **kana is the deciding evidence**. Japanese prose
# almost always carries kana, so when any kana is present the ideographs are
# attributed to `ja`; with no kana the text is Chinese. A Japanese headline
# written entirely in kanji is indistinguishable from Chinese at this size by
# any statistical method, and is treated as Chinese -- the cost of that rare
# miss is one unnecessary translation, which is the safe direction.
_KANA_RANGES: Final[tuple[tuple[int, int], ...]] = (
    (0x3040, 0x309F),  # hiragana
    (0x30A0, 0x30FF),  # katakana
)
_CJK_RANGES: Final[tuple[tuple[int, int], ...]] = (
    (0x3400, 0x4DBF),  # extension A
    (0x4E00, 0x9FFF),  # unified ideographs
    (0xF900, 0xFAFF),  # compatibility ideographs
)
_LATIN_RANGES: Final[tuple[tuple[int, int], ...]] = (
    (0x0041, 0x005A),
    (0x0061, 0x007A),
)

# The languages this can tell apart. A target outside this set gets no shortcut
# rather than a wrong one.
_KNOWN_TARGETS: Final[frozenset[str]] = frozenset({"zh", "ja", "en"})

# A body counts as already-target when most of its scripted characters belong
# to that language. 0.5 is deliberately the midpoint: the sampler found real X
# posts like 「如果真是"自我提升向内求"，respect。」, majority-Chinese with a
# Latin tail, and translating those is exactly the waste this avoids. A
# genuinely bilingual post still translates, because there the target script is
# not the majority.
_TARGET_SCRIPT_SHARE: Final = 0.5

# Below this many scripted characters a ratio is noise: "OK", "2026" and
# one-word titles would all be judged by one or two glyphs. Short bodies go to
# the provider, which is the safe direction -- an unnecessary call costs a
# fraction of a cent, a skipped one leaves untranslated text.
_MIN_SCRIPTED_CHARS: Final = 12


def _in_ranges(codepoint: int, ranges: tuple[tuple[int, int], ...]) -> bool:
    return any(low <= codepoint <= high for low, high in ranges)


def _is_already_in(text: str, target_lang: str) -> bool:
    """Whether `text` is predominantly written in `target_lang`.

    Deliberately not a language *detector*: it answers one yes/no question for
    the languages this deployment translates between, and returns False when
    unsure. Every uncertain answer costs a provider call rather than leaving
    the reader with text they cannot read.
    """
    if target_lang not in _KNOWN_TARGETS:
        # An unknown target gets no shortcut: without knowing what its script
        # looks like, any judgement here would be a guess.
        return False

    # Kana decides how the shared ideograph block is attributed, so it is
    # resolved once for the whole body rather than per character.
    has_kana = any(_in_ranges(ord(char), _KANA_RANGES) for char in text)

    in_target = 0
    scripted = 0
    for char in text:
        codepoint = ord(char)
        if _in_ranges(codepoint, _LATIN_RANGES):
            language = "en"
        elif _in_ranges(codepoint, _KANA_RANGES):
            language = "ja"
        elif _in_ranges(codepoint, _CJK_RANGES):
            language = "ja" if has_kana else "zh"
        else:
            continue
        scripted += 1
        if language == target_lang:
            in_target += 1

    if scripted < _MIN_SCRIPTED_CHARS:
        return False
    return in_target / scripted >= _TARGET_SCRIPT_SHARE


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

    Content already written in `target_lang` is not sent anywhere. It produces
    an empty artifact — the same shape as an item with no text — which is a
    SUCCESS, so the gate opens and the item publishes with its original body.
    That is the correct outcome for a post that needs no translation, and it is
    why the check runs before `_configured()`: a deployment whose feed is
    entirely Chinese needs no provider configured to serve it.
    """
    source_text = _strip_to_text(item_ctx.get("content", ""))
    if not source_text:
        # Title-only items (common in announcement feeds) still have the
        # title to translate; a genuinely empty item produces an empty
        # artifact as a SUCCESS — failing here would deadlock the gate on
        # content shapes the chain can never act on.
        title_text = _strip_to_text(item_ctx.get("title", ""))
        if not title_text:
            return str(store(""))
        source_text = title_text

    if _is_already_in(source_text, target_lang):
        LOGGER.info(
            "item %s: already in %s, skipping translation",
            item_ctx.get("item_id", "?"),
            target_lang,
        )
        return str(store(""))

    base, key, models = _configured()
    chunks = _segment(source_text)
    if len(chunks) > 1:
        LOGGER.info(
            "item %s: %d chars split into %d segments for translation",
            item_ctx.get("item_id", "?"),
            len(source_text),
            len(chunks),
        )
    language = _LANG_NAMES.get(target_lang, target_lang)

    translated: list[str] = []
    for chunk in chunks:
        translated.append(
            _translate_one(chunk, language=language, base=base, key=key, models=models)
        )
    paragraphs = "".join(
        f"<p>{html.escape(p)}</p>"
        for segment in translated
        for p in segment.splitlines()
        if p.strip()
    )
    ref: str = store(paragraphs)
    return ref


def _segment(text_to_split: str) -> list[str]:
    """Split into chunks no larger than MAX_CHARS, preferring paragraph
    boundaries. No truncation: every character survives (ADR 0018 review)."""
    if len(text_to_split) <= MAX_CHARS:
        return [text_to_split]
    chunks: list[str] = []
    current = ""
    for paragraph in text_to_split.split("\n\n"):
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) <= MAX_CHARS:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        while len(paragraph) > MAX_CHARS:
            cut = paragraph.rfind(". ", 0, MAX_CHARS)
            cut = cut + 1 if cut > 0 else MAX_CHARS
            chunks.append(paragraph[:cut].rstrip())
            paragraph = paragraph[cut:].lstrip()
        current = paragraph
    if current:
        chunks.append(current)
    return chunks


def _translate_one(
    source_text: str,
    *,
    language: str,
    base: str,
    key: str,
    models: list[str],
) -> str:
    """One chat completion through the model fallback chain."""
    for index, model in enumerate(models):
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
                # LLM completions through a relayed endpoint regularly take
                # minutes: the read bound is what bounds a slow generation,
                # and cutting it at 120s killed real requests (observed: a
                # gemini-flash reply took 126s end to end).
                timeout=httpx.Timeout(connect=10.0, read=300.0, write=15.0, pool=360.0),
            )
        except httpx.HTTPError as exc:
            if index + 1 < len(models):
                LOGGER.warning(
                    "model %s unreachable (%r); falling back to %s",
                    model,
                    exc,
                    models[index + 1],
                )
                continue
            raise ProcessorError(
                ErrorClass.NETWORK, f"translation provider unreachable: {exc!r}"
            ) from exc

        if response.status_code == 401:
            raise ProcessorError(ErrorClass.AUTH, "provider rejected the API key")
        if response.status_code in (429, 500, 502, 503, 504):
            # Rate limit / server error: the fallback chain exists exactly
            # for this. The last model re-raises as blocked/network.
            if index + 1 < len(models):
                LOGGER.warning(
                    "model %s returned %d; falling back to %s",
                    model,
                    response.status_code,
                    models[index + 1],
                )
                continue
            raise ProcessorError(
                ErrorClass.BLOCKED
                if response.status_code == 429
                else ErrorClass.NETWORK,
                f"all models exhausted; last HTTP {response.status_code}",
            )
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

        return reply
    raise ProcessorError(
        ErrorClass.NETWORK, "model chain exhausted"
    )  # pragma: no cover
