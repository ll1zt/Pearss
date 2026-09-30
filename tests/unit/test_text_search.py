"""The token stream the index and the query share must actually find the text.

Scenario register (normative source: ADR 0035 decisions 1, 2, 9; issue #78).

  AC-81 用户搜索中文两字词能命中正文
    Given 一篇文章正文含 "eBPF 在内核中运行沙箱程序"
    When  用户搜索 "内核"
    Then  该文章被召回

  AC-82 用户搜索中文三字以上词能命中(覆盖跨 bigram 边界)
    Given 一篇文章正文含 "可观测性"
    When  用户搜索 "可观测性"
    Then  该文章被召回

  AC-83 用户用假名搜索能命中
    Given 一篇日文文章含 "どうやったら"
    When  用户搜索 "どうやったら"
    Then  该文章被召回

  AC-84 英文大小写不敏感
    Given 文章含 "eBPF"
    When  用户搜索 "ebpf" 或 "EBPF"
    Then  两种写法都召回同一文章

  AC-85 散落的 token 不产生假阳性
    Given 文章正文含 "可观 测性 是 观测 的 特性"
    When  用户搜索 "可观测性"
    Then  该文章**不**被召回

  AC-86 单字搜索不漏召回(unigram 回退)
    Given 文章含 "神经网络的原理"
    When  用户搜索 "神"
    Then  该文章被召回

  AC-87 入库与查询用同一个函数(不变量)
    Given 语料里任意一篇
    When  用 `tokenize()` 建索引、用 `phrase_query()` 查询
    Then  每篇都能被它自己的内容召回, 且 `tokenize()` 对同一输入恒定

**断言的是召回, 不是 token 列表**(issue 的断言强度要求): 内部切出哪些 token 是
实现细节, 重构分词实现时不该碎。两个例外, 都写在下面: `phrase_query()` 的输出是给
FTS5 的**契约字符串**(格式错就搜不到), 以及简繁不互通这条**已知限制**要显式覆盖。

**真实资源**: 召回在真实的 FTS5 表上验证(内存库, 由 `tokenize()` 建), 不做字符串比较
——"分词对了但 FTS5 查询语法错了"是真实失败模式, 只有真索引能抓住它。
"""

from __future__ import annotations

import sqlite3

import pytest

from core.text_search import phrase_query, tokenize

pytestmark = pytest.mark.unit

# The golden set (ADR 0035 section 9 asks for 20-30 queries across the three
# scripts the corpus is written in). Each row is (document, query, recalled).
# The tags name the acceptance scenario a row exists for; rows marked
# "limitation" record something the design *cannot* do, on purpose, so that it
# is a known boundary rather than a surprise.
GOLDEN: list[tuple[str, str, bool, str]] = [
    # -- Chinese, the two-character case (AC-81)
    ("eBPF 在内核中运行沙箱程序", "内核", True, "AC-81"),
    ("这套可观测性方案基于 eBPF", "观测", True, "AC-81"),
    ("本文讨论卷积神经网络", "网络", True, "AC-81"),
    # -- Chinese, three characters and longer: the bigram boundary (AC-82)
    ("这套可观测性方案基于 eBPF", "可观测性", True, "AC-82"),
    ("可观测性平台上线了", "可观测性", True, "AC-82"),
    ("沙箱程序在内核中运行", "内核中运行", True, "AC-82"),
    ("本文讨论卷积神经网络", "卷积神经网络", True, "AC-82"),
    # -- Japanese kana, including the mixed kanji case (AC-83)
    ("どうやったら上手くいくのか", "どうやったら", True, "AC-83"),
    ("どうやったら上手くいくのか", "やったら", True, "AC-83"),
    ("QT AIも半導体も重要鉱物(レアアース)も…", "レアアース", True, "AC-83"),
    ("QT AIも半導体も重要鉱物(レアアース)も…", "半導体", True, "AC-83"),
    # -- Latin case folding (AC-84)
    ("eBPF 在内核中运行", "ebpf", True, "AC-84"),
    ("eBPF 在内核中运行", "EBPF", True, "AC-84"),
    ("How to debug eBPF programs", "DEBUG", True, "AC-84"),
    # -- code-ish latin runs stay whole
    ("用 c++ 和 foo_bar 写的", "c++", True, "AC-84"),
    ("用 c++ 和 foo_bar 写的", "foo_bar", True, "AC-84"),
    # -- scattered characters must not add up to a word (AC-85)
    ("可观 测性 是 观测 的 特性", "可观测性", False, "AC-85"),
    ("神 经 网 络 的 原 理", "神经网络", False, "AC-85"),
    # -- one character still finds its text (AC-86)
    ("神经网络的原理", "神", True, "AC-86"),
    ("神经网络的原理", "络", True, "AC-86"),
    # -- terms are ANDed, so a missing one is not recalled
    ("eBPF 在内核中运行沙箱程序", "eBPF 内核", True, "AC-81"),
    ("eBPF 在内核中运行沙箱程序", "eBPF 用户态", False, "AC-81"),
    ("A post about distributed systems", "distributed systems", True, "AC-84"),
    ("A post about distributed systems", "distributed storage", False, "AC-84"),
    # -- recorded limitations, not surprises
    (
        "內核的問題",
        "内核",
        False,
        "limitation: 简繁不互通 —— bigram 桥接不了码位差异(ADR 0035 决策 9)",
    ),
    (
        "内核的问题",
        "內核",
        False,
        "limitation: 同上, 反方向",
    ),
    (
        "本文讨论卷积神经网络",
        "积神",
        True,
        "limitation: bigram 对未登录词对是宽松的('积神' 是相邻二元)",
    ),
]


