"""Whether a body is already written in a language, without a detector.

This lives in core because two callers need the same answer at different
times: the pipeline decides at *enqueue* time whether a translation is worth
queueing at all, and the translate processor decides again when it runs. If
those two disagreed, an item could be queued and then skipped -- occupying a
queue slot that only ever produces nothing -- which is exactly the coupling
this exists to remove.

Deliberately not a language *detector*. It answers one yes/no question for the
languages a deployment translates between, and returns False when unsure: an
uncertain answer costs a provider call, while a wrong skip leaves the reader
with text they cannot read. That asymmetry is the whole design.
"""

from __future__ import annotations

from typing import Final

# Unicode script ranges. Characters outside them (punctuation, digits, emoji,
# Cyrillic, Arabic) count as "other" and never decide the outcome on their own.
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
KNOWN_TARGETS: Final[frozenset[str]] = frozenset({"zh", "ja", "en"})

# A body counts as already-target when most of its scripted characters belong
# to that language. 0.5 is deliberately the midpoint: the sampler found real X
# posts like 「如果真是"自我提升向内求"，respect。」, majority-Chinese with a
# Latin tail, and translating those is exactly the waste this avoids. A
# genuinely bilingual post still translates, because there the target script is
# not the majority.
TARGET_SCRIPT_SHARE: Final = 0.5

# Below this many scripted characters a ratio is noise: "OK", "2026" and
# one-word titles would all be judged by one or two glyphs. Short bodies go to
# the provider, which is the safe direction -- an unnecessary call costs a
# fraction of a cent, a skipped one leaves untranslated text.
MIN_SCRIPTED_CHARS: Final = 12


def _in_ranges(codepoint: int, ranges: tuple[tuple[int, int], ...]) -> bool:
    return any(low <= codepoint <= high for low, high in ranges)


def is_already_in(text: str, target_lang: str) -> bool:
    """Whether `text` is predominantly written in `target_lang`.

    See the module docstring for why this answers a narrower question than a
    detector would, and why every unsure answer is False.
    """
    if target_lang not in KNOWN_TARGETS:
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

    if scripted < MIN_SCRIPTED_CHARS:
        return False
    return in_target / scripted >= TARGET_SCRIPT_SHARE


__all__ = [
    "KNOWN_TARGETS",
    "MIN_SCRIPTED_CHARS",
    "TARGET_SCRIPT_SHARE",
    "is_already_in",
]
