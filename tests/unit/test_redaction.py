"""Scenario register (normative source: SCENARIOS.md).

AC-297: one implementation turns a URL into something safe to log -- no query,
no userinfo, no fragment. The fetch-site half of AC-297 lives in
``tests/unit/test_http.py``; the "only one implementation" structural guard
lives in ``tests/unit/test_structure_guards.py``.
"""

from __future__ import annotations

import pytest

from adapters.redaction import redact_url_for_log

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # userinfo (a basic-auth password), query (a feed token) and fragment
        # (an OAuth token) are each a credential, and all three are dropped.
        (
            "https://alice:s3cr3t@feed.example.com/rss?token=X#frag",
            "https://feed.example.com/rss",
        ),
        ("https://feed.example.com/rss?token=X", "https://feed.example.com/rss"),
        (
            "https://feed.example.com/rss#access_token=X",
            "https://feed.example.com/rss",
        ),
        ("https://feed.example.com/rss", "https://feed.example.com/rss"),
        # Host and port identify the resource, so they stay.
        ("https://feed.example.com:8443/rss", "https://feed.example.com:8443/rss"),
        ("http://127.0.0.1:8080/rss", "http://127.0.0.1:8080/rss"),
        # An IPv6 literal keeps its brackets: rebuilding the authority from
        # hostname/port would emit a URL that no longer parses.
        ("https://[::1]:8080/rss?token=X", "https://[::1]:8080/rss"),
        # Not a URL: an opaque id or a relative path is cut at ?/# only.
        ("guid-123", "guid-123"),
        ("/v1/items/abc?token=X", "/v1/items/abc"),
        ("urn:uuid:abc#frag", "urn:uuid:abc"),
    ],
)
def test_a_url_is_reduced_to_what_identifies_the_resource(
    raw: str, expected: str
) -> None:
    assert redact_url_for_log(raw) == expected
