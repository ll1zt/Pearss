"""The translate processor: input shaping, the title-only fallback, and the
model fallback chain."""

from __future__ import annotations

import gzip
import importlib
import json
import logging
import pathlib
import time

import httpx
import pytest

from core.enums import ErrorClass
from core.language import prose_text
from core.plugins.hooks import ProcessorError
from plugins.processors.translate.processor import (
    MAX_ATTEMPT_SECONDS,
    MAX_CHARS,
    _segment,
    attempt_timeout,
    process,
    processor_id,
)

# Declared for the whole module: the recipe targets select by marker, so
# without this every test here is deselected and never runs. The language
# verdicts below are this processor's gate -- they were written and then
# invisible.
pytestmark = pytest.mark.unit


def test_processor_id_is_stable() -> None:
    assert processor_id() == "translate"


def test_the_shared_extractor_removes_markup_and_decodes_entities() -> None:
    assert prose_text("<p>a &amp; b<br>c</p>") == "a & b c"


def test_title_only_items_translate_their_title(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """A title-only item (announcement feeds) still gets a product.

    Failing on empty content would deadlock the gate on items the chain can
    never act on - the exact failure an naist subscription produced live.
    """
    from plugins.processors.translate.processor import _reset_config_cache

    _reset_config_cache()
    monkeypatch.setenv("PEARSS_TRANSLATE_BASE_URL", "https://llm.example/v1")
    monkeypatch.setenv("PEARSS_TRANSLATE_API_KEY", "k")
    monkeypatch.setenv("PEARSS_TRANSLATE_MODEL", "m")

    captured: dict[str, object] = {}

    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        request = httpx.Request("POST", url, json=kwargs.get("json"))
        captured["text"] = json.loads(request.read())["messages"][1]["content"]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "承認発表の翻訳"}}]},
        )

    monkeypatch.setattr(httpx, "post", fake_post)

    stored: list[str] = []

    def fake_store(content: str) -> str:
        stored.append(content)
        return "cas://fake"

    ref = process(
        {"title": "合格発表", "content": ""},
        target_lang="zh",
        store=fake_store,
    )

    assert ref == "cas://fake"
    assert stored == ["<p>承認発表の翻訳</p>"]
    assert captured["text"] == "合格発表", "the title is the translation input"


def test_a_genuinely_empty_item_succeeds_with_an_empty_artifact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """No text anywhere: the chain completes with an empty product rather
    than failing forever - 'nothing to process' is a completion."""
    from plugins.processors.translate.processor import _reset_config_cache

    _reset_config_cache()
    monkeypatch.setenv("PEARSS_TRANSLATE_BASE_URL", "https://llm.example/v1")
    monkeypatch.setenv("PEARSS_TRANSLATE_API_KEY", "k")
    monkeypatch.setenv("PEARSS_TRANSLATE_MODEL", "m")

    called = False

    def origin(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]})

    ref = process(
        {"title": "", "content": ""}, target_lang="zh", store=lambda c: "cas://empty"
    )
    assert ref == "cas://empty"
    assert called is False, "no provider call for an empty item"


def test_an_unconfigured_provider_is_parse_not_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.processors.translate.processor import _reset_config_cache

    monkeypatch.delenv("PEARSS_TRANSLATE_BASE_URL", raising=False)
    monkeypatch.delenv("PEARSS_TRANSLATE_API_KEY", raising=False)
    monkeypatch.delenv("PEARSS_TRANSLATE_MODEL", raising=False)
    monkeypatch.delenv("PEARSS_TRANSLATE_MODELS", raising=False)
    _reset_config_cache()
    with pytest.raises(ProcessorError) as excinfo:
        process({"title": "t", "content": "c"}, target_lang="zh", store=lambda c: "x")
    assert excinfo.value.error_class is ErrorClass.PARSE


def test_a_gzipped_provider_reply_is_decoded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Fixture realism (2026-09-09): real providers send gzip; a handler that
    forgets the header pair would have hidden the double-decompression bug."""
    from plugins.processors.translate.processor import _reset_config_cache

    _reset_config_cache()
    monkeypatch.setenv("PEARSS_TRANSLATE_BASE_URL", "https://llm.example/v1")
    monkeypatch.setenv("PEARSS_TRANSLATE_API_KEY", "k")
    monkeypatch.setenv("PEARSS_TRANSLATE_MODEL", "m")

    def origin(request: httpx.Request) -> httpx.Response:
        body = gzip.compress(
            json.dumps({"choices": [{"message": {"content": "訳文"}}]}).encode()
        )
        return httpx.Response(
            200,
            content=body,
            headers={"content-encoding": "gzip"},
        )

    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        return origin(httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    ref = process(
        {"title": "t", "content": "本文"}, target_lang="ja", store=lambda c: "cas://ok"
    )
    assert ref == "cas://ok"


def test_segmentation_preserves_every_character() -> None:
    """No truncation: long text is split and every non-whitespace char
    survives across the chunk boundaries (ADR 0018 review commitment)."""

    text = "段落一。\n\n" + "b" * 30000 + "\n\n段落三。"
    chunks = _segment(text)
    assert len(chunks) > 1

    def strip_all(s: str) -> str:
        return "".join(s.split())

    assert strip_all("\n\n".join(chunks)) == strip_all(text)
    assert all(len(chunk) <= MAX_CHARS for chunk in chunks)


# --------------------------------------------------------------------------
# Skipping content already in the target language
# --------------------------------------------------------------------------


def _no_provider(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Fail the test if any HTTP call is attempted.

    The skip path must not reach the provider at all: that is the whole point,
    and an accidental call is invisible in the return value (both paths return
    a ref), so it has to be asserted directly.
    """
    from plugins.processors.translate.processor import _reset_config_cache

    _reset_config_cache()
    for name in (
        "PEARSS_TRANSLATE_BASE_URL",
        "PEARSS_TRANSLATE_API_KEY",
        "PEARSS_TRANSLATE_MODEL",
        "PEARSS_TRANSLATE_MODELS",
    ):
        monkeypatch.delenv(name, raising=False)
    calls: list[str] = []

    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        calls.append(url)
        raise AssertionError(f"provider called for already-target text: {url}")

    monkeypatch.setattr(httpx, "post", fake_post)
    return calls


def test_chinese_content_is_not_sent_for_translation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The common case: a Chinese post in a feed targeting Chinese.

    Sampled live at ~69% of one X account's timeline, all of it pointless work
    that also held the publish gate shut.
    """
    calls = _no_provider(monkeypatch)
    stored: list[str] = []

    def fake_store(content: str) -> str:
        stored.append(content)
        return "cas://skip"

    ref = process(
        {
            "title": "如何评价这个方案？",
            "content": "<p>我觉得这个设计有几个明显的取舍需要讨论，首先是复杂度。</p>",
        },
        target_lang="zh",
        store=fake_store,
    )
    assert ref == "cas://skip"
    assert calls == []
    # An empty artifact, the same shape as an item with no text: the gate opens
    # on SUCCEEDED, and the Reader API only merges a translation whose ref is
    # non-empty, so the original body is served alone.
    assert stored == [""]


def test_mixed_chinese_with_a_latin_tail_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real posts mix scripts. Majority-Chinese still means no translation."""
    _no_provider(monkeypatch)
    ref = process(
        {"title": "t", "content": "如果真是“自我提升向内求”，respect。" * 3},
        target_lang="zh",
        store=lambda c: "cas://skip",
    )
    assert ref == "cas://skip"


