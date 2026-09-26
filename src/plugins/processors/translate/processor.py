"""The OpenAI-compatible translation processor.

Reference implementation for the P3 processor chain (ADR 0018): takes a
stored item's HTML, asks a chat-completions endpoint for a translation, and
stores the result as a `translation` artifact. Everything transport — the
endpoint, the model, the API key — arrives from the environment at spawn
time, exactly as credentials do for collectors:

    PEARSS_TRANSLATE_BASE_URL   e.g. https://api.openai.com/v1
    PEARSS_TRANSLATE_API_KEY    bearer token
    PEARSS_TRANSLATE_MODEL      model id

The product keeps the article's structure (ADR 0031): the body is split into
block-level units, only the *text* of a translatable block is sent, and the
translation is refilled into the original tag shell. Images, code blocks and
tables are copied through verbatim rather than translated — a provider asked
to translate `bpftrace -e 'kprobe:do_sys_open'` can only mangle it, and
whitespace inside `<pre>` is code semantics that a prose round-trip destroys.
Flattening the article to paragraphs was what left readers a wall of text with
no headings, diagram, code or links beside an intact original (#33).

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
from urllib.parse import urljoin

import httpx
from lxml import html as lxml_html

from adapters.sanitize import sanitize_html
from core.enums import ErrorClass
from core.language import is_already_in, prose_text
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


@HOOKIMPL
def processor_id() -> str:
    """The stable registration key."""
    return "translate"


@HOOKIMPL
def processor_version() -> str:
    """Bumped when the prompt or parsing changes: products are re-derived."""
    return "0.1.0"


def _provider_reason(response: httpx.Response) -> str:
    """The provider's own words for a refusal, trimmed for a log line.

    OpenAI-compatible errors carry `{"error": {"message": ...}}`, and that
    message is the actionable part: "unknown provider for model X" points
    straight at the misconfigured env var, while the status code alone sends
    the operator looking for a network problem. Never trusted or rendered --
    it lands in an enrichment row's detail for a human to read.
    """
    try:
        payload = response.json()
    except ValueError:
        return "no parseable error body"
    if not isinstance(payload, dict):
        return "no parseable error body"
    error = payload.get("error")
    message = error.get("message") if isinstance(error, dict) else None
    if not isinstance(message, str) or not message.strip():
        return "no parseable error body"
    return " ".join(message.split())[:200]


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

    The product keeps the source's block structure (ADR 0031): translatable
    blocks are sent as text and refilled into their original tag shells, while
    non-text nodes (images, code, tables) are copied through verbatim. The
    reader therefore gets the same article it would have read in the original
    language -- headings to skim, a diagram, a code block, working links --
    rather than a wall of paragraphs.

    Content already written in `target_lang` is not sent anywhere. It produces
    an empty artifact — the same shape as an item with no text — which is a
    SUCCESS, so the gate opens and the item publishes with its original body.
    That is the correct outcome for a post that needs no translation, and it is
    why the check runs before `_configured()`: a deployment whose feed is
    entirely Chinese needs no provider configured to serve it.
    """
    source_text = prose_text(item_ctx.get("content", ""))
    if not source_text:
        # Title-only items (common in announcement feeds) still have the
        # title to translate; a genuinely empty item produces an empty
        # artifact as a SUCCESS — failing here would deadlock the gate on
        # content shapes the chain can never act on.
        title_text = prose_text(item_ctx.get("title", ""))
        if not title_text:
            return str(store(""))
        source_text = title_text

    if is_already_in(source_text, target_lang):
        LOGGER.info(
            "item %s: already in %s, skipping translation",
            item_ctx.get("item_id", "?"),
            target_lang,
        )
        return str(store(""))

    base_url, key, models = _configured()
    language = _LANG_NAMES.get(target_lang, target_lang)

    # What the provider is asked to translate, and the template the product is
    # rebuilt from. The units of translation are the article's own blocks, so
    # the prose is not reflowed across tag boundaries.
    body, units = _split_blocks(prose=source_text, markup=item_ctx.get("content", ""))
    requests: list[str] = []
    for unit in units:
        unit_text = str(unit["text"])
        if unit_text:
            requests.extend(_segment(unit_text))
    if len(requests) > 1:
        LOGGER.info(
            "item %s: %d blocks split into %d segments for translation",
            item_ctx.get("item_id", "?"),
            len(units),
            len(requests),
        )

    translated = [
        _translate_one(chunk, language=language, base=base_url, key=key, models=models)
        for chunk in requests
    ]
    product = _rebuild(body=body, units=units, translated=translated)
    # Absolutised for the same reason the fulltext extractor does it: the
    # artifact is rendered in a reader or on a phone, where a relative path
    # points at the reader's host instead of the article's (issue #39).
    item_url = str(item_ctx.get("url") or "")
    if item_url:
        product = _absolutise(product, base=item_url)
    # The provider's text is assembled into *markup* now, so the store-time
    # boundary has to apply here too (ADR 0007, ADR 0031). Writing the product
    # as a document and letting the sanitiser take it back to its allowlist
    # means a reply containing `</p><script>` is neutralised rather than
    # published; when the product is built by escaping text into fresh tags
    # this is a no-op, which is why the output is identical either way.
    ref: str = store(_sanitise(product))
    return ref


