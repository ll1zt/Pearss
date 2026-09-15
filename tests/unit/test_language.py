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
    prose_text,
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


@pytest.mark.unit
def test_the_threshold_is_inclusive_at_the_boundary() -> None:
    """A body exactly at the share is already-in, not translated.

    The comment calls 0.4 "the point where the sampled mixed posts start
    reading as Chinese", so the boundary belongs on the skip side. Written with
    exactly 8 ideographs against 12 Latin letters -- a ratio of exactly 0.4 --
    because any other ratio would pass under a strict `>` too, leaving the
    inclusive comparison untested.
    """
    at_the_boundary = ("很" * 8) + ("a" * 12)
    just_under = ("很" * 7) + ("a" * 13)

    assert is_already_in(at_the_boundary, "zh") is True
    assert is_already_in(just_under, "zh") is False


@pytest.mark.unit
def test_both_ends_of_a_script_range_count_as_inside_it() -> None:
    """Range membership is inclusive at both bounds.

    The ranges are written as `low <= c <= high`; a mutation to a half-open
    comparison drops the first or last code point of every range, and those sit
    at the edges of a block -- real characters, not sentinels. Asserted through
    the public verdict so the range table stays an implementation detail.
    """
    # U+4E00 and U+9FFF are the CJK block's documented endpoints; each is a
    # real ideograph that must be counted, and enough of them must clear the
    # sample floor for the verdict to be about the range and not the floor.
    first, last = "\u4e00", "\u9fff"
    assert is_already_in(first * MIN_SCRIPTED_CHARS, "zh") is True
    assert is_already_in(last * MIN_SCRIPTED_CHARS, "zh") is True

    # One code point outside either end must not be counted as CJK.
    assert is_already_in("\u4dff" * MIN_SCRIPTED_CHARS, "zh") is False
    assert is_already_in("\ua000" * MIN_SCRIPTED_CHARS, "zh") is False


@pytest.mark.unit
def test_numeric_character_references_are_decoded_before_the_verdict() -> None:
    """A CJK body encoded as `&#NNNNN;` runs is still Chinese.

    Feeds do this, and a reader of the raw string sees no Chinese at all --
    so a verdict taken before decoding says "translate me" about a body that
    is entirely Chinese. Both callers (the enqueue-time check and the
    processor) must extract text the same way, or one queues what the other
    skips.
    """
    encoded = "&#" + str(ord("中")) + ";" * 1
    body = ("&#20013;" * 20) + " ok"
    assert prose_text(body).startswith("中" * 20)
    assert is_already_in(prose_text(body), "zh") is True
    # The raw string, undecoded, is ASCII and reads as anything but Chinese.
    assert is_already_in(body, "zh") is False
    assert encoded  # the literal above is what a feed emits


@pytest.mark.unit
def test_prose_text_strips_markup_and_scripts() -> None:
    assert prose_text("<p>hello <b>world</b></p>") == "hello world"
    # A script's source must not survive as "prose" the classifier reads.
    assert (
        prose_text("<script>var x = '中中中中中中中中中中中中中中'</script>ok") == "ok"
    )
    assert prose_text("") == ""