def test_english_content_is_still_translated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The skip must not swallow the work it exists to protect."""
    from plugins.processors.translate.processor import _reset_config_cache

    _reset_config_cache()
    monkeypatch.setenv("PEARSS_TRANSLATE_BASE_URL", "https://llm.example/v1")
    monkeypatch.setenv("PEARSS_TRANSLATE_API_KEY", "k")
    monkeypatch.setenv("PEARSS_TRANSLATE_MODEL", "m")
    seen: list[str] = []

    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        request = httpx.Request("POST", url, json=kwargs.get("json"))
        seen.append(json.loads(request.read())["messages"][1]["content"])
        return httpx.Response(200, json={"choices": [{"message": {"content": "译文"}}]})

    monkeypatch.setattr(httpx, "post", fake_post)
    ref = process(
        {
            "title": "t",
            "content": "<p>" + "This is a long English sentence. " * 4 + "</p>",
        },
        target_lang="zh",
        store=lambda c: "cas://translated",
    )
    assert ref == "cas://translated"
    assert seen, "the provider should have been called for English text"


def test_short_content_is_translated_rather_than_guessed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under the sample floor a ratio is noise, so the safe direction wins.

    "OK" and "2026" are not evidence of a language; sending them costs a
    fraction of a cent, skipping them would leave text unread.
    """
    from plugins.processors.translate.processor import _reset_config_cache

    _reset_config_cache()
    monkeypatch.setenv("PEARSS_TRANSLATE_BASE_URL", "https://llm.example/v1")
    monkeypatch.setenv("PEARSS_TRANSLATE_API_KEY", "k")
    monkeypatch.setenv("PEARSS_TRANSLATE_MODEL", "m")
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **kw: httpx.Response(
            200, json={"choices": [{"message": {"content": "好"}}]}
        ),
    )
    ref = process(
        {"title": "OK", "content": "<p>好的</p>"},
        target_lang="zh",
        store=lambda c: "cas://translated",
    )
    assert ref == "cas://translated"


def test_japanese_is_not_mistaken_for_chinese(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kanji fall inside the Chinese ranges; kana is what marks Japanese.

    The body is deliberately kanji-heavy with only a little kana. Under the
    correct ordering the kana chars are claimed by `ja`, the kanji fall to `zh`,
    and the target share stays under the threshold. Listing the CJK ranges
    first would make the kana unreachable (they sit in no CJK range, so they
    count as "other") and leave a kanji-only majority reading as Chinese,
    skipping Japanese text the reader cannot read.

    A kana-dominant body passes under either ordering, which is why the fixture
    is weighted toward kanji -- verified by reordering the ranges and watching
    only this test go red.
    """
    from plugins.processors.translate.processor import _reset_config_cache

    _reset_config_cache()
    monkeypatch.setenv("PEARSS_TRANSLATE_BASE_URL", "https://llm.example/v1")
    monkeypatch.setenv("PEARSS_TRANSLATE_API_KEY", "k")
    monkeypatch.setenv("PEARSS_TRANSLATE_MODEL", "m")
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **kw: httpx.Response(
            200, json={"choices": [{"message": {"content": "译文"}}]}
        ),
    )
    calls: list[str] = []

    def recording_post(url: str, **kwargs: object) -> httpx.Response:
        calls.append(url)
        return httpx.Response(200, json={"choices": [{"message": {"content": "译文"}}]})

    monkeypatch.setattr(httpx, "post", recording_post)
    kanji_heavy = "日本語文章漢字多数今日天気図書館勉強会資料作成確認済"
    ref = process(
        {"title": "t", "content": f"<p>{kanji_heavy}です。</p>"},
        target_lang="zh",
        store=lambda c: "cas://empty" if c == "" else "cas://translated",
    )
    # Asserting the provider was reached, not just the returned ref: both the
    # skip and the translate path return a ref, so only the call proves the
    # text was not silently classified as Chinese.
    assert calls, "kanji-heavy Japanese was skipped as Chinese"
    assert ref == "cas://translated"


def test_an_unknown_target_language_is_never_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ranges for a language means no shortcut, not a wrong one."""
    from core.language import is_already_in

    assert is_already_in("Это довольно длинный русский текст.", "ru") is False
    assert is_already_in("这是一段足够长的中文内容，不应该被跳过。", "ru") is False


# --------------------------------------------------------------------------
# Failure classification
# --------------------------------------------------------------------------


def _provider(monkeypatch: pytest.MonkeyPatch, status: int, body: object) -> None:
    """Configure a provider and make every request answer `status`."""
    from plugins.processors.translate.processor import _reset_config_cache

    _reset_config_cache()
    monkeypatch.setenv("PEARSS_TRANSLATE_BASE_URL", "https://llm.example/v1")
    monkeypatch.setenv("PEARSS_TRANSLATE_API_KEY", "k")
    monkeypatch.setenv("PEARSS_TRANSLATE_MODEL", "m")
    monkeypatch.delenv("PEARSS_TRANSLATE_MODELS", raising=False)
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **kw: httpx.Response(status, json=body),
    )


def _classify() -> ProcessorError:
    with pytest.raises(ProcessorError) as excinfo:
        process({"title": "t", "content": "c"}, target_lang="zh", store=lambda c: "x")
    return excinfo.value


def test_a_rejected_request_is_parse_not_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 400 is the request's fault, so retrying it is pure waste.

    Observed live: a model name set in the singular env var holding a
    comma-separated pair made every call answer 400. Classified as `network`,
    the worker retried each one three times with backoff and reported a
    network problem, which pointed the operator at DNS instead of at the
    setting that was wrong.
    """
    _provider(
        monkeypatch,
        400,
        {
            "error": {
                "message": "unknown provider for model m",
                "type": "invalid_request_error",
            }
        },
    )
    exc = _classify()
    assert exc.error_class is ErrorClass.PARSE
    assert "unknown provider for model m" in exc.detail
    assert "400" in exc.detail


def test_a_missing_endpoint_is_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    _provider(monkeypatch, 404, {"error": {"message": "not found"}})
    assert _classify().error_class is ErrorClass.PARSE


def test_a_rejected_key_is_still_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """401 keeps its own class: the remedy is a new key, not a fixed request."""
    _provider(monkeypatch, 401, {"error": {"message": "invalid api key"}})
    assert _classify().error_class is ErrorClass.AUTH


def test_a_rate_limit_is_still_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """429 stays retryable: waiting is exactly the right response."""
    _provider(monkeypatch, 429, {"error": {"message": "rate limited"}})
    assert _classify().error_class is ErrorClass.BLOCKED


def test_a_server_error_is_still_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """5xx is genuinely transient, so the retry policy should keep applying."""
    _provider(monkeypatch, 503, {"error": {"message": "overloaded"}})
    assert _classify().error_class is ErrorClass.NETWORK


def test_an_unparseable_error_body_does_not_break_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reason is a courtesy; the class is the contract."""
    from plugins.processors.translate.processor import _reset_config_cache

    _reset_config_cache()
    monkeypatch.setenv("PEARSS_TRANSLATE_BASE_URL", "https://llm.example/v1")
    monkeypatch.setenv("PEARSS_TRANSLATE_API_KEY", "k")
    monkeypatch.setenv("PEARSS_TRANSLATE_MODEL", "m")
    monkeypatch.delenv("PEARSS_TRANSLATE_MODELS", raising=False)
    monkeypatch.setattr(
        httpx, "post", lambda url, **kw: httpx.Response(400, text="<html>nope</html>")
    )
    exc = _classify()
    assert exc.error_class is ErrorClass.PARSE
    assert "no parseable error body" in exc.detail


# --------------------------------------------------------------------------
# Structure-preserving translation (ADR 0031, issue #33)
#
# The register for these scenarios is this header; they are plugin-level and
# live with the plugin, unlike the chain-level ones (AC-14..AC-18) which are in
# tests/integration/test_chain_dataflow.py.
#
#   AC-19 译文保留原文块级结构
#     Given 条目正文含 h2/strong/figure+img/pre+code/ul+li/a
#     When 翻译
#     Then 产物保留这些标签（不是一串裸 <p>）
#
#   AC-20 非文本节点不送 provider，且原样保留
#     Given 正文含 <pre><code> 代码块与 <img>
#     When 翻译
#     Then provider 输入不含代码内容；产物中的代码原样（缩进/换行不折叠）、
#          图片 src 与 alt 原样
#
#   AC-21 图片地址绝对化
#     Given 正文用相对路径 <img src="/img/x.png">
#     When 翻译
#     Then 产物中的 src 以条目 URL 为基解析为绝对 URL
#
#   AC-22 provider 段数不匹配时不静默错位
#     Given provider 返回的段数少于请求的段数
#     When 翻译
#     Then 不把译文错位拼接到别的段落上
#
#   AC-23 纯文本正文的产物形态不变
#     Given 条目正文是普通段落(无结构)
#     When 翻译
#     Then 产物仍是段落序列, 可读性不退化
#
#   AC-24 provider 的回复是不可信数据
#     Given provider 返回含 </p><script> 的回复
#     When 组装产物
#     Then 产物经 sanitize_html 后无残留, 且不带解析用包装
# --------------------------------------------------------------------------

