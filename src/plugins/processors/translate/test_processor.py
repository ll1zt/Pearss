"""The translate processor: input shaping and the title-only fallback."""

from __future__ import annotations

import gzip
import json
import pathlib

import httpx
import pytest

from core.enums import ErrorClass
from core.plugins.hooks import ProcessorError
from plugins.processors.translate.processor import (
    MAX_CHARS,
    _segment,
    _strip_to_text,
    process,
    processor_id,
)


def test_processor_id_is_stable() -> None:
    assert processor_id() == "translate"


def test_strip_to_text_removes_markup_and_decodes_entities() -> None:
    assert _strip_to_text("<p>a &amp; b<br>c</p>") == "a & b c"


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