def _sanitise(product: str) -> str:
    """The stored product: the markup back through the allowlist, no wrapper.

    The `<div>` is a parsing frame, not content -- it is what makes a fragment
    parse as a document and what the sanitiser then hands back -- so it is
    removed explicitly rather than relied upon to disappear. A body that
    sanitises to nothing (an empty reply, a reply that was entirely dropped
    markup) stores the empty artifact the title-only path already produces,
    which is a SUCCESS and opens the gate.
    """
    cleaned = sanitize_html(f"<div>{product}</div>")
    if cleaned.startswith("<div>") and cleaned.endswith("</div>"):
        cleaned = cleaned[len("<div>") : -len("</div>")]
    return cleaned


# Block-level tags whose *text* is prose worth translating. A block is one
# translation unit on purpose: sending the flattened article as one string and
# re-wrapping the reply by line is what collapsed multi-paragraph bodies into a
# single paragraph and lost every tag around them (#33).
_TRANSLATABLE_BLOCKS: Final[frozenset[str]] = frozenset(
    {
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "p",
        "li",
        "blockquote",
        "figcaption",
        "dd",
        "dt",
        "td",
        "th",
        "summary",
        "caption",
        "div",
    }
)

# Non-text nodes copied through verbatim, never sent to a provider. Code is
# here because `pre`/`code` content is not prose and its whitespace is
# semantics; img keeps its alt (not prose to translate).
_PRESERVED: Final[frozenset[str]] = frozenset({"pre", "code", "img", "svg", "math"})

# Containers that are never article content in themselves.
_SKIPPED: Final[frozenset[str]] = frozenset({"script", "style", "noscript", "template"})


def _split_blocks(
    *, prose: str, markup: str
) -> tuple[lxml_html.HtmlElement | None, list[dict[str, Any]]]:
    """Split the body into the units to translate, in document order.

    Returns the parsed body (None when there is nothing to walk) plus one dict
    per unit. A unit with empty `text` is a preserved node (an image, a code
    block) and is left alone; a unit with text is a translatable block whose
    content is replaced by its translation.

    The tree is returned rather than a list of tag shells so that *container*
    structure survives by construction: `ul`, `figure`, `table` and `blockquote`
    are rebuilt around their own children instead of being dropped because the
    walk flattened them away (which is how `<ul>` was lost the first time).

    Falls back to a single text unit when the markup cannot be parsed, because
    the alternative is refusing to translate a body whose *text* is perfectly
    readable -- the input this processor is trusted with is sanitised HTML, and
    a malformed page should still get a translation.
    """
    if not markup.strip():
        return None, [_text_unit(prose)]
    # unreachable: lxml's HTML parser repairs rather than rejects, so the
    # `<div>` frame above always parses (measured against NUL bytes,
    # unterminated tags and a stray DOCTYPE). Kept because the fallback is what
    # a future parser change would need, not because input can reach it.
    document = lxml_html.fromstring(f"<div>{markup}</div>")  # pragma: no cover

    units = _collect_units(document)
    if not units:
        return None, [_text_unit(prose)]
    return document, units