RICH_BODY = (
    "<h2>Understanding eBPF</h2>"
    "<p>eBPF runs sandboxed programs <strong>in the kernel</strong>.</p>"
    '<figure><img src="/img/ebpf.png" alt="Architecture diagram"></figure>'
    "<pre><code>bpftool prog list\n    bpftrace -e 'kprobe:do_sys_open'</code></pre>"
    "<ul><li>Observability</li><li>Networking</li></ul>"
    '<p>See the <a href="/docs/start">official docs</a>.</p>'
)


def _rich_provider(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """A provider that marks each request so the product is greppable.

    Records every input, so a test can assert what was *not* sent (the code
    block, the image alt text) as well as what came back.
    """
    from plugins.processors.translate.processor import _reset_config_cache

    _reset_config_cache()
    monkeypatch.setenv("PEARSS_TRANSLATE_BASE_URL", "https://llm.example/v1")
    monkeypatch.setenv("PEARSS_TRANSLATE_API_KEY", "k")
    monkeypatch.setenv("PEARSS_TRANSLATE_MODEL", "m")
    seen: list[str] = []

    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        request = httpx.Request("POST", url, json=kwargs.get("json"))
        user = json.loads(request.read())["messages"][1]["content"]
        seen.append(user)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": f"[译]{user}"}}]}
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    return seen


def _translate(
    monkeypatch: pytest.MonkeyPatch,
    body: str,
    *,
    url: str = "https://blog.example/post",
) -> tuple[str, list[str]]:
    seen = _rich_provider(monkeypatch)
    stored: list[str] = []

    def fake_store(content: str) -> str:
        stored.append(content)
        return "cas://product"

    ref = process(
        {"item_id": "i-1", "title": "T", "content": body, "url": url},
        target_lang="zh",
        store=fake_store,
    )
    assert ref == "cas://product"
    assert stored, "a product must have been stored"
    return stored[0], seen


def test_the_translation_keeps_the_articles_structure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-19: headings, emphasis, images, code, lists and links survive.

    Measured on the deployed code before this fix: every one of these tags was
    gone from the product, leaving a single wall of `<p>` text -- so the
    translated article had no headings to skim, no diagram, no code block and
    no links, while the original beneath it had all of them.
    """
    product, _ = _translate(monkeypatch, RICH_BODY)

    for tag in ("<h2", "<strong", "<img", "<pre", "<code", "<ul", "<li", "<a "):
        assert tag in product, f"{tag} was dropped from the product: {product[:200]}"


def test_the_code_block_is_not_sent_to_the_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-20: code is copied through, never translated.

    A second defect the reproduction surfaced: the code block *was* sent, so a
    provider was asked to translate `bpftrace -e 'kprobe:do_sys_open'` -- a
    pointless call, a chance to mangle it, and the reason its indentation came
    back collapsed (whitespace is code semantics).
    """
    product, seen = _translate(monkeypatch, RICH_BODY)

    joined = "\n".join(seen)
    assert "bpftrace" not in joined, (
        f"the provider must not be asked to translate code; it saw {joined[:200]}"
    )
    assert "kprobe" not in joined
    # ...and the code survives verbatim in the product, whitespace included.
    assert "bpftool prog list\n    bpftrace -e 'kprobe:do_sys_open'" in product, (
        f"the code block must be preserved verbatim: {product[:300]}"
    )


def test_the_image_is_preserved_with_its_alt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-20: an image is not text; it is carried through untouched."""
    product, seen = _translate(monkeypatch, RICH_BODY)

    assert 'alt="Architecture diagram"' in product, product[:300]
    assert "Architecture diagram" not in "\n".join(seen), (
        "alt text is not prose to translate"
    )


def test_a_relative_image_url_becomes_absolute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-21: the product is rendered elsewhere, so relative is a broken link.

    Same reasoning as the fulltext extractor's URL handling (issue #39): the
    artifact is served in a reader or on a phone, where `/img/ebpf.png` points
    at the reader's own host.
    """
    product, _ = _translate(monkeypatch, RICH_BODY, url="https://blog.example/post")

    assert 'src="https://blog.example/img/ebpf.png"' in product, product[:300]


def test_a_provider_reply_with_no_content_fails_visibly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-22: a provider that answers nothing produces a failure, not a product.

    The rule that matters is the one a partial reply could break: a block must
    never receive *another* block's translation, because that product reads as
    plausible output while carrying the wrong text under the right heading.
    Driving the provider to return fewer blocks than were asked for shows how
    that is guaranteed here -- an empty `choices[0].message.content` is already
    a `parse` failure (the existing reply contract), so the item is retried or
    reported rather than published half-misaligned.

    This test exists to pin that interaction, since the block-per-request shape
    (ADR 0031) makes "a reply per block" the expectation a provider could
    violate in exactly this way.
    """
    from plugins.processors.translate.processor import _reset_config_cache

    _reset_config_cache()
    monkeypatch.setenv("PEARSS_TRANSLATE_BASE_URL", "https://llm.example/v1")
    monkeypatch.setenv("PEARSS_TRANSLATE_API_KEY", "k")
    monkeypatch.setenv("PEARSS_TRANSLATE_MODEL", "m")

    replies = iter(["【一】", ""])  # the second block gets no content

    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        content = next(replies, "")
        return httpx.Response(
            200, json={"choices": [{"message": {"content": content}}]}
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    body = "<p>First paragraph.</p><p>Second paragraph.</p>"
    stored: list[str] = []

    def fake_store(content: str) -> str:
        stored.append(content)
        return "cas://product"

    with pytest.raises(ProcessorError) as excinfo:
        process(
            {
                "item_id": "i",
                "title": "T",
                "content": body,
                "url": "https://b.example/p",
            },
            target_lang="zh",
            store=fake_store,
        )
    assert excinfo.value.error_class is ErrorClass.PARSE, (
        "an empty reply is the existing parse failure; classification unchanged"
    )
    assert stored == [], "no partial product may be stored"


def test_plain_paragraphs_still_produce_readable_paragraphs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-23: the overwhelmingly common input must not degrade.

    Most feeds are plain prose. The product must remain a paragraph sequence
    with the translated text, not a wrapped or restructured document.
    """
    product, seen = _translate(
        monkeypatch, "<p>Hello there.</p><p>Second paragraph.</p>"
    )

    assert product.count("<p>") == 2, product
    assert "Hello there." in product
    assert "Second paragraph." in product
    assert "<div" not in product and "<article" not in product, (
        "plain prose must not gain a wrapper element"
    )
    # One request per paragraph, and that is the deliberate trade (ADR 0031):
    # a unit's prose has to reach the provider whole or the rebuilt block would
    # be a fragment of a sentence inside someone else's tag. The per-block
    # request is also what makes "which block failed?" answerable.
    assert len(seen) == 2, f"one request per paragraph, got {len(seen)}"


# --------------------------------------------------------------------------
# The structure machinery's own edge cases
#
# These are the paths the structure change added that a happy-path test cannot
# reach: markup the parser rejects, nodes that must be skipped, inline content
# that is not worth a shell, and markers a provider mangles. Each one is a
# degradation path, so each is pinned to the degradation it promises rather
# than to the code that implements it.
# --------------------------------------------------------------------------


def test_markup_the_parser_rejects_falls_back_to_prose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unparseable body must still be translated, as plain paragraphs.

    The input this processor is handed is sanitised HTML, so this is a
    should-not-happen path -- but the wrong response is to refuse. The *text*
    is perfectly readable, and refusing would hold an item's gate shut over a
    tag the parser disliked.
    """
    from plugins.processors.translate.processor import _split_blocks

    body, units = _split_blocks(prose="Some readable prose.", markup="")
    assert body is None, "nothing to rebuild a tree from"
    assert units == [{"element": None, "text": "Some readable prose.", "shells": []}]


def test_noise_nodes_are_skipped_and_comments_ignored() -> None:
    """`script`/`style` content is not article prose, and comments are not nodes.

    Translating a `<script>` body would send code to the provider (the same
    defect class as the code block, but with a worse payload), and a comment is
    not an element to be rebuilt.
    """
    from plugins.processors.translate.processor import _split_blocks

    _body, units = _split_blocks(
        prose="x",
        markup=(
            "<!-- a comment --><script>evil()</script><style>.a{}</style>"
            "<p>Real prose here.</p>"
        ),
    )
    texts = [unit["text"] for unit in units if unit["text"]]
    assert texts == ["Real prose here."], texts
    assert "evil()" not in " ".join(texts)


def test_inline_code_is_not_given_a_shell() -> None:
    """`code` inside a paragraph keeps its text and loses its tag.

    Deliberate: `code` is in the preserved set, so it is never a *marker*
    either. The sentence still reads correctly (the identifier is part of the
    prose), and the alternative -- a shell whose content the provider is told
    not to translate -- would need a second marker kind for no reader benefit.
    """
    from plugins.processors.translate.processor import _split_blocks

    _body, units = _split_blocks(prose="x", markup="<p>Use <code>foo()</code> now.</p>")
    assert units[0]["text"] == "Use foo() now."
    assert units[0]["shells"] == [], "an inline code span gets no marker shell"


def test_a_marker_the_provider_lost_is_dropped_not_guessed() -> None:
    """The promise: prose survives, the tag may not.

    Four ways a provider can mangle a marker -- dropped, unbalanced, an index
    that does not exist, and mismatched open/close pairs. In every case the
    *text* must come through and no `[[n]]` may reach the reader; a guessed tag
    around the wrong words would be worse than a missing tag.
    """
    from plugins.processors.translate.processor import _restore_inline

    shells = [("<a href='/x'>", "</a>")]
    assert _restore_inline("plain text", shells) == "plain text"
    assert _restore_inline("see [[0]]link", shells) == "see link"
    assert _restore_inline("see [[5]]x[[/5]]", shells) == "see x"
    assert _restore_inline("see [[0]]x[[/1]]", [*shells, ("<b>", "</b>")]) == "see x"
    # A well-formed marker is restored, which is the case all the above are the
    # *degradation* of.
    assert _restore_inline("see [[0]]link[[/0]]", shells) == "see <a href='/x'>link</a>"


def test_a_nested_inline_pair_is_restored_once_per_pass() -> None:
    """Nested inline markup restores inner-out across passes.

    `strong` inside `a` is two markers whose spans nest, so a single pass can
    only satisfy the inner one; the outer pass then wraps the result. Pinned
    because a single-pass implementation would silently leave the outer tag
    off, which reads as "the link is gone" in the product.
    """
    from plugins.processors.translate.processor import _restore_inline

    shells = [("<a href='/x'>", "</a>"), ("<strong>", "</strong>")]
    restored = _restore_inline("see [[0]]the [[1]]docs[[/1]][[/0]] now", shells)
    assert restored == "see <a href='/x'>the <strong>docs</strong></a> now"


def test_absolutise_leaves_markup_it_cannot_parse_alone() -> None:
    """A link that stays relative is cosmetic; a dropped product is not.

    The absolutiser is best-effort by design, so its failure mode is stated:
    return the markup untouched (tested through the public path below, since an
    HTML fragment reaching it always parses).
    """
    from plugins.processors.translate.processor import _absolutise

    assert _absolutise("", base="https://h.example/p") == ""
    # `a` without href and an element with no URL attribute are left as-is.
    assert (
        _absolutise("<p>no links <span>x</span></p>", base="https://h.example/p")
        == "<p>no links <span>x</span></p>"
    )


# --------------------------------------------------------------------------
# AC-24: the provider's reply is untrusted markup
#
# The product used to be built by escaping text into fresh `<p>` tags, so a
# hostile reply could only ever be text. Now the reply's *text* is assembled
# into markup, which means a reply containing `</p><script>` closes the block
# early and injects whatever follows. The store-time allowlist (ADR 0007) has
# to apply here for the same reason it applies to a feed body: the boundary
# belongs where untrusted input becomes stored content.
# --------------------------------------------------------------------------


def test_a_hostile_provider_reply_cannot_inject_markup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-24: a reply that closes the tag early is neutralised, not published.

    The provider is a remote service reached over the network; its output is
    exactly as untrusted as a blog page. Without the boundary, the reply below
    stores a live `<script>` (and an `onerror` handler on an injected image)
    into an artifact that every reader then serves.
    """
    from plugins.processors.translate.processor import _reset_config_cache

    _reset_config_cache()
    monkeypatch.setenv("PEARSS_TRANSLATE_BASE_URL", "https://llm.example/v1")
    monkeypatch.setenv("PEARSS_TRANSLATE_API_KEY", "k")
    monkeypatch.setenv("PEARSS_TRANSLATE_MODEL", "m")

    hostile = (
        "</p><script>steal(document.cookie)</script>"
        '<img src="x" onerror="steal()">'
        '<p onclick="steal()">text'
    )
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **kw: httpx.Response(
            200, json={"choices": [{"message": {"content": hostile}}]}
        ),
    )
    stored: list[str] = []

    def fake_store(content: str) -> str:
        stored.append(content)
        return "cas://product"

    process(
        {
            "item_id": "i",
            "title": "T",
            "content": "<p>Long enough prose here.</p>",
            "url": "https://b.example/p",
        },
        target_lang="zh",
        store=fake_store,
    )
    product = stored[0]

    assert "<script" not in product, product
    assert "onclick" not in product, product
    assert "onerror" not in product, product
    assert "steal" not in product, product
    # The text is what the reader gets, inside the block's own tag.
    assert "text" in product


