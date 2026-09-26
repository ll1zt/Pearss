"""The translate processor: input shaping and the title-only fallback."""

from __future__ import annotations

import gzip
import json
import pathlib

import httpx
import pytest

from core.enums import ErrorClass
from core.language import prose_text
from core.plugins.hooks import ProcessorError
from plugins.processors.translate.processor import (
    MAX_CHARS,
    _segment,
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
