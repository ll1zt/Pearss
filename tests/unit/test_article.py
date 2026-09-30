"""The shared article extractor: markup extraction, measurement, taxonomy.

Scenario register (normative source: `SCENARIOS.md`, AC-167). These tests lived
beside the fulltext processor while it owned the extractor; they moved with the
code when it became `adapters/article.py`, so the module that implements "page
HTML -> the article" carries its own proof (issue #167). Their assertions are
unchanged -- that is what makes the move a pure move, and the *pinned* golden
test below is AC-167's falsifiable half: a second implementation that renders
the same article differently fails here.

Extraction is a pure function over HTML: every input is a string, so there is
no origin, no network and no database in this file.
"""

from __future__ import annotations

import pytest

from adapters import article
from core.enums import ErrorClass

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------
# AC-167: one implementation, and its output is pinned
# --------------------------------------------------------------------------


def test_ac167_extraction_output_is_pinned_byte_for_byte() -> None:
    """AC-167: the shared extractor's markup is exactly what it was before.

    A golden string rather than a substring check. The move's whole promise is
    that a caller receives identical markup -- the fulltext processor today,
    the address-based fetch path next -- and "contains a paragraph" would stay
    green through a reordering, a re-wrapping or an attribute change that a
    renderer sees.
    """
    page = (
        "<html><body><nav>menu</nav><article>"
        "<h1>Headline</h1><p>Body text long enough to win the density score.</p>"
        "</article><footer>copyright</footer></body></html>"
    )

    assert article.extract_article_markup(page, base_url="https://example.test/") == (
        "<h1>Headline</h1><p>Body text long enough to win the density score.</p>"
    )


# --------------------------------------------------------------------------
# Extraction quality: pinned behavior of the pure extractor
# --------------------------------------------------------------------------


def test_the_extractor_prefers_the_largest_text_block() -> None:
    """Article detection: the dense text container wins over site chrome."""
    html_text = (
        "<html><body><nav>menu menu</nav>"
        + "<article><p>"
        + ("Long main article text. " * 30)
        + "</p></article>"
        + "<aside>sidebar note</aside></body></html>"
    )
    markup = article.extract_article_markup(html_text, base_url="https://example.test/")
    assert "Long main article text" in markup
    assert "menu" not in markup
    assert "sidebar note" not in markup


def test_the_extractor_drops_nav_aside_script_and_style() -> None:
    html_text = (
        "<html><head><style>.x{color:red}</style></head><body>"
        "<script>var a=1;</script>"
        "<article><p>" + ("Body prose. " * 30) + "</p></article>"
        "<nav>nav links</nav><aside>aside text</aside></body></html>"
    )
    markup = article.extract_article_markup(html_text, base_url="https://example.test/")
    assert "Body prose" in markup
    assert "nav links" not in markup
    assert "aside text" not in markup
    assert "color:red" not in markup
    assert "var a=1" not in markup


# --------------------------------------------------------------------------
# Parse-failure paths: each is reachable, so each gets a test
# --------------------------------------------------------------------------
#
# The coverage gate asked for these lines, and the honest answer is to reach
# them rather than declare them unreachable: all three were confirmed
# reachable with real input before writing the tests. An XML declaration
# carrying an encoding hint is the common real-world trigger — a page served
# as text/xml without a byte-order mark parses fine from bytes and raises
# "Unicode strings with encoding declaration are not supported" from str.


def test_a_page_with_an_encoding_declaration_still_parses() -> None:
    """lxml refuses a str with an encoding declaration; the retry strips it.

    Reachable in production: some origins serve the document as XML, and
    reporting such a page as unparseable would drop a perfectly good article.
    """
    page = (
        '<?xml version="1.0" encoding="utf-8"?><html><body><article>'
        "<h2>Title</h2><p>" + ("Long enough prose to keep. " * 20) + "</p>"
        "</article></body></html>"
    )
    markup = article.extract_article_markup(page, base_url="https://e.test/")

    assert "<h2" in markup
    assert "Long enough prose" in markup


def test_a_page_that_cannot_be_parsed_at_all_is_a_parse_error() -> None:
    """Both attempts fail -> ProcessorError(parse), never a silent empty body.

    The unterminated declaration is the case without a `?>` to split on, so
    the fallback's second branch (cut at `>`) is the one that runs.
    """
    from core.plugins.hooks import ProcessorError

    with pytest.raises(ProcessorError) as excinfo:
        article.extract_article_markup(
            '<?xml version="1.0" encoding="utf-8"', base_url="https://e.test/"
        )
    assert excinfo.value.error_class is ErrorClass.PARSE


def test_an_empty_document_is_a_parse_error() -> None:
    """A non-ValueError failure takes the outer handler, same classification.

    An empty body (a JS shell, a truncated response) raises lxml's ParserError
    rather than ValueError, and both must land in the taxonomy identically —
    otherwise one of them escapes as an unclassified crash.
    """
    from core.plugins.hooks import ProcessorError

    with pytest.raises(ProcessorError) as excinfo:
        article.extract_article_markup("", base_url="https://e.test/")
    assert excinfo.value.error_class is ErrorClass.PARSE


def test_measuring_unparseable_content_falls_back_to_word_count() -> None:
    """The threshold measurement never raises on a fragment it cannot parse.

    It runs before the fetch decision and on the stored body, so raising here
    would turn a malformed summary into a failed row instead of a plain short
    one — the measurement is allowed to be approximate, not fatal.
    """
    measured = article.plain_length('<?xml version="1.0" encoding="utf-8"?>')

    assert measured > 0