def test_the_product_carries_no_parsing_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-24 (the other half): sanitising must not change the product's shape.

    The wrapper exists to make a fragment parse as a document; it is not
    content and must not reach the reader, or every translated item would gain
    an element (and a nesting level) the original never had.
    """
    product, _ = _translate(monkeypatch, RICH_BODY)

    assert not product.startswith("<div>"), product[:80]
    assert product.count("<div") == 0, product[:200]
    # And the sanitised product still keeps the structure it was built with.
    for tag in ("<h2", "<pre", "<img", "<a "):
        assert tag in product, product[:200]


def test_sanitising_preserves_the_absolute_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-24: order matters -- absolutise, then sanitise, and the URL survives.

    The allowlist permits `href`/`src` and restricts their schemes, so an
    absolutised `http`/`https` URL passes through. If sanitising stripped them,
    AC-21's guarantee would be silently undone by the boundary that protects
    it.
    """
    product, _ = _translate(monkeypatch, RICH_BODY, url="https://blog.example/post")

    assert 'src="https://blog.example/img/ebpf.png"' in product
    assert 'href="https://blog.example/docs/start"' in product


def test_a_comment_inside_a_block_keeps_the_text_around_it() -> None:
    """A comment is not content, but the text next to it is.

    `<p>a<!-- c -->b</p>` is one paragraph reading "ab". Dropping the comment is
    the point; dropping the text *after* it (the comment's tail) would silently
    eat part of the sentence, which is the kind of loss that shows up as a
    truncated translation rather than as a missing tag.
    """
    from plugins.processors.translate.processor import _split_blocks

    _body, units = _split_blocks(prose="ab", markup="<p>a<!-- a comment -->b</p>")
    assert len(units) == 1
    assert units[0]["text"] == "ab", (
        f"the text either side of a comment must survive: {units[0]['text']!r}"
    )


def test_sanitising_an_empty_product_yields_an_empty_artifact() -> None:
    """A product that sanitises to nothing is an empty artifact, not a `<div>`.

    Reachable through the provider, not just defensively: a reply that is
    entirely markup the allowlist drops sanitises away, and the fallback must
    be the empty-artifact SUCCESS the title-only path already uses -- an item
    whose chain cannot produce content must not hold its gate shut forever.
    """
    from plugins.processors.translate.processor import _sanitise

    assert _sanitise("") == ""
    assert _sanitise("<script>evil()</script>") == ""
    assert _sanitise("<p>kept</p>") == "<p>kept</p>"


