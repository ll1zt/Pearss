"""CJK-aware pre-tokenisation for FTS5 (ADR 0035 decisions 1, 2 and 9).

FTS5's own tokenisers cannot segment CJK: `unicode61` splits on script
boundaries it does not know, and `trigram` needs three characters before it
matches anything (ADR 0035, facts 1 and 2). So the text is tokenised *before*
it is stored and before it is searched, and FTS5 is left to index a plain
space-separated stream.

**The invariant is that both sides call `tokenize()`** -- a document indexed
with one segmentation and queried with another silently matches nothing. That
is the one thing this module cannot enforce by itself, so it is what the tests
exist for.

Why overlapping bigrams rather than a dictionary segmenter: a dictionary
evolves with its version, so an upgrade re-segments the same text, and the
index and the query disagree without anything reporting an error. Bigrams are
deterministic and script-independent, and are what Lucene's `cjk_bigram` filter
does. Replacing `tokenize()` later is allowed -- it just requires rebuilding the
index (ADR 0035 decision 1).

The shape, for a CJK run like "内核中":

    unigrams   内 核 中
    bigrams    内核 核中

Unigrams come first so a run's bigrams are *adjacent positions* in the indexed
stream, which is what makes a phrase query work: FTS5 matches a quoted phrase
against consecutive positions, so `"内核"` (one bigram) finds the run, and
`"可观 观测 测性"` finds a three-character term. A single character has no
bigram and is searched as its own unigram, which is the fallback that keeps
one-character queries working.
"""

from __future__ import annotations

import itertools

# Han, hiragana, katakana: the scripts the corpus is actually written in
# (ADR 0035 fact 1 measured it). Hangul and others are left out deliberately --
# nothing in the corpus uses them, and adding a script that is never tested is
# how a tokeniser acquires a silent hole.
_CJK_RANGES = (
    (0x4E00, 0x9FFF),  # CJK unified ideographs
    (0x3040, 0x309F),  # hiragana
    (0x30A0, 0x30FF),  # katakana
)

# Characters that keep a latin run together. Code and identifiers are common in
# this corpus ("c++", "e-mail", "foo_bar", "v1.2", "c#"), and splitting them
# would produce fragments nobody searches for.
_WORD_PUNCTUATION = "+-_.#@'"

__all__ = ["phrase_query", "tokenize"]


def _is_cjk(character: str) -> bool:
    return any(low <= ord(character) <= high for low, high in _CJK_RANGES)


def _is_word(character: str) -> bool:
    return character.isalnum() or character in _WORD_PUNCTUATION


def _runs(text: str) -> list[tuple[str, bool]]:
    """Split `text` into maximal (run, is_cjk) pieces.

    Anything that is neither CJK nor a word character is a separator and does
    not appear in the output: punctuation carries no meaning for matching, and
    keeping it would make every query depend on it.
    """
    runs: list[tuple[str, bool]] = []
    current: list[str] = []
    current_is_cjk = False

    def flush() -> None:
        if current:
            runs.append(("".join(current), current_is_cjk))

    for character in text:
        if _is_cjk(character):
            kind = True
        elif _is_word(character):
            kind = False
        else:
            # A separator ends the run. Skipping it without closing the run is
            # how "How to debug" became one token and how "可观 测性 是 观测"
            # grew bigrams across its spaces -- which made the query
            # "可观测性" match text that does not contain it (AC-85).
            flush()
            current = []
            continue
        if current and kind is not current_is_cjk:
            flush()
            current = []
        current.append(character)
        current_is_cjk = kind
    flush()
    # Punctuation counts only when it joins letters or digits: "c++", "foo_bar"
    # and "v1.2" stay whole, while "..." is a separator rather than a token
    # nobody could ever search for.
    return [
        (run, is_cjk)
        for run, is_cjk in runs
        if is_cjk or any(character.isalnum() for character in run)
    ]


def tokenize(text: str) -> str:
    """The token stream stored in the index and produced from a query.

    Call this on both sides or neither: an index built from one segmentation and
    searched with another returns nothing, without raising anything.
    """
    tokens: list[str] = []
    for run, is_cjk in _runs(text):
        if not is_cjk:
            tokens.append(run.lower())
            continue
        tokens.extend(run)
        tokens.extend(first + second for first, second in itertools.pairwise(run))
    return " ".join(tokens)


def _phrase(tokens: list[str]) -> str:
    """One FTS5 phrase: the tokens in order, as a quoted string.

    A quoted string inside an FTS5 phrase is escaped by doubling the quote, and
    a user's query can contain one -- `"` alone would end the phrase early and
    turn the rest of the query into syntax.
    """
    return '"' + " ".join(tokens).replace('"', '""') + '"'


def phrase_query(text: str) -> str:
    """The FTS5 `MATCH` expression for what the user typed (ADR 0035 decision 2).

    Each whitespace-separated term becomes one quoted phrase, and the terms are
    ANDed. The phrase is what makes a multi-character CJK term mean "this word
    appears here" rather than "these characters appear somewhere": a bare AND of
    bigrams matches `可观 测性 是 观测` for the query `可观测性`, which is a false
    positive measured in ADR 0035 (fact 4).

    Latin terms are quoted too -- one token, so the phrase is just that token --
    which keeps FTS5's own operators (`*`, `NEAR`, `OR`) out of the hands of a
    user who typed them as text.

    Returns an empty string when the query has no searchable characters, which
    callers must treat as "no filter" rather than as a match-nothing expression.
    """
    clauses: list[str] = []
    for run, is_cjk in _runs(text):
        if not is_cjk:
            clauses.append(_phrase([run.lower()]))
        elif len(run) == 1:
            clauses.append(_phrase([run]))
        else:
            clauses.append(
                _phrase([first + second for first, second in itertools.pairwise(run)])
            )
    return " AND ".join(clauses)