@pytest.fixture
def index() -> sqlite3.Connection:
    """A real FTS5 table holding every golden document, tokenised by `tokenize`.

    In memory, and built through the same function the query side uses: that is
    the invariant under test, not an implementation detail of the fixture.
    """
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE VIRTUAL TABLE docs USING fts5(body_tok, tokenize='unicode61')"
    )
    connection.executemany(
        "INSERT INTO docs (body_tok) VALUES (?)",
        [(tokenize(document),) for document, _, _, _ in GOLDEN],
    )
    return connection


def _recalled(index: sqlite3.Connection, query: str) -> set[str]:
    """The documents a user's query would return."""
    rows = index.execute(
        "SELECT rowid FROM docs WHERE docs MATCH ?", (phrase_query(query),)
    ).fetchall()
    return {GOLDEN[row[0] - 1][0] for row in rows}


@pytest.mark.parametrize(
    ("document", "query", "expected", "note"),
    GOLDEN,
    ids=[f"{row[3]}-{row[1]}" for row in GOLDEN],
)
def test_the_golden_queries_find_what_they_should(
    index: sqlite3.Connection, document: str, query: str, expected: bool, note: str
) -> None:
    """AC-60: every row of the golden set, against a real index."""
    found = _recalled(index, query)
    if expected:
        assert document in found, f"query {query!r} did not find {document!r}"
    else:
        assert document not in found, f"query {query!r} should not find {document!r}"


# --------------------------------------------------------------------------
# AC-87: the invariant, which only the tests can hold
# --------------------------------------------------------------------------


def test_every_document_is_found_by_its_own_text(index: sqlite3.Connection) -> None:
    """AC-87: index and query through the same function, or nothing matches.

    This is the failure the design is built around: a document indexed with one
    segmentation and searched with another returns nothing, and nothing raises.
    Searching each document for a distinctive slice of itself is the cheapest
    way to notice.
    """
    # "found among the results", not "the only result": a document's text
    # legitimately matches a longer document that contains it.
    missing = [
        document
        for document, _, _, _ in GOLDEN
        if document not in _recalled(index, document)
    ]
    assert not missing, f"documents not found by their own text: {missing}"


def test_tokenize_is_deterministic() -> None:
    """AC-87: no dictionary, no version, no state -- the same input, always.

    A dictionary segmenter would tie the index to the segmenter's version: two
    builds of the same text can disagree after an upgrade, and the index and the
    query drift apart silently (ADR 0035 decision 1).
    """
    for document, _, _, _ in GOLDEN:
        assert tokenize(document) == tokenize(document)
    assert tokenize("") == ""
    assert tokenize("   ") == ""


# --------------------------------------------------------------------------
# The contract strings: the one place exact output is asserted
# --------------------------------------------------------------------------


def test_phrase_query_builds_the_expression_the_index_expects() -> None:
    """`phrase_query`'s output is a contract with FTS5, not an internal.

    The shape is the ADR's (decision 2): overlapping tokens as one quoted
    phrase, several terms ANDed. A wrong separator or an unquoted term is not a
    subtle degradation -- it is a syntax error or a different query.
    """
    assert phrase_query("可观测性") == '"可观 观测 测性"'
    assert phrase_query("内核") == '"内核"'
    assert phrase_query("神") == '"神"'
    assert phrase_query("eBPF") == '"ebpf"'
    assert phrase_query("eBPF 内核") == '"ebpf" AND "内核"'


def test_a_query_with_nothing_searchable_yields_no_expression() -> None:
    """Punctuation alone is not a search, and must not become a phrase.

    An empty expression means "no filter" to the caller; the alternative --
    returning `""` quoted as a phrase -- would be a query that matches nothing
    and would silently hide every item.
    """
    assert phrase_query("   ") == ""
    assert phrase_query("!!! ...") == ""
    assert phrase_query('"') == ""
