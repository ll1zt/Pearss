"""Tests for URL canonicalisation.

Canonicalisation feeds both identity and dedupe, so a bug here shows up as
either spurious duplicates or wrongly merged articles — neither of which is
visible until a feed is already polluted.
"""

from __future__ import annotations

import pytest

from core.normalize import canonicalize_url, source_canonical_key


@pytest.mark.unit
def test_scheme_and_host_are_lowercased() -> None:
    assert canonicalize_url("HTTPS://Example.COM/Path") == "https://example.com/Path"


@pytest.mark.unit
def test_default_ports_are_dropped() -> None:
    assert canonicalize_url("https://example.com:443/a") == "https://example.com/a"
    assert canonicalize_url("http://example.com:80/a") == "http://example.com/a"


@pytest.mark.unit
def test_non_default_ports_are_preserved() -> None:
    assert (
        canonicalize_url("https://example.com:8443/a") == "https://example.com:8443/a"
    )


@pytest.mark.unit
def test_fragment_is_removed() -> None:
    assert canonicalize_url("https://example.com/a#section") == "https://example.com/a"


@pytest.mark.unit
def test_empty_path_becomes_root() -> None:
    assert canonicalize_url("https://example.com") == "https://example.com/"


@pytest.mark.unit
def test_duplicate_slashes_are_collapsed() -> None:
    assert canonicalize_url("https://example.com//a///b") == "https://example.com/a/b"


@pytest.mark.unit
@pytest.mark.parametrize(
    "param",
    [
        "utm_source",
        "utm_medium",
        "fbclid",
        "gclid",
        "mc_cid",
        "_hsenc",
        "yclid",
        "igshid",
    ],
)
def test_tracking_parameters_are_dropped(param: str) -> None:
    url = f"https://example.com/a?{param}=zzz&keep=1"
    assert canonicalize_url(url) == "https://example.com/a?keep=1"


@pytest.mark.unit
def test_meaningful_query_is_preserved() -> None:
    # The query identifies the resource on many sites; dropping it would merge
    # distinct articles.
    assert (
        canonicalize_url("https://example.com/p?id=42") == "https://example.com/p?id=42"
    )


@pytest.mark.unit
def test_query_order_does_not_matter() -> None:
    first = canonicalize_url("https://example.com/a?x=1&y=2")
    second = canonicalize_url("https://example.com/a?y=2&x=1")
    assert first == second


@pytest.mark.unit
def test_url_is_trimmed() -> None:
    assert canonicalize_url("  https://example.com/a  ") == "https://example.com/a"


@pytest.mark.unit
def test_canonicalization_is_idempotent() -> None:
    once = canonicalize_url("https://example.com:443/a?utm_source=x&b=2&a=1#f")
    assert canonicalize_url(once) == once


@pytest.mark.unit
@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "not a url",
        "mailto:someone@example.com",
        "javascript:alert(1)",
        "https:///no-host",
    ],
)
def test_unusable_urls_are_rejected(url: str) -> None:
    # Silently returning these would let a non-article URL become an item's
    # identity. Failing loudly is the point.
    with pytest.raises(ValueError):
        canonicalize_url(url)


# --------------------------------------------------------------------------
# Source dedupe keys
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://a.example/feed?utm_source=x", "https://a.example/feed"),
        ("HTTPS://A.Example:443/Feed#f", "https://a.example/Feed"),
    ],
)
def test_rss_source_keys_on_the_canonical_url(url: str, expected: str) -> None:
    assert source_canonical_key(source_type="rss", url=url) == expected


@pytest.mark.unit
def test_web_source_keys_on_the_canonical_url() -> None:
    assert (
        source_canonical_key(source_type="web", url="https://a.example/page?fbclid=1")
        == "https://a.example/page"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("supplied", "expected"),
    [
        ("newsletter-intake", "agent://newsletter-intake"),
        ("agent://newsletter-intake", "agent://newsletter-intake"),
    ],
)
def test_agent_source_gets_a_synthetic_key(supplied: str, expected: str) -> None:
    # An agent source is pushed to rather than polled, so it has no URL to key
    # on — but the dedupe key still cannot be NULL, or the uniqueness
    # constraint stops covering the row entirely.
    assert source_canonical_key(source_type="agent", url=supplied) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("source_type", "url"),
    [
        ("rss", None),
        ("rss", ""),
        ("web", None),
        ("agent", None),
        ("agent", "   "),
    ],
)
def test_a_key_is_always_produced_or_the_call_is_refused(
    source_type: str, url: str | None
) -> None:
    with pytest.raises(ValueError):
        source_canonical_key(source_type=source_type, url=url)


@pytest.mark.unit
def test_unknown_source_type_is_refused() -> None:
    with pytest.raises(ValueError):
        source_canonical_key(source_type="carrier_pigeon", url="https://a.example/f")