# --------------------------------------------------------------------------
# The model fallback chain (issue #145)
#
# Cross-module register (normative source: SCENARIOS.md, owned by issue #145):
#
#   AC-133 model #0 回空正文时由下一个 model 完成翻译
#   AC-134 model #0 回 401 时链不中断
#   AC-135 model #0 回地域 400 时链不中断
#   AC-136 model #0 的 200 回复不是 JSON 时同样回退
#   AC-137 全部 model 失败且都非瞬时 → parse, detail 汇总每个 model 的原因
#   AC-138 全部 model 失败但至少一个瞬时 → 取最后一个瞬时失败的分类
#   AC-139 单 model 回空正文仍是可见失败 (AC-22 不变, 钉住现状)
#   AC-140 此前零覆盖的 429/5xx 与「连不上」回退 (钉住现状)
#
# The chain is the provider client's, not the item's: a model that says no for
# any reason (401, a region-blocked 400, an empty completion, a non-JSON body)
# is a reason to ask the next model, not to fail the item. Only when every
# model has failed does the item fail.
# --------------------------------------------------------------------------


def _chain_provider(
    monkeypatch: pytest.MonkeyPatch,
    responses: dict[str, httpx.Response | Exception],
    *,
    models: tuple[str, ...] = ("m1", "m2"),
) -> list[str]:
    """Configure a model chain and answer each request by its `model` field.

    Returns the model ids in the order they were asked. `responses` maps a
    model id to the response it should give (or an exception it should raise);
    a model id in `models` but not in `responses` answers 200 with a canned
    translation.
    """
    from plugins.processors.translate.processor import _reset_config_cache

    _reset_config_cache()
    monkeypatch.setenv("PEARSS_TRANSLATE_BASE_URL", "https://llm.example/v1")
    monkeypatch.setenv("PEARSS_TRANSLATE_API_KEY", "k")
    monkeypatch.setenv("PEARSS_TRANSLATE_MODELS", ",".join(models))
    monkeypatch.delenv("PEARSS_TRANSLATE_MODEL", raising=False)
    asked: list[str] = []

    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        body = kwargs.get("json")
        model = str(body["model"]) if isinstance(body, dict) else ""
        asked.append(model)
        answer = responses.get(model)
        if isinstance(answer, Exception):
            raise answer
        if answer is not None:
            return answer
        return httpx.Response(200, json={"choices": [{"message": {"content": "译文"}}]})

    monkeypatch.setattr(httpx, "post", fake_post)
    return asked


_ENGLISH_BODY = "<p>This is an English sentence long enough to translate.</p>"


def _translate_body(
    monkeypatch: pytest.MonkeyPatch, body: str
) -> tuple[str | None, list[str]]:
    """Translate one body with a chain already configured.

    Returns (product or None when process raised, stored products). The caller
    configures the chain with `_chain_provider` first.
    """
    stored: list[str] = []

    def fake_store(content: str) -> str:
        stored.append(content)
        return "cas://product"

    try:
        process(
            {
                "item_id": "i-145",
                "title": "T",
                "content": body,
                "url": "https://b.example/p",
            },
            target_lang="zh",
            store=fake_store,
        )
    except ProcessorError:
        return None, stored
    return (stored[0] if stored else None), stored


def test_a_model_that_answers_with_no_content_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-133: an empty completion is the next model's turn, not the item's end.

    Empty content is exactly the failure most likely to be provider-side and
    transient: measured live, three such rows all succeeded on retry in 1.0s.
    Treating it as terminal is what left `hy3` never called.
    """
    asked = _chain_provider(
        monkeypatch,
        {"m1": httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})},
    )
    product, stored = _translate_body(monkeypatch, _ENGLISH_BODY)

    assert asked == ["m1", "m2"], "the chain must continue past an empty reply"
    assert product is not None, "the second model should have completed the item"
    assert "译文" in product
    assert stored and stored[0] == product


def test_a_rejected_key_does_not_end_the_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-134: a 401 on model #0 is not a 401 on the item.

    The acceptance cost, recorded in issue #145: a mis-keyed model no longer
    surfaces immediately; it surfaces only when every model fails and the
    aggregate classifies as `auth`.
    """
    asked = _chain_provider(
        monkeypatch,
        {"m1": httpx.Response(401, json={"error": {"message": "invalid api key"}})},
    )
    product, _ = _translate_body(monkeypatch, _ENGLISH_BODY)

    assert asked == ["m1", "m2"], "401 must fall back like any other refusal"
    assert product is not None and "译文" in product


def test_a_region_blocked_request_still_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-135: the live failure was a 400, and it must not stop the chain.

    `User location is not supported for the API use.` is per-request and
    region-dependent, not a defect of the text being translated; the fallback
    chain is precisely what makes such an outage survivable.
    """
    asked = _chain_provider(
        monkeypatch,
        {
            "m1": httpx.Response(
                400,
                json={
                    "error": {
                        "message": ("User location is not supported for the API use.")
                    }
                },
            )
        },
    )
    product, _ = _translate_body(monkeypatch, _ENGLISH_BODY)

    assert asked == ["m1", "m2"]
    assert product is not None and "译文" in product


def test_a_non_json_200_also_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-136: a 200 whose body is not JSON is a per-model failure too."""
    asked = _chain_provider(
        monkeypatch, {"m1": httpx.Response(200, text="<html>relay error</html>")}
    )
    product, _ = _translate_body(monkeypatch, _ENGLISH_BODY)

    assert asked == ["m1", "m2"]
    assert product is not None and "译文" in product


def test_all_models_failing_non_transiently_is_parse_with_every_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-137: the item fails only when the whole chain did, and the detail says so.

    Aggregating every model's reason is the point: the operator sees which
    models were tried and what each said, rather than only the last one.
    """
    _chain_provider(
        monkeypatch,
        {
            "m1": httpx.Response(400, json={"error": {"message": "bad model"}}),
            "m2": httpx.Response(200, json={"choices": [{"message": {"content": ""}}]}),
        },
    )
    with pytest.raises(ProcessorError) as excinfo:
        _run_chain(monkeypatch)

    exc = excinfo.value
    assert exc.error_class is ErrorClass.PARSE
    assert "m1:" in exc.detail, exc.detail
    assert "m2:" in exc.detail, exc.detail
    assert "bad model" in exc.detail, exc.detail
    assert "empty completion content" in exc.detail, exc.detail


def test_all_models_failing_with_a_transient_takes_the_last_transient_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-138: a rate limit anywhere in the chain keeps the row retryable.

    If the last failure alone decided, a 429 followed by a 400 would classify
    `parse` and the worker would never retry a row that only needed waiting.
    """
    _chain_provider(
        monkeypatch,
        {
            "m1": httpx.Response(429, json={"error": {"message": "rate limited"}}),
            "m2": httpx.Response(200, json={"choices": [{"message": {"content": ""}}]}),
        },
    )
    with pytest.raises(ProcessorError) as excinfo:
        _run_chain(monkeypatch)

    assert excinfo.value.error_class is ErrorClass.BLOCKED


def test_a_single_model_answering_nothing_is_still_a_visible_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-139: AC-22 is unchanged -- an empty reply is a parse failure, not silence.

    Already correct before this change; pinned so the new loop cannot turn the
    single-model case into an empty artifact that opens the gate on nothing.
    """
    _chain_provider(
        monkeypatch,
        {"m1": httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})},
        models=("m1",),
    )
    with pytest.raises(ProcessorError) as excinfo:
        _run_chain(monkeypatch)

    assert excinfo.value.error_class is ErrorClass.PARSE


def test_the_previously_uncovered_transient_paths_fall_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-140: 429/5xx and an unreachable model were already handled; pinned.

    Zero tests set a model chain before this issue, so these branches had no
    test at all. They were correct; this holds them while the loop is rewritten.
    The 501 is the default for any status outside the named set: it classifies
    `network`, so an unlisted provider answer stays retryable rather than
    silently permanent.
    """
    for first in (
        httpx.Response(429, json={"error": {"message": "rate limited"}}),
        httpx.Response(503, json={"error": {"message": "overloaded"}}),
        httpx.Response(501, json={"error": {"message": "not implemented"}}),
        httpx.ConnectError("connection refused"),
    ):
        asked = _chain_provider(monkeypatch, {"m1": first})
        product, _ = _translate_body(monkeypatch, _ENGLISH_BODY)
        assert asked == ["m1", "m2"], f"{first!r} should fall back"
        assert product is not None and "译文" in product


