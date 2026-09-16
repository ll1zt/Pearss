"""Canonical URL identity for IPv6 literals.

A host is a dedupe key component (source.canonical_url is NOT NULL and unique),
so an unbracketed IPv6 literal must never be written back: the output would no
longer parse and the same origin would become two different identities.
"""

from __future__ import annotations

import pytest

from core.normalize import canonicalize_url

pytestmark = pytest.mark.unit


def test_user_ipv6_feed_url_keeps_its_brackets() -> None:
    """Given a bracketed IPv6 origin, when canonicalized, then still bracketed."""
    assert (
        canonicalize_url("https://[2001:4860:4860::8888]/feed")
        == "https://[2001:4860:4860::8888]/feed"
    )


def test_user_ipv6_with_non_default_port_keeps_brackets_and_port() -> None:
    assert (
        canonicalize_url("http://[2001:db8::1]:8080/a") == "http://[2001:db8::1]:8080/a"
    )


def test_user_ipv6_default_port_is_dropped_but_brackets_remain() -> None:
    assert canonicalize_url("https://[2001:db8::1]:443/a") == "https://[2001:db8::1]/a"


def test_user_ipv6_canonicalization_is_idempotent() -> None:
    once = canonicalize_url("https://[2001:4860:4860::8888]:443/feed")
    assert canonicalize_url(once) == once


def test_user_ipv6_host_case_is_normalized_inside_brackets() -> None:
    # Hex digits may be upper case in a literal; the host is lowercased like
    # any other.
    assert canonicalize_url("https://[2001:DB8::1]/a") == "https://[2001:db8::1]/a"
