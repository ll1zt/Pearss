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


# --------------------------------------------------------------------------
# The one allowed class: the avatar marker
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_the_avatar_class_survives() -> None:
    """A reader hooks border-radius off this class to round the avatar.

    It is the single exception to "no class anywhere": a fixed constant,
    admitted by value through the attribute filter rather than by the
    whitelist, which is what keeps it a styling hook and nothing more.
    """
    out = sanitize_html(
        '<img src="https://a.example/p.png" class="pearss-avatar" alt="A">'
    )
    assert 'class="pearss-avatar"' in out
    assert 'src="https://a.example/p.png"' in out
    assert 'alt="A"' in out


@pytest.mark.unit
def test_a_class_that_merely_contains_the_marker_is_dropped() -> None:
    """Partial injection must not ride along with the marker.

    `class="pearss-avatar evil"` surviving as both tokens would let remote
    content tag its markup with arbitrary additional hooks; the filter keeps
    the attribute only when the whole value is the constant.
    """
    out = sanitize_html(
        '<img src="https://a.example/p.png" class="pearss-avatar evil">'
    )
    assert "class" not in out


@pytest.mark.unit
def test_any_other_class_is_still_dropped() -> None:
    out = sanitize_html('<img src="https://a.example/p.png" class="rounded-xl">')
    assert "class" not in out


@pytest.mark.unit
def test_the_class_filter_does_not_touch_other_attributes() -> None:
    """The filter sees every whitelisted attribute, not just class.

    nh3 calls it per attribute and treats None as "strip", so a filter that
    answers None outside its target silently deletes every img's src. This is
    exactly the bug the first cut of the filter had; the avatar markup is the
    canary because it is the only place class and src appear together.
    """
    out = sanitize_html(
        '<img src="https://a.example/p.png" class="pearss-avatar"'
        ' alt="Ada" width="24" height="24" loading="lazy">'
    )
    assert 'src="https://a.example/p.png"' in out
    assert 'alt="Ada"' in out
    assert 'width="24"' in out
    assert 'height="24"' in out
    assert 'loading="lazy"' in out
    assert 'class="pearss-avatar"' in out


@pytest.mark.unit
def test_a_class_on_any_other_tag_is_dropped() -> None:
    out = sanitize_html('<a href="https://e.com" class="pearss-avatar">x</a>')
    assert "class" not in out
    assert 'href="https://e.com"' in out


# --------------------------------------------------------------------------
# The host-constrained video (ADR 0043, issue #91)
#
# Cross-module register (normative source: SCENARIOS.md, owned by issue #91):
#
#   AC-157 白名单宿主的视频以 <video controls> 出现, 无 autoplay/loop/muted
#   AC-158 非白名单宿主 / javascript: / onerror 的 video 被剥离
#   AC-159 降级: video 不被放行时 fallback 内容(缩略图+外链)仍存活
#   AC-160 净化对 video 的其余边界(幂等, 非视频正文不受前置 pass 影响)
# --------------------------------------------------------------------------

_VIDEO_SRC = "https://video.twimg.com/amplify_video/1/vid/avc1/832x480/x.mp4"


@pytest.mark.unit
def test_a_whitelisted_video_survives_with_controls() -> None:
    """AC-157: only a video from the enumerated host is admitted."""
    out = sanitize_html(
        f'<video src="{_VIDEO_SRC}" poster="https://pbs.twimg.com/t.jpg"'
        " controls></video>"
    )
    assert "<video" in out
    assert _VIDEO_SRC in out
    assert "controls" in out
    assert 'poster="https://pbs.twimg.com/t.jpg"' in out


@pytest.mark.unit
@pytest.mark.parametrize(
    "attribute", ["autoplay", "loop", "muted", "onerror='alert(1)'"]
)
def test_a_whitelisted_video_drops_autoplay_and_handlers(attribute: str) -> None:
    """AC-157: autoplay is harassment and bypasses traffic prefs; dropped."""
    out = sanitize_html(
        f'<video src="{_VIDEO_SRC}" {attribute} controls></video>'
    ).lower()
    assert "autoplay" not in out
    assert "loop" not in out
    assert "muted" not in out
    assert "onerror" not in out
    assert "alert(" not in out
    assert _VIDEO_SRC in out


