"""Tests for HTML sanitisation of untrusted feed bodies.

Feed bodies are attacker-controlled. These assert the dangerous constructs are
gone and the ordinary ones survive — a sanitiser that strips everything would
pass a naive test but destroy the point of storing markup.
"""

from __future__ import annotations

import pytest

from adapters.sanitize import sanitize_html


@pytest.mark.unit
def test_ordinary_formatting_survives() -> None:
    html = "<p>Hello <b>bold</b> and <em>italic</em>.</p>"
    assert sanitize_html(html) == html


@pytest.mark.unit
def test_links_survive() -> None:
    out = sanitize_html('<a href="https://example.com/post">Read more</a>')
    assert "https://example.com/post" in out
    assert "Read more" in out


@pytest.mark.unit
def test_lists_and_tables_survive() -> None:
    html = "<ul><li>one</li><li>two</li></ul>"
    assert sanitize_html(html) == html
    table = "<table><tr><td>cell</td></tr></table>"
    assert "cell" in sanitize_html(table)


@pytest.mark.unit
def test_images_survive_without_handlers() -> None:
    out = sanitize_html('<img src="https://example.com/a.png" alt="a">')
    assert "https://example.com/a.png" in out
    assert "<img" in out


# --------------------------------------------------------------------------
# Execution vectors
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "html",
    [
        "<script>alert(1)</script>",
        "<SCRIPT>alert(1)</SCRIPT>",
        "<script src='https://evil.example/x.js'></script>",
        "<iframe src='https://evil.example'></iframe>",
        "<object data='x.swf'></object>",
        "<embed src='x.swf'>",
        "<style>body{background:url(https://track.example)}</style>",
    ],
)
def test_executable_tags_are_removed(html: str) -> None:
    out = sanitize_html(html).lower()
    assert "<script" not in out
    assert "<iframe" not in out
    assert "<object" not in out
    assert "<embed" not in out
    assert "<style" not in out


@pytest.mark.unit
def test_script_content_does_not_survive_as_text() -> None:
    # Dropping the tag but keeping its text would leave the payload visible
    # and, in some contexts, still reachable.
    out = sanitize_html("<p>ok</p><script>var stolen = 1;</script>")
    assert "stolen" not in out
    assert "ok" in out


@pytest.mark.unit
@pytest.mark.parametrize(
    "html",
    [
        "<p onclick='alert(1)'>x</p>",
        "<img src=x onerror='alert(1)'>",
        "<body onload='alert(1)'>",
        "<a href='#' onmouseover='alert(1)'>x</a>",
    ],
)
def test_event_handler_attributes_are_stripped(html: str) -> None:
    out = sanitize_html(html).lower()
    # The payload vanishing is the real assertion: a handler attribute that
    # survived would still execute even if the name were rewritten.
    assert "alert(" not in out
    assert "onerror" not in out
    assert "onclick" not in out
    assert "onload" not in out
    assert "onmouseover" not in out


@pytest.mark.unit
@pytest.mark.parametrize(
    "html",
    [
        '<a href="javascript:alert(1)">x</a>',
        '<a href="JaVaScRiPt:alert(1)">x</a>',
        '<a href="data:text/html,<script>alert(1)</script>">x</a>',
        '<a href="vbscript:msgbox(1)">x</a>',
    ],
)
def test_dangerous_url_schemes_are_dropped(html: str) -> None:
    out = sanitize_html(html).lower()
    assert "javascript:" not in out
    assert "vbscript:" not in out
    assert "data:text/html" not in out
    assert "alert(" not in out


@pytest.mark.unit
def test_style_attribute_is_dropped() -> None:
    # CSS can carry url() beacons and behaviour we cannot audit.
    out = sanitize_html('<p style="background:url(https://track.example)">x</p>')
    assert "track.example" not in out
    assert "style=" not in out.lower()


@pytest.mark.unit
def test_form_and_input_elements_are_dropped() -> None:
    out = sanitize_html('<form action="https://evil.example"><input name="pw"></form>')
    assert "<form" not in out.lower()
    assert "<input" not in out.lower()


@pytest.mark.unit
def test_svg_is_dropped() -> None:
    # <svg><script> is a classic filter-bypass carrier.
    out = sanitize_html("<svg><script>alert(1)</script></svg>")
    assert "<svg" not in out.lower()
    assert "alert" not in out.lower()


@pytest.mark.unit
def test_comments_are_stripped() -> None:
    out = sanitize_html("<p>x</p><!-- conditional -->")
    assert "<!--" not in out


@pytest.mark.unit
def test_data_attributes_are_dropped() -> None:
    out = sanitize_html('<div data-foo="bar">x</div>')
    assert "data-foo" not in out


# --------------------------------------------------------------------------
# Behaviour of the function itself
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "html",
    ["", None],
)
def test_empty_input_returns_empty(html: str | None) -> None:
    assert sanitize_html(html or "") == ""


@pytest.mark.unit
def test_plain_text_passes_through_unchanged() -> None:
    assert sanitize_html("Just words, no markup.") == "Just words, no markup."


@pytest.mark.unit
def test_oversized_input_is_refused() -> None:
    # A backstop for a stored or hand-edited row; fetch already bounds size.
    assert sanitize_html("x" * 2_000_001) == ""


@pytest.mark.unit
def test_links_get_noopener_rel() -> None:
    # Without this a remote page can reach back through window.opener.
    out = sanitize_html('<a href="https://example.com">x</a>')
    assert "noopener" in out.lower()


@pytest.mark.unit
def test_idempotent() -> None:
    html = '<p>Hi <a href="https://e.com">link</a></p><script>bad()</script>'
    once = sanitize_html(html)
    assert sanitize_html(once) == once