def _text_unit(text: str) -> dict[str, Any]:
    """A bare text unit: the fallback shape, and the title-only case."""
    return {"element": None, "text": text, "shells": []}


def _collect_units(root: lxml_html.HtmlElement) -> list[dict[str, Any]]:
    """Walk the tree, emitting one unit per translatable block.

    The walk stops descending at a translatable block: an `li` containing a
    `p` is ONE unit (`li`'s prose includes the `p`'s), not two, so the same
    sentence cannot be translated twice and reassembled out of order.

    A block's *inline* children (links, emphasis, inline code) are kept as
    paired marker shells rather than flattened, so the rebuilt block still has
    working links and its emphasis -- "同构" as the issue asks, without asking
    the provider to translate a sentence in fragments.
    """
    units: list[dict[str, Any]] = []
    for child in root.iterchildren():
        if not isinstance(child.tag, str):
            continue  # comments and processing instructions
        tag = child.tag.lower()
        if tag in _SKIPPED:
            continue
        if tag in _PRESERVED:
            units.append({"element": child, "text": "", "shells": []})
            continue
        if tag in _TRANSLATABLE_BLOCKS:
            shells: list[tuple[str, str]] = []
            text = _flatten_inline(child, shells)
            units.append({"element": child, "text": text, "shells": shells})
            continue
        # A structural wrapper (article/section/ul/ol/table/figure/...): recurse
        # without emitting anything of its own.
        units.extend(_collect_units(child))
    return units


# Inline elements worth keeping inside a translated block. Each becomes a
# paired marker around its own text, so the provider translates the whole
# sentence while the tag survives; `a` is the one that really matters (a
# translation whose links are gone is a dead end for the reader).
_INLINE: Final[frozenset[str]] = frozenset(
    {
        "a",
        "strong",
        "b",
        "em",
        "i",
        "u",
        "s",
        "del",
        "ins",
        "mark",
        "small",
        "sub",
        "sup",
        "abbr",
        "q",
        "cite",
        "time",
        "span",
    }
)


def _flatten_inline(
    element: lxml_html.HtmlElement, shells: list[tuple[str, str]]
) -> str:
    """An element's text with inline children replaced by paired markers.

    `[[0]]inner[[/0]]` marks the region that belongs inside the element whose
    shell is `shells[0]`. Paired rather than one-sided so the *translated*
    inner text can be recovered and put back inside the original tag: a
    one-sided marker would leave the shell with nothing to hold.
    """
    parts: list[str] = [element.text or ""]
    for child in element.iterchildren():
        if not isinstance(child.tag, str):
            # A comment carries no text of its own; whatever follows it is the
            # enclosing block's text, so the tail is kept and the node skipped.
            # Reachable: `<p>a<!-- c -->b</p>` makes the tail `"b"`, which is
            # part of the paragraph's prose.
            parts.append(child.tail or "")
            continue
        index = len(shells)
        tag = child.tag.lower()
        if tag not in _INLINE or tag in _PRESERVED:
            # Inline code, or anything not worth a shell: keep the text, drop
            # the tag. `code` is here because its content is not prose.
            parts.append(_flatten_inline(child, shells))
            parts.append(child.tail or "")
            continue
        open_tag = _open_tag(child)
        close_tag = f"</{tag}>"
        shells.append((open_tag, close_tag))
        inner = _flatten_inline(child, shells)
        parts.append(f"[[{index}]]{inner}[[/{index}]]")
        parts.append(child.tail or "")
    return "".join(parts)


def _open_tag(element: lxml_html.HtmlElement) -> str:
    """The element's opening tag with its attributes, and nothing inside."""
    attributes = "".join(
        f' {name}="{value}"' for name, value in sorted(element.attrib.items())
    )
    tag = element.tag.lower() if isinstance(element.tag, str) else "span"
    return f"<{tag}{attributes}>"