def _run_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Translate one English body, letting a ProcessorError propagate."""
    process(
        {
            "item_id": "i-145",
            "title": "T",
            "content": _ENGLISH_BODY,
            "url": "https://b.example/p",
        },
        target_lang="zh",
        store=lambda content: "cas://product",
    )


# --------------------------------------------------------------------------
# Void elements inside a block (issue #150)
#
# Cross-module register (normative source: SCENARIOS.md, owned by issue #150):
#
#   AC-141 块内 <br> 在译文里保留 (p)
#   AC-142 li / blockquote / td 内的 <br> 同样保留
#   AC-143 块内 <wbr> 保留
#   AC-144 块内 <img> 不被删除: src 绝对化, alt 不变
#   AC-145 被引推文 blockquote 内的 <a><img></a> 图片保留, 不出现空 <a></a>
#   AC-146 模型丢掉 void 标记时降级安全 (正文完整, 无字面 [[n]])
#   AC-147 顶层 <br> 不受影响 (钉住现状)
#
# `_flatten_inline` only had two branches: a paired marker for `_INLINE`, and
# "keep the text, drop the tag" for everything else. A void element has no
# text, so `br`/`wbr`/`img`/`hr` fell into the second branch and vanished --
# a line break welded two lines into one, and a block-internal image was
# deleted outright. These pin the third branch: a one-sided marker whose shell
# is the element itself.
# --------------------------------------------------------------------------


def test_a_line_break_inside_a_paragraph_survives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-141: the X shape -- `\n` became `<br/>` inside one `<p>` -- survives.

    Measured on the deployed code: the break was gone from the product, so a
    multi-line tweet read as one welded line. This is the collector's own
    output shape (`collector.py` turns `\n` into `<br/>`), not a corner case.
    """
    product, _ = _translate(monkeypatch, "<p>line1<br/>line2</p>")

    assert "<br" in product, product
    assert "line1line2" not in product, f"the lines were welded: {product}"


@pytest.mark.parametrize(
    "markup",
    [
        "<ul><li>line1<br/>line2</li></ul>",
        "<blockquote>line1<br/>line2</blockquote>",
        "<table><tr><td>line1<br/>line2</td></tr></table>",
    ],
    ids=["li", "blockquote", "td"],
)
def test_a_line_break_inside_a_list_quote_or_cell_survives(
    monkeypatch: pytest.MonkeyPatch, markup: str
) -> None:
    """AC-142: the break is kept wherever the block sits, not only in `p`."""
    product, _ = _translate(monkeypatch, markup)

    assert "<br" in product, product
    assert "line1line2" not in product, f"the two lines were welded: {product}"


