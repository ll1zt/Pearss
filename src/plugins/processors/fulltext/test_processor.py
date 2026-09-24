"""The fulltext processor: extraction, fetch bounds, and error taxonomy.

Scenario register: src/plugins/processors/fulltext/SCENARIOS.md (AC-1..AC-6);
AC-7/AC-8 live in tests/integration/test_fulltext_chain.py, where the
app-level wiring belongs. Every origin in this suite is a local
ThreadingHTTPServer or a pure function; no test touches a real blog.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from core.enums import ErrorClass
from plugins.processors.fulltext import processor

pytestmark = pytest.mark.unit

# --------------------------------------------------------------------------
# The local origin: a real HTTP server, so the fetch path (timeouts, size
# caps, SSRF guard) is real. A fresh origin per test keeps request counts
# legible.
# --------------------------------------------------------------------------


class _Origin:
    """A minimal origin serving canned paths with canned statuses."""

    def __init__(self) -> None:
        self.routes: dict[str, tuple[int, bytes]] = {}
        self.requests: list[str] = []
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                outer.requests.append(self.path)
                status, body = outer.routes.get(self.path, (404, b"no such page"))
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = int(self._httpd.server_address[1])
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def stop(self) -> None:
        # shutdown() asks serve_forever() to stop; server_close() only closes the
        # listening socket, leaving the loop to spin on a closed socket. At ~89%
        # of a core per leaked server that is not a tidy-up detail -- it slowed
        # every later test in the same process (measured, issue #53).
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture
def origin() -> Iterator[_Origin]:
    one = _Origin()
    yield one
    one.stop()


@pytest.fixture(autouse=True)
def _hermetic_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve every host to 127.0.0.1 so the guard sees a public-shaped name.

    PEARSS_TEST_DNS is the sandbox-visible override (the variable the plugin
    runtime already forwards into children), and PEARSS_ALLOW_PRIVATE_FETCH
    admits the loopback answer. Both are the documented test escape hatches.
    """
    monkeypatch.setenv("PEARSS_TEST_DNS", '"*"')
    monkeypatch.setenv("PEARSS_ALLOW_PRIVATE_FETCH", "1")


ARTICLE = (
    """<html><head><title>t</title></head><body>
<nav>menu menu</nav>
<article>
<h1>The real headline</h1>
<p>"""
    + ("Paragraph of prose. " * 40)
    + """</p>
<p>Second paragraph of the article body.</p>
</article>
<footer>copyright</footer>
</body></html>"""
)


def _store_sink() -> tuple[Callable[[str], str], list[str]]:
    stored: list[str] = []

    def store(content: str) -> str:
        stored.append(content)
        return "artifact-ref"

    return store, stored


# --------------------------------------------------------------------------
# AC-1: summary item -> full text extracted, sanitized, published
# --------------------------------------------------------------------------


def test_user_summary_item_gets_full_text_extracted_from_the_article_page(
    origin: _Origin,
) -> None:
    """AC-1: Given a summary-only item, When processed, Then the product is
    the sanitized article text from the item's own URL, not the summary."""
    origin.routes["/post/1"] = (200, ARTICLE.encode())
    store, stored = _store_sink()

    processor.process(
        {
            "item_id": "it-1",
            "title": "The real headline",
            "content": "<p>Short summary&#8230;</p>",
            "url": origin.url("/post/1"),
        },
        target_lang="en",
        store=store,
    )

    assert stored, "an artifact must be produced"
    body = stored[0]
    assert "Paragraph of prose" in body
    assert "Second paragraph" in body
    # Site chrome (nav/footer) is not part of the article.
    assert "menu menu" not in body
    assert "copyright" not in body


def test_user_extracted_full_text_passes_the_store_time_sanitiser(
    origin: _Origin,
) -> None:
    """AC-1: the product is ADR-0007-safe: scripts and handlers gone."""
    dangerous = (
        "<html><body><article><p>"
        + ("Safe prose. " * 40)
        + '</p><script>alert(1)</script><p onclick="x()">More prose.</p>'
        + "</article></body></html>"
    )
    origin.routes["/post/2"] = (200, dangerous.encode())
    store, stored = _store_sink()

    processor.process(
        {
            "item_id": "it-2",
            "title": "t",
            "content": "<p>sum</p>",
            "url": origin.url("/post/2"),
        },
        target_lang="en",
        store=store,
    )

    body = stored[0]
    assert "<script" not in body
    assert "onclick" not in body
    assert "alert(1)" not in body
    assert "More prose" in body


# --------------------------------------------------------------------------
# AC-2: origin unreachable -> ProcessorError, taxonomy classes preserved
# --------------------------------------------------------------------------