def _is_element(node: Any) -> bool:
    """Whether a node is a real element (not a comment or PI).

    lxml yields comments and processing instructions from `iterchildren()` and
    types them as elements, so the check is a runtime necessity even though the
    stubs claim it is always true.
    """
    return isinstance(getattr(node, "tag", None), str)


def _serialize(element: lxml_html.HtmlElement) -> str:
    """An element's subtree as HTML (the tag shell the product is rebuilt in)."""
    return str(lxml_html.tostring(element, encoding="unicode"))


def _rebuild(
    *,
    body: lxml_html.HtmlElement | None,
    units: list[dict[str, Any]],
    translated: list[str],
) -> str:
    """Put each translation back into its block, and serialize the body.

    `translated` is consumed in order. A unit's prose is one segment unless it
    exceeds MAX_CHARS, so a unit maps to one or more replies; `_fill_unit`
    consumes the replies for one unit only, which is what keeps a short
    provider reply from shifting later blocks (they keep their original text
    instead of receiving someone else's translation).
    """
    cursor = 0
    for unit in units:
        if not unit["text"]:
            continue  # preserved node: image, code block -- left untouched
        count = max(1, len(_segment(unit["text"])))
        replies = translated[cursor : cursor + count]
        cursor += count
        _fill_unit(unit, replies)
    if body is None:
        # The fallback path: no tree to rebuild, so the replies are the
        # paragraphs and the units list holds exactly one of them.
        return "".join(_paragraphs(reply) for reply in translated if reply.strip())
    return "".join(
        _serialize(child) for child in body.iterchildren() if _is_element(child)
    )


def _fill_unit(unit: dict[str, Any], replies: list[str]) -> None:
    """Put one unit's translation into its element, in place.

    Inline markers are restored first, so the block's links and emphasis are
    inside the translation. A block the provider did not answer for keeps its
    original text: that is the only outcome that cannot mislead, since a
    borrowed neighbour's translation reads as plausible output.
    """
    text = "".join(reply.strip() for reply in replies if reply.strip())
    if not text:
        # unreachable: `_parse_reply` rejects an empty completion, so a block's
        # replies are never all-blank. This is the guard for a provider whose
        # contract changes, and the block would keep its original text.
        return  # pragma: no cover
    element = unit["element"]
    if element is None:
        return
    restored = _restore_inline(text, unit.get("shells") or [])
    _replace_content(element, restored)


# Innermost-only: the inner group refuses a `[` so a nested pair (`<a><strong>`
# is `[[0]][[1]]text[[/1]][[/0]]`) cannot be paired with its outer neighbour. A
# non-greedy "first close wins" pattern cross-pairs them -- open 0 with close 1
# -- and, because that mismatch is treated as a mangled marker, both tags were
# silently dropped. The backreference keeps open and close the same index.
_MARKER = re.compile(r"\[\[(?P<index>\d+)\]\](?P<inner>[^\[\]]*)\[\[/(?P=index)\]\]")


def _restore_inline(text: str, shells: list[tuple[str, str]]) -> str:
    """Wrap each marked span in its original tag.

    A marker the provider dropped, duplicated or unbalanced is dropped from the
    output rather than guessed at: the translated *text* is the part that
    matters, and a misplaced link is worse than a missing tag. The prose
    survives either way, and this is the only failure mode here -- it cannot
    corrupt the text.
    """
    if not shells:
        return text
    result = text
    # Each pass resolves the innermost markers only, so a nested pair
    # (`<a><strong>x</strong></a>`) needs one pass per level; 8 levels of
    # inline nesting is far past anything a feed produces.
    for _ in range(8):
        replaced = False

        def _sub(match: re.Match[str]) -> str:
            nonlocal replaced
            index = int(match.group("index"))
            inner = match.group("inner")
            if index >= len(shells):
                # An index the provider invented: keep the text, drop the tag.
                return inner
            open_tag, close_tag = shells[index]
            replaced = True
            return f"{open_tag}{inner}{close_tag}"

        result = _MARKER.sub(_sub, result)
        if not replaced:
            break
    # Any marker left over (unbalanced, or an index the provider invented) is
    # stripped so no `[[0]]` reaches the reader.
    return _LEFTOVER_MARKER.sub("", result)


