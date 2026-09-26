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
    text = processor._extract_article_text(html_text)
    assert "Long main article text" in text
    assert "menu" not in text
    assert "sidebar note" not in text


def test_the_extractor_drops_nav_aside_script_and_style() -> None:
    html_text = (
        "<html><head><style>.x{color:red}</style></head><body>"
        "<script>var a=1;</script>"
        "<article><p>" + ("Body prose. " * 30) + "</p></article>"
        "<nav>nav links</nav><aside>aside text</aside></body></html>"
    )
    text = processor._extract_article_text(html_text)
    assert "Body prose" in text
    assert "nav links" not in text
    assert "aside text" not in text
    assert "color:red" not in text
    assert "var a=1" not in text


def test_the_processor_id_is_stable() -> None:
    assert processor.processor_id() == "fulltext"
    assert processor.processor_version() == "0.1.0"