def test_user_unreachable_origin_keeps_the_summary_and_fails_the_row(
    origin: _Origin,
) -> None:
    """AC-2: a 5xx page classifies as network, the summary survives."""
    from core.plugins.hooks import ProcessorError

    origin.routes["/post/3"] = (500, b"boom")
    store, stored = _store_sink()

    with pytest.raises(ProcessorError) as excinfo:
        processor.process(
            {
                "item_id": "it-3",
                "title": "t",
                "content": "<p>summary</p>",
                "url": origin.url("/post/3"),
            },
            target_lang="en",
            store=store,
        )
    assert excinfo.value.error_class is ErrorClass.NETWORK
    assert stored == []


def test_user_private_network_target_is_refused_without_a_request(
    origin: _Origin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-6: SSRF guard classes a private destination as blocked."""
    from core.plugins.hooks import ProcessorError

    monkeypatch.delenv("PEARSS_ALLOW_PRIVATE_FETCH")
    store, stored = _store_sink()

    with pytest.raises(ProcessorError) as excinfo:
        processor.process(
            {
                "item_id": "it-6",
                "title": "t",
                "content": "<p>sum</p>",
                "url": "http://127.0.0.1:9/x",
            },
            target_lang="en",
            store=store,
        )
    assert excinfo.value.error_class is ErrorClass.BLOCKED
    assert stored == []


# --------------------------------------------------------------------------
# AC-3: item already carries full text -> no network request at all
# --------------------------------------------------------------------------


def test_user_full_text_item_is_not_refetched(origin: _Origin) -> None:
    """AC-3: a body at or above the threshold is left alone."""
    long_body = "<p>" + ("Already complete prose. " * 60) + "</p>"
    store, stored = _store_sink()

    processor.process(
        {"item_id": "it-4", "title": "t", "content": long_body, "url": "x"},
        target_lang="en",
        store=store,
    )

    assert origin.requests == [], "no fetch may happen for full-text items"
    assert stored == [""]


# --------------------------------------------------------------------------
# AC-4: 200 but no extractable article -> parse failure, no fake success
# --------------------------------------------------------------------------


def test_user_page_without_an_article_body_is_a_parse_failure(
    origin: _Origin,
) -> None:
    """AC-4: a 200 page whose extracted text is below the floor is parse."""
    from core.plugins.hooks import ProcessorError

    origin.routes["/post/5"] = (200, b"<html><body><p>tiny</p></body></html>")
    store, stored = _store_sink()

    with pytest.raises(ProcessorError) as excinfo:
        processor.process(
            {
                "item_id": "it-5",
                "title": "t",
                "content": "<p>sum</p>",
                "url": origin.url("/post/5"),
            },
            target_lang="en",
            store=store,
        )
    assert excinfo.value.error_class is ErrorClass.PARSE
    assert stored == []


# --------------------------------------------------------------------------
# AC-5: title-only items succeed with an empty artifact, like translate
# --------------------------------------------------------------------------


def test_user_title_only_item_produces_an_empty_product_without_a_fetch(
    origin: _Origin,
) -> None:
    """AC-5: no body -> nothing to fetch; success opens the gate."""
    store, stored = _store_sink()
    ref = processor.process(
        {"item_id": "it-7", "title": "Announcement", "content": "", "url": "x"},
        target_lang="en",
        store=store,
    )
    assert origin.requests == []
    assert ref == "artifact-ref"
    assert stored == [""]


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
    markup = processor._extract_article_markup(
        html_text, base_url="https://example.test/"
    )
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
    markup = processor._extract_article_markup(
        html_text, base_url="https://example.test/"
    )
    assert "Body prose" in markup
    assert "nav links" not in markup
    assert "aside text" not in markup
    assert "color:red" not in markup
    assert "var a=1" not in markup


def test_the_processor_id_is_stable() -> None:
    assert processor.processor_id() == "fulltext"
    assert processor.processor_version() == "0.1.0"


# --------------------------------------------------------------------------
# AC-8..AC-12: the product keeps the article's structure (issue #32)
# --------------------------------------------------------------------------
#
# The first extractor returned plain text and re-wrapped every block as a bare
# <p>, so a fetched article lost its headings, images, code blocks, lists,
# quotes, tables and links — measured on a real page before the fix:
# h1/h2, img, pre/code, ul/li, blockquote, table and a were all gone, and
# nested blocks printed twice. These tests pin the structure, not just the
# words.

RICH_ARTICLE = """<html><body><nav>site menu</nav><article>
<h1>Understanding eBPF</h1>
<h2>Why it matters</h2>
<p>eBPF runs sandboxed programs <strong>in the kernel</strong>.</p>
<figure><img src="/images/ebpf-arch.png" alt="eBPF architecture diagram"></figure>
<pre><code>bpftool prog list
    bpftrace -e 'kprobe:do_sys_open { printf("%s", comm); }'</code></pre>
<ul><li>Observability</li><li>Networking</li></ul>
<blockquote><p>The kernel is the ultimate event source.</p></blockquote>
<table><tr><th>Hook</th><th>Use</th></tr><tr><td>kprobe</td><td>tracing</td></tr></table>
<p>See the <a href="/docs/start">official docs</a>.</p>
</article><footer>copyright</footer></body></html>"""


def _fetch_product(origin: _Origin, html_text: str) -> str:
    """Run the real processor over the local origin and return the product."""
    origin.routes["/post/rich"] = (200, html_text.encode())
    store, stored = _store_sink()
    processor.process(
        {
            "item_id": "it-rich",
            "title": "Understanding eBPF",
            "content": "<p>Short summary.</p>",
            "url": origin.url("/post/rich"),
        },
        target_lang="en",
        store=store,
    )
    assert stored, "an artifact must be produced"
    return stored[0]


def test_user_full_text_keeps_the_articles_structure(origin: _Origin) -> None:
    """AC-8: headings, images, code, lists, quotes, tables and links survive.

    A reader that receives bare paragraphs cannot show a code block as code,
    a diagram as an image, or a link as a link — which is what the summary
    feed's reader was staring at before this fix.
    """
    product = _fetch_product(origin, RICH_ARTICLE)

    for tag in (
        "<h1",
        "<h2",
        "<img",
        "<pre",
        "<code",
        "<ul",
        "<li",
        "<blockquote",
        "<table",
        "<tr",
        "<td",
        "<a ",
        "<figure",
    ):
        assert tag in product, f"{tag} was dropped from the product"
    # Site chrome is still gone.
    assert "site menu" not in product
    assert "copyright" not in product


def test_user_full_text_keeps_image_alt_and_absolute_urls(origin: _Origin) -> None:
    """AC-9: relative href/src are resolved against the item URL, alt kept.

    The artifact is rendered somewhere else entirely (a reader, a phone), so
    a relative path is a broken link there — and an image with no alt is
    invisible to anyone not looking at pixels.
    """
    product = _fetch_product(origin, RICH_ARTICLE)

    assert f'src="{origin.url("/images/ebpf-arch.png")}"' in product
    assert f'href="{origin.url("/docs/start")}"' in product
    assert 'alt="eBPF architecture diagram"' in product


def test_user_full_text_does_not_repeat_nested_blocks(origin: _Origin) -> None:
    """AC-10: a quote's paragraph appears once, not twice.

    Walking descendants while also emitting the ancestor printed the sentence
    twice (measured: the same blockquote text appeared twice in the product).
    """
    product = _fetch_product(origin, RICH_ARTICLE)

    assert product.count("The kernel is the ultimate event source.") == 1


def test_user_full_text_keeps_code_indentation(origin: _Origin) -> None:
    """AC-11: whitespace inside <pre> is code, not layout to collapse."""
    product = _fetch_product(origin, RICH_ARTICLE)

    assert "bpftool prog list" in product
    # The indented continuation line keeps its indentation and its own line.
    assert "    bpftrace" in product
    assert "bpftool prog list\n" in product


def test_user_full_text_is_still_sanitised(origin: _Origin) -> None:
    """AC-12: structure is kept, danger is not.

    The extractor now passes markup through, so the sanitiser is doing real
    work here rather than seeing pre-escaped text.
    """
    hostile = (
        "<html><body><article>"
        "<h2>Title</h2>"
        "<p>" + ("Long enough prose to pass the floor. " * 10) + "</p>"
        "<script>steal()</script>"
        '<p onclick="steal()">Inline handler</p>'
        '<p><a href="javascript:steal()">Bad link</a></p>'
        "</article></body></html>"
    )
    product = _fetch_product(origin, hostile)

    assert "<script" not in product
    assert "onclick" not in product
    assert "javascript:" not in product
    assert "steal()" not in product
    # The legitimate structure is still there.
    assert "<h2" in product
    assert "Inline handler" in product


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
    markup = processor._extract_article_markup(page, base_url="https://e.test/")

    assert "<h2" in markup
    assert "Long enough prose" in markup


def test_a_page_that_cannot_be_parsed_at_all_is_a_parse_error() -> None:
    """Both attempts fail -> ProcessorError(parse), never a silent empty body.

    The unterminated declaration is the case without a `?>` to split on, so
    the fallback's second branch (cut at `>`) is the one that runs.
    """
    from core.plugins.hooks import ProcessorError

    with pytest.raises(ProcessorError) as excinfo:
        processor._extract_article_markup(
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
        processor._extract_article_markup("", base_url="https://e.test/")
    assert excinfo.value.error_class is ErrorClass.PARSE


def test_measuring_unparseable_content_falls_back_to_word_count() -> None:
    """The threshold measurement never raises on a fragment it cannot parse.

    It runs before the fetch decision and on the stored body, so raising here
    would turn a malformed summary into a failed row instead of a plain short
    one — the measurement is allowed to be approximate, not fatal.
    """
    measured = processor._plain_length('<?xml version="1.0" encoding="utf-8"?>')

    assert measured > 0
