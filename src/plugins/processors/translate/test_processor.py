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
    monkeypatch.delenv("PEARSS_TRANSLATE_BASE_URL", raising=False)
    with pytest.raises(ProcessorError) as excinfo:
        process({"title": "t", "content": "c"}, target_lang="zh", store=lambda c: "x")
    assert excinfo.value.error_class is ErrorClass.PARSE


def test_a_gzipped_provider_reply_is_decoded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Fixture realism (2026-09-09): real providers send gzip; a handler that
    forgets the header pair would have hidden the double-decompression bug."""
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