@pytest.mark.unit
@pytest.mark.parametrize(
    "src",
    [
        "https://evil.example/x.mp4",
        "https://video.twimg.com.evil.example/x.mp4",
        "http://video.twimg.com/x.mp4",
        "javascript:alert(1)",
    ],
)
def test_a_video_from_an_untrusted_source_is_removed(src: str) -> None:
    """AC-158: the allowlist admits a source, not the tag itself.

    A scheme-only check would let any host serve the media; a host-suffix check
    would let `video.twimg.com.evil.example` through. The element is removed
    whole rather than left as an inert blank video.
    """
    out = sanitize_html(f'<video src="{src}" controls></video>')
    assert "<video" not in out.lower()
    assert "evil.example" not in out
    assert "alert(" not in out
    assert "javascript:" not in out.lower()


@pytest.mark.unit
def test_a_video_url_that_cannot_be_parsed_is_refused() -> None:
    """AC-158: `urlparse` can raise (an invalid IPv6 host); that is not allowed.

    The exception path is the difference between "refuse this URL" and "fail
    the whole ingest".
    """
    out = sanitize_html('<video src="https://[bad" controls></video>')
    assert "<video" not in out.lower()


@pytest.mark.unit
def test_a_video_url_with_no_host_is_refused() -> None:
    """AC-158: a URL with no host cannot be on the allowlist.

    `urlparse("https://").hostname` is None, and a naive check must not let a
    hostless URL slip through.
    """
    out = sanitize_html('<video src="https://" controls></video>')
    assert "<video" not in out.lower()


@pytest.mark.unit
def test_a_removed_video_keeps_the_text_around_it() -> None:
    """AC-160: the pre-pass removes the element, not its neighbour's text."""
    out = sanitize_html(
        '<p>before</p><video src="https://evil.example/x.mp4"></video><p>after</p>'
    )
    assert "before" in out
    assert "after" in out
    assert "<video" not in out.lower()


@pytest.mark.unit
def test_the_video_fallback_content_is_kept_inside_the_video() -> None:
    """AC-159: the collector's fallback link lives inside the allowed video."""
    inner = (
        '<a href="https://x.com/s/1">'
        '<img src="https://pbs.twimg.com/t.jpg" alt="video"/></a>'
    )
    out = sanitize_html(f'<video src="{_VIDEO_SRC}" controls>{inner}</video>')
    assert "<video" in out
    assert 'href="https://x.com/s/1"' in out
    assert "pbs.twimg.com/t.jpg" in out


@pytest.mark.unit
def test_the_video_fallback_survives_without_the_video_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-159 (the degradation): reverting the allowlist must not lose the video.

    This is the hard constraint of ADR 0043: the collector puts the existing
    thumbnail+link INSIDE the `<video>`, so when the tag is not allowed nh3
    strips the tag and keeps the content. Simulated by dropping `video` from
    the allowlist, which is exactly what a revert does.
    """
    from adapters import sanitize

    monkeypatch.setattr(sanitize, "ALLOWED_TAGS", sanitize.ALLOWED_TAGS - {"video"})
    out = sanitize.sanitize_html(
        f'<video src="{_VIDEO_SRC}" controls>'
        '<a href="https://x.com/s/1"><img src="https://pbs.twimg.com/t.jpg"'
        ' alt="video"/></a></video>'
    )

    assert "<video" not in out.lower()
    assert 'href="https://x.com/s/1"' in out, "the fallback link must survive"
    assert "pbs.twimg.com/t.jpg" in out


@pytest.mark.unit
def test_sanitising_a_video_is_idempotent() -> None:
    """AC-160: the pre-pass and the allowlist pass compose to a fixed point."""
    html = (
        f'<p>x</p><video src="{_VIDEO_SRC}" controls>'
        '<a href="https://x.com/s/1">v</a></video>'
    )
    once = sanitize_html(html)
    assert sanitize_html(once) == once