_LEFTOVER_MARKER = re.compile(r"\[\[/?\d+\]\]")


def _paragraphs(text: str) -> str:
    """Wrap plain text as paragraphs, one per line the provider returned."""
    return "".join(
        f"<p>{html.escape(line.strip())}</p>"
        for line in text.splitlines()
        if line.strip()
    )


def _replace_content(element: lxml_html.HtmlElement, inner: str) -> None:
    """Replace `element`'s content with `inner`, keeping the tag and attributes.

    The node is rebuilt by *parsing* `<tag attrs>inner</tag>` rather than by
    setting `.text`, because `inner` may contain restored inline markup (a link,
    emphasis) and assigning it as text would escape it into visible
    `&lt;strong&gt;`. The opening tag is taken from the element's own
    serialization, so attributes, their quoting and an empty `<p>` survive
    exactly as the source had them.

    A parse failure leaves the element untouched -- the original text, still
    inside its own tag. That is the safe direction: a mangled tag is worse than
    an untranslated block, and the block is still readable.
    """
    tag = element.tag.lower() if isinstance(element.tag, str) else ""
    # unreachable: the frame is built from this element's own opening tag, which
    # the parser has already accepted once (checked by fuzzing 3000 hostile
    # bodies: the HTML parser repairs rather than rejects). A failure here would
    # keep the block's original text, which is the safe direction.
    try:  # pragma: no cover
        serialized = _serialize(element)
        open_tag = serialized[: serialized.index(">") + 1]
        rebuilt = lxml_html.fromstring(f"{open_tag}{inner}</{tag}>")
    except Exception:  # pragma: no cover
        LOGGER.warning("could not rebuild the %s block; keeping its original text", tag)
        return
    parent = element.getparent()
    if parent is None:
        return  # pragma: no cover - a unit element always came from the walk
    parent.replace(element, rebuilt)


def _absolutise(markup: str, *, base: str) -> str:
    """Resolve relative `href`/`src` against the item URL.

    Only for `a` and `img`: those are the attributes a reader follows or
    fetches. Best-effort by design -- if the fragment cannot be parsed the
    markup is returned untouched, because a link that stays relative is a
    cosmetic problem while a dropped product is not.
    """
    # unreachable for the same reason as _split_blocks: the parser repairs the
    # frame instead of rejecting it. Best-effort is still the contract -- a
    # link left relative is cosmetic, a dropped product is not.
    try:  # pragma: no cover
        wrapper = lxml_html.fromstring(f"<div>{markup}</div>")
    except Exception:  # pragma: no cover
        return markup
    for element in wrapper.iter():
        if not _is_element(element):
            continue  # pragma: no cover - a comment carries no href or src
        attribute = {"a": "href", "img": "src"}.get(str(element.tag).lower())
        if attribute and element.get(attribute):
            element.set(attribute, urljoin(base, element.get(attribute)))
    return "".join(
        _serialize(child) for child in wrapper.iterchildren() if _is_element(child)
    )


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
            # A 4xx that is not 401/429 is the request's fault, not the
            # network's: an unknown model name, a malformed body, an endpoint
            # that does not exist. Classifying those as `network` made the
            # worker retry them to exhaustion -- three identical 400s, each
            # after a backoff, while the real problem was a configuration
            # string that only surfaced once the attempts ran out.
            #
            # `parse` is the taxonomy's home for "this cannot be processed as
            # things stand": not retried automatically, and not `auth` either
            # (the key was accepted; the request was refused).
            if 400 <= response.status_code < 500:
                raise ProcessorError(
                    ErrorClass.PARSE,
                    f"provider rejected the request: HTTP {response.status_code}"
                    f" ({_provider_reason(response)})",
                )
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