def test_a_word_break_opportunity_survives(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-143: `wbr` carries no text either, and is preserved for the same reason."""
    product, _ = _translate(monkeypatch, "<p>very<wbr/>longword</p>")

    assert "<wbr" in product, product
    assert "very" in product and "longword" in product, product


def test_an_image_inside_a_paragraph_is_not_deleted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-144: a block-internal image survives, absolute and with its alt.

    The old walk deleted it: `img` is in `_PRESERVED`, but that protection
    only applied at top level, so an image inside a `<p>` was flattened away
    as "a tag not worth a shell". A reader lost the picture with no trace.
    """
    product, _ = _translate(
        monkeypatch,
        '<p>see <img src="/i.png" alt="pic"/> here</p>',
        url="https://blog.example/post",
    )

    assert "<img" in product, product
    assert 'alt="pic"' in product, product
    assert 'src="https://blog.example/i.png"' in product, product


def test_a_quoted_tweets_image_survives(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-145: the shape `_quoted_html` produces -- `<a><img></a>` in a quote.

    Measured on the deployed code, the product kept an empty `<a>` where the
    image had been: the anchor got its marker, the void image inside it did
    not, and the image was gone while its link stayed.
    """
    body = (
        "<blockquote>Quoted Guy · @qg a quoted preview text "
        '<a href="https://pbs.twimg.com/media/abc.jpg">'
        '<img src="https://pbs.twimg.com/media/abc.jpg" alt="photo"/></a> '
        '<a href="https://x.com/i/status/1">查看被引推文</a></blockquote>'
    )
    product, _ = _translate(monkeypatch, body)

    assert "<img" in product, product
    assert "abc.jpg" in product, product
    assert '<a href="https://pbs.twimg.com/media/abc.jpg"></a>' not in product, (
        f"the image is gone but its link remains: {product}"
    )


def test_a_dropped_void_marker_degrades_safely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-146: a provider that drops `[[n]]` loses the break, never the text.

    Already correct before this change (the marker did not exist); pinned so
    the new one-sided marker cannot leak as literal `[[n]]` or corrupt the
    block when the model omits it.
    """
    from plugins.processors.translate.processor import _reset_config_cache

    _reset_config_cache()
    monkeypatch.setenv("PEARSS_TRANSLATE_BASE_URL", "https://llm.example/v1")
    monkeypatch.setenv("PEARSS_TRANSLATE_API_KEY", "k")
    monkeypatch.setenv("PEARSS_TRANSLATE_MODEL", "m")
    monkeypatch.delenv("PEARSS_TRANSLATE_MODELS", raising=False)
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **kw: httpx.Response(
            200, json={"choices": [{"message": {"content": "line1line2"}}]}
        ),
    )
    stored: list[str] = []

    def fake_store(content: str) -> str:
        stored.append(content)
        return "cas://product"

    process(
        {
            "item_id": "i-150",
            "title": "T",
            "content": "<p>line1<br/>line2</p>",
            "url": "https://b.example/p",
        },
        target_lang="zh",
        store=fake_store,
    )
    product = stored[0]

    assert "<br" not in product, product
    assert "line1line2" in product, f"the text must survive: {product}"
    assert "[[" not in product, f"a literal marker leaked: {product}"


def test_a_top_level_break_is_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-147: a `<br>` *between* blocks was never affected; pinned as-is.

    It is not inside any unit, so it stays in the tree and is serialized by
    the rebuild untouched. The fix must not start treating it as a unit.
    """
    product, seen = _translate(monkeypatch, "<p>a</p><br/><p>b</p>")

    assert "<br" in product, product
    assert product.count("<p>") == 2, product
    assert len(seen) == 2, "the top-level break is not a translation unit"


# --------------------------------------------------------------------------
# The segment boundary of an oversized block (issue #151)
#
# Cross-module register (normative source: SCENARIOS.md, owned by issue #151):
#
#   AC-148 超过 MAX_CHARS 的块的段间分隔不丢
#   AC-149 不需要切分的块产物逐字节不变 (钉住现状)
#
# `_segment` splits a block on `\n\n`, consuming the separator; `_fill_unit`
# then joined the per-segment replies with `""`, so the boundary disappeared
# and the two halves welded together (`AAAA...BBBB` with no whitespace).
# --------------------------------------------------------------------------


def _echo_provider(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """A provider that returns each request's text verbatim, recording inputs.

    Verbatim (not the `[译]`-prefixed echo the other helpers use) so a test can
    read the segment boundary straight out of the product.
    """
    from plugins.processors.translate.processor import _reset_config_cache

    _reset_config_cache()
    monkeypatch.setenv("PEARSS_TRANSLATE_BASE_URL", "https://llm.example/v1")
    monkeypatch.setenv("PEARSS_TRANSLATE_API_KEY", "k")
    monkeypatch.setenv("PEARSS_TRANSLATE_MODEL", "m")
    monkeypatch.delenv("PEARSS_TRANSLATE_MODELS", raising=False)
    seen: list[str] = []

    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        body = kwargs.get("json")
        user = body["messages"][1]["content"] if isinstance(body, dict) else ""
        seen.append(str(user))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": str(user)}}]}
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    return seen


def test_a_paragraph_boundary_in_a_segmented_block_survives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-148: a block over MAX_CHARS keeps its paragraph boundary.

    Measured on the deployed code: `_segment` split `A*15000\n\nB*15000` into
    two requests, the provider echoed both, and the product was
    `A*15000B*15000` -- the separator was consumed by the split and the replies
    were joined with `""`, so the two halves welded into one run.
    """
    seen = _echo_provider(monkeypatch)
    left = "A" * 15000
    right = "B" * 15000
    stored: list[str] = []

    def fake_store(content: str) -> str:
        stored.append(content)
        return "cas://product"

    process(
        {
            "item_id": "i-151",
            "title": "T",
            "content": f"<p>{left}\n\n{right}</p>",
            "url": "https://b.example/p",
        },
        target_lang="zh",
        store=fake_store,
    )
    product = stored[0]

    assert len(seen) == 2, "the oversized block must have been split in two"
    assert f"{left}\n\n{right}" in product, (
        f"the paragraph boundary was dropped: {product[:80]!r}..."
    )
    assert f"{left}{right}" not in product, "the two segments were welded together"


def test_a_block_that_is_not_split_is_byte_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-149: the single-segment path is untouched (pinned).

    A short block produces exactly one reply, so the join is a no-op on it;
    this holds that a fix for the multi-reply case cannot leak into the common
    one-segment path.
    """
    seen = _echo_provider(monkeypatch)
    stored: list[str] = []

    def fake_store(content: str) -> str:
        stored.append(content)
        return "cas://product"

    process(
        {
            "item_id": "i-151b",
            "title": "T",
            "content": "<p>Short English prose here.</p>",
            "url": "https://b.example/p",
        },
        target_lang="zh",
        store=fake_store,
    )

    assert len(seen) == 1, "a short block is one request"
    assert stored[0] == "<p>Short English prose here.</p>", stored[0]


# --------------------------------------------------------------------------
# The row's remaining budget (AC-221, AC-222)
# --------------------------------------------------------------------------


def test_a_single_attempt_never_spends_more_than_half_the_remaining_budget() -> None:
    """AC-222: one attempt gets min(60s, remaining/2), or no attempt at all.

    An attempt allowed to spend what is left is an attempt the parent kills at
    the row's deadline, and a killed child loses every model's reason. Halving
    also leaves the next model in the chain a share of what remains instead of
    a budget already spent by the first (issue #227).
    """
    assert attempt_timeout(4_000.0) == MAX_ATTEMPT_SECONDS
    assert attempt_timeout(200.0) == MAX_ATTEMPT_SECONDS
    assert attempt_timeout(10.0) == 5.0
    assert attempt_timeout(2.0) == 1.0
    # Below the floor nothing can come back in time, so the attempt is not
    # started at all: starting it only races the parent's kill.
    assert attempt_timeout(1.0) is None
    assert attempt_timeout(0.2) is None


def test_a_spent_budget_fails_the_row_without_asking_any_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-221: a budget already gone is reported, not spent on a doomed call."""
    from plugins.processors.translate.processor import _reset_config_cache

    _reset_config_cache()
    monkeypatch.setenv("PEARSS_TRANSLATE_BASE_URL", "https://llm.example/v1")
    monkeypatch.setenv("PEARSS_TRANSLATE_API_KEY", "k")
    monkeypatch.setenv("PEARSS_TRANSLATE_MODEL", "m")

    def never(url: str, **kwargs: object) -> httpx.Response:
        raise AssertionError(f"a model was asked with no budget left: {url}")

    monkeypatch.setattr(httpx, "post", never)

    with pytest.raises(ProcessorError) as excinfo:
        process(
            {
                "title": "T",
                "content": "<p>An English article.</p>",
                "deadline": time.monotonic() - 1.0,
            },
            target_lang="zh",
            store=lambda content: "cas://product",
        )

    assert excinfo.value.error_class is ErrorClass.NETWORK
    assert "budget" in excinfo.value.detail


class _Clock:
    """A monotonic clock the test advances, so nothing waits in real time.

    The processor reads `time.monotonic()` through one module attribute, so a
    stalled attempt can be simulated by advancing the clock by exactly the
    budget that attempt was given. The chain's arithmetic then holds still
    under test instead of depending on how busy the machine is (issue #213).
    """

    def __init__(self, start: float) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _stalling_provider(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock, models: str
) -> list[httpx.Timeout]:
    """A provider that burns each attempt's whole budget, and its fallback.

    Returns the timeouts the processor handed to the HTTP boundary, in order.
    """
    from plugins.processors.translate.processor import _reset_config_cache

    _reset_config_cache()
    monkeypatch.setenv("PEARSS_TRANSLATE_BASE_URL", "https://llm.example/v1")
    monkeypatch.setenv("PEARSS_TRANSLATE_API_KEY", "k")
    monkeypatch.setenv("PEARSS_TRANSLATE_MODELS", models)
    monkeypatch.delenv("PEARSS_TRANSLATE_MODEL", raising=False)
    monkeypatch.setattr(
        importlib.import_module("plugins.processors.translate.processor"),
        "time",
        clock,
    )
    handed: list[httpx.Timeout] = []

    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        timeout = kwargs["timeout"]
        assert isinstance(timeout, httpx.Timeout)
        handed.append(timeout)
        clock.advance(timeout.read or 0.0)
        raise httpx.ReadTimeout("provider never replied")

    monkeypatch.setattr(httpx, "post", fake_post)
    return handed


def test_a_stalled_model_is_abandoned_inside_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-221/AC-222: the attempt is bounded by the budget, and its reason survives.

    The chain reports what it collected instead of being killed: with 2s of row
    budget the first model gets 1s (half of what is left, and under the 60s
    cap), and once that is spent the second model is not asked at all -- asking
    it is what would end in the parent's kill with nothing to show. This is the
    in-process half of the contract; the same expectations through the real
    child live in `tests/integration/test_processor_deadline.py`, which the
    parent's coverage cannot see because the child is a separate process.
    """
    clock = _Clock(start=1000.0)
    handed = _stalling_provider(monkeypatch, clock, models="primary,fallback")

    with pytest.raises(ProcessorError) as excinfo:
        process(
            {
                "title": "T",
                "content": "<p>An English article.</p>",
                "deadline": clock.monotonic() + 2.0,
            },
            target_lang="zh",
            store=lambda content: "cas://product",
        )

    assert excinfo.value.error_class is ErrorClass.NETWORK
    assert [timeout.read for timeout in handed] == [1.0], "half the row budget"
    assert excinfo.value.detail.startswith("primary: "), excinfo.value.detail
    assert "unreachable" in excinfo.value.detail, excinfo.value.detail


# --------------------------------------------------------------------------
# The row's model policy and its per-model report (AC-225..228)
#
# The health itself lives in the worker (`app/model_health`): this processor
# runs in a child spawned per row, so it cannot outlive one. What crosses the
# boundary is data -- a policy in, a report out.
# --------------------------------------------------------------------------


def _model_provider(
    monkeypatch: pytest.MonkeyPatch, answers: dict[str, tuple[int, dict[str, object]]]
) -> list[str]:
    """A provider that records the model each call names, answering per model."""
    from plugins.processors.translate.processor import _reset_config_cache

    _reset_config_cache()
    monkeypatch.setenv("PEARSS_TRANSLATE_BASE_URL", "https://llm.example/v1")
    monkeypatch.setenv("PEARSS_TRANSLATE_API_KEY", "k")
    monkeypatch.setenv("PEARSS_TRANSLATE_MODELS", "primary,fallback")
    monkeypatch.delenv("PEARSS_TRANSLATE_MODEL", raising=False)
    asked: list[str] = []

    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        request = httpx.Request("POST", url, json=kwargs.get("json"))
        model = json.loads(request.read())["model"]
        asked.append(model)
        status, payload = answers.get(
            model, (200, {"choices": [{"message": {"content": f"[{model}]"}}]})
        )
        return httpx.Response(status, json=payload)

    monkeypatch.setattr(httpx, "post", fake_post)
    return asked


def _ctx(content: str, **context: object) -> dict[str, object]:
    return {
        "item_id": "i-228",
        "title": "T",
        "content": content,
        "url": "https://b.example/p",
        **context,
    }


THREE_BLOCKS = (
    "<p>English prose one.</p><p>English prose two.</p><p>English prose three.</p>"
)


def test_a_cooling_model_is_not_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-227: the row's policy decides which models are candidates at all."""
    asked = _model_provider(monkeypatch, {})
    events: list[tuple[str, str | None]] = []

    process(
        _ctx(
            "<p>English prose.</p>",
            model_policy={"skip": ["primary"], "attempts_per_model": 2},
            model_events=events,
        ),
        target_lang="zh",
        store=lambda content: "cas://product",
    )

    assert asked == ["fallback"]
    assert events == [("fallback", None)]


def test_a_wholly_cooling_chain_asks_nothing_and_fails_transiently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-230: no candidate left is local backpressure, not a provider verdict."""
    asked = _model_provider(monkeypatch, {})
    events: list[tuple[str, str | None]] = []

    with pytest.raises(ProcessorError) as excinfo:
        process(
            _ctx(
                "<p>English prose.</p>",
                model_policy={
                    "skip": ["primary", "fallback"],
                    "attempts_per_model": 2,
                },
                model_events=events,
            ),
            target_lang="zh",
            store=lambda content: "cas://product",
        )

    assert excinfo.value.error_class is ErrorClass.NETWORK
    assert "cooling" in excinfo.value.detail, excinfo.value.detail
    assert asked == [], "a cooling chain must not be asked again"
    assert events == [], "nothing was asked, so nothing is reported"


def test_a_broken_model_is_retried_at_most_twice_per_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-226: the budget counts rows, not the blocks inside one."""
    asked = _model_provider(
        monkeypatch, {"primary": (503, {"error": {"message": "overloaded"}})}
    )

    process(
        _ctx(
            THREE_BLOCKS,
            model_policy={"skip": [], "attempts_per_model": 2},
            model_events=[],
        ),
        target_lang="zh",
        store=lambda content: "cas://product",
    )

    assert asked.count("primary") == 2, asked
    assert asked.count("fallback") == 3, asked


def test_a_permanent_rejection_does_not_spend_the_retry_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-229: a 401 demotes the model at once; asking again is pure waste."""
    asked = _model_provider(
        monkeypatch, {"primary": (401, {"error": {"message": "invalid api key"}})}
    )

    process(
        _ctx(
            THREE_BLOCKS,
            model_policy={"skip": [], "attempts_per_model": 2},
            model_events=[],
        ),
        target_lang="zh",
        store=lambda content: "cas://product",
    )

    assert asked.count("primary") == 1, asked
    assert asked.count("fallback") == 3, asked


def test_the_report_names_the_model_that_failed_and_the_one_that_answered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-225, AC-228: the worker's registry acts on this report, nothing else."""
    _model_provider(
        monkeypatch, {"primary": (503, {"error": {"message": "overloaded"}})}
    )
    events: list[tuple[str, str | None]] = []

    process(
        _ctx(
            "<p>English one.</p><p>English two.</p>",
            model_policy={"skip": [], "attempts_per_model": 2},
            model_events=events,
        ),
        target_lang="zh",
        store=lambda content: "cas://product",
    )

    assert events == [("primary", "network"), ("fallback", None)]


def test_each_block_announces_itself_on_the_progress_logger(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """AC-232: the block counter is what a killed row is diagnosed from.

    A killed child never reports its aggregate, so the host keeps the child's
    last log lines instead; this is the line that says where the chain had got
    to (ADR 0050).
    """
    _model_provider(monkeypatch, {})

    with caplog.at_level(logging.INFO, logger="pearss.progress"):
        process(
            _ctx(
                THREE_BLOCKS,
                model_policy={"skip": [], "attempts_per_model": 2},
                model_events=[],
            ),
            target_lang="zh",
            store=lambda content: "cas://product",
        )

    assert [
        record.getMessage()
        for record in caplog.records
        if record.name == "pearss.progress"
    ] == ["chunk 1/3", "chunk 2/3", "chunk 3/3"]


def test_the_per_attempt_ceiling_comes_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider that legitimately takes minutes must not be cut at 60s.

    The default cap is 60s -- one slow model must not own a six-minute row --
    and the case it would otherwise break is real: a relayed gemini-flash reply
    was measured at 126s end to end (issue #228, ADR 0047).
    """
    from plugins.processors.translate.processor import _reset_config_cache

    _reset_config_cache()
    monkeypatch.setenv("PEARSS_TRANSLATE_BASE_URL", "https://llm.example/v1")
    monkeypatch.setenv("PEARSS_TRANSLATE_API_KEY", "k")
    monkeypatch.setenv("PEARSS_TRANSLATE_MODEL", "m")
    monkeypatch.setenv("PEARSS_TRANSLATE_ATTEMPT_SECONDS", "5")
    handed: list[httpx.Timeout] = []

    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        timeout = kwargs["timeout"]
        assert isinstance(timeout, httpx.Timeout)
        handed.append(timeout)
        return httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]})

    monkeypatch.setattr(httpx, "post", fake_post)

    process(
        _ctx("<p>English prose.</p>", deadline=time.monotonic() + 4000.0),
        target_lang="zh",
        store=lambda content: "cas://product",
    )

    assert [timeout.read for timeout in handed] == [5.0]


def _provider_with_a_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[str], dict[str, str]]:
    """A provider that answers everything except chunks matching a needle.

    The needle is mutable so one test can make the same document succeed
    partially and then succeed fully: the shape a retry actually sees.
    """
    from plugins.processors.translate.processor import _reset_config_cache

    _reset_config_cache()
    monkeypatch.setenv("PEARSS_TRANSLATE_BASE_URL", "https://llm.example/v1")
    monkeypatch.setenv("PEARSS_TRANSLATE_API_KEY", "k")
    monkeypatch.setenv("PEARSS_TRANSLATE_MODEL", "m")
    monkeypatch.delenv("PEARSS_TRANSLATE_MODELS", raising=False)
    asked: list[str] = []
    state = {"fail_on": ""}

    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        request = httpx.Request("POST", url, json=kwargs.get("json"))
        text = json.loads(request.read())["messages"][1]["content"]
        asked.append(text)
        if state["fail_on"] and state["fail_on"] in text:
            return httpx.Response(503, json={"error": {"message": "overloaded"}})
        return httpx.Response(
            200, json={"choices": [{"message": {"content": f"[{text}]"}}]}
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    return asked, state


def test_a_retry_translates_only_the_blocks_that_are_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """AC-234: what a previous attempt translated is not paid for again.

    A document that died with most of its blocks already translated used to
    start from block one on the next attempt -- the same outage, paid for once
    per block. What the cache holds is the *first attempt's* work, so this
    asserts both halves: the missing blocks are asked for, and the result is the
    document a single successful pass would have produced.
    """
    cache = tmp_path / "cache"
    body = "<p>prose one.</p><p>prose two.</p><p>prose three.</p>"
    asked, state = _provider_with_a_flag(monkeypatch)
    state["fail_on"] = "prose two"

    stored: list[str] = []
    with pytest.raises(ProcessorError):
        process(
            _ctx(body, cache_dir=str(cache)),
            target_lang="zh",
            store=lambda content: stored.append(content) and "cas://product",
        )
    assert stored == [], "a failed pass stores nothing"
    (cached_block,) = list(cache.glob("*"))
    assert cached_block.read_text(), "the block that did succeed was kept"

    first_attempt = list(asked)
    state["fail_on"] = ""
    del asked[:]

    process(
        _ctx(body, cache_dir=str(cache)),
        target_lang="zh",
        store=lambda content: stored.append(content) and "cas://product",
    )

    assert first_attempt, "sanity: the first attempt did reach the provider"
    assert any("prose one" in text for text in first_attempt)
    assert not any("prose one" in text for text in asked), (
        f"the retry asked again for a block it already had: {asked}"
    )
    assert any("prose two" in text for text in asked)

    # The product of the retry is the product of one clean pass, byte for byte.
    fresh: list[str] = []
    process(
        _ctx(body, cache_dir=str(tmp_path / "fresh")),
        target_lang="zh",
        store=lambda content: fresh.append(content) and "cas://product",
    )
    assert stored == fresh
