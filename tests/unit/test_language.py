"""The language verdict: which bodies a translation chain can skip.

The cases here are drawn from real items, because the thresholds are only
meaningful against the shapes that actually arrive. Two of them overturned an
earlier design: a kanji-heavy Japanese sentence read as Chinese under
first-match script ranges, and a Chinese post salted with English technical
terms read as untranslated under a bare-majority threshold.
"""

from __future__ import annotations

import pytest

from core.language import (
    KNOWN_TARGETS,
    MIN_SCRIPTED_CHARS,
    TARGET_SCRIPT_SHARE,
    is_already_in,
)

# Real items, kept verbatim: they are the evidence for where the thresholds sit.
CHINESE_PLAIN = "如果真是“自我提升向内求”，respect。"
CHINESE_TECHNICAL = "pi agent 上面的两个 subagent 包用起来都有痛点"
CHINESE_LONG = (
    "在期待已久的云南之旅中收到了同事们准备的生日惊喜，这绝对是我人生最难忘的一次旅行。"
)
ENGLISH = (
    "JUST IN: Former White House AI Czar says the new rules will land next quarter."
)
JAPANESE_KANA_HEAVY = "これは日本語の文章です。漢字も含まれています。"
JAPANESE_KANJI_HEAVY = "日本語文章漢字多数今日天気図書館勉強会資料作成確認済です。"


@pytest.mark.unit
def test_chinese_is_recognised() -> None:
    assert is_already_in(CHINESE_LONG, "zh") is True
    assert is_already_in(CHINESE_PLAIN, "zh") is True


@pytest.mark.unit
def test_a_chinese_post_salted_with_english_terms_still_counts_as_chinese() -> None:
    """The case that moved the threshold from a bare majority to 0.4.

    Fifteen Latin letters against thirteen ideographs: readable Chinese to a
    Chinese reader, but 46% by script count, so a 0.5 threshold queued it for
    translation. The cost of the old behaviour was a provider call on every
    technical post; the cost of this one is bounded by the English-dominant
    case below, which must keep translating.
    """
    assert is_already_in(CHINESE_TECHNICAL, "zh") is True


@pytest.mark.unit
def test_english_dominant_text_still_translates() -> None:
    """The error this must not make: an English post read as Chinese.

    Without this, lowering the threshold would be indistinguishable from
    removing the check.
    """
    assert is_already_in(ENGLISH, "zh") is False


@pytest.mark.unit
def test_english_text_counts_as_english() -> None:
    assert is_already_in(ENGLISH, "en") is True
    assert is_already_in(CHINESE_LONG, "en") is False


@pytest.mark.unit
def test_japanese_is_not_mistaken_for_chinese() -> None:
    """Kanji sit in the Chinese ranges; kana is what marks Japanese.

    Under first-match ranges with the CJK block listed first, the kana of this
    sentence fell outside every CJK range (counted as "other") while its kanji
    read as Chinese -- 93% by the ratio, so real Japanese was skipped as
    already-Chinese.
    """
    assert is_already_in(JAPANESE_KANJI_HEAVY, "zh") is False
    assert is_already_in(JAPANESE_KANA_HEAVY, "zh") is False
    assert is_already_in(JAPANESE_KANA_HEAVY, "ja") is True


@pytest.mark.unit
def test_an_unknown_target_gets_no_shortcut() -> None:
    """Without knowing a language's script, any answer would be a guess."""
    assert is_already_in("Это довольно длинный русский текст.", "ru") is False
    assert is_already_in(CHINESE_LONG, "ru") is False


@pytest.mark.unit
def test_short_bodies_are_never_skipped() -> None:
    """Under the sample floor a ratio is noise, so the safe direction wins.

    "OK", a date, an emoji: one or two glyphs cannot evidence a language, and
    sending them costs a fraction of a cent while skipping them would leave
    text unread.
    """
    assert is_already_in("好的", "zh") is False
    assert is_already_in("OK", "zh") is False
    assert is_already_in("2026", "zh") is False
    assert is_already_in("", "zh") is False
    # The floor is a count of scripted characters, not of characters.
    assert is_already_in("！？。，", "zh") is False
    below_floor = "很" * (MIN_SCRIPTED_CHARS - 1)
    assert is_already_in(below_floor, "zh") is False
    assert is_already_in("很" * MIN_SCRIPTED_CHARS, "zh") is True


@pytest.mark.unit
def test_the_threshold_sits_where_the_comment_says() -> None:
    """Pin the constant to its documented value.

    It is a product decision recorded in the comment above it, and a silent
    drift would change which posts get translated without any test noticing.
    """
    assert TARGET_SCRIPT_SHARE == 0.4
    assert frozenset({"zh", "ja", "en"}) == KNOWN_TARGETS
