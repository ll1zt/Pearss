"""Fixture realism: responses on the wire carry content-encoding.

MockTransport handlers default to uncompressed bodies, which let the
HostClientTransport adapter ship already-decompressed content with the
origin's gzip header still attached - the child-side httpx then gunzipped
plain bytes (zlib -3) and the failure masqueraded as CookieCloud being
unreachable. This pins the fix and the convention: fixture transports
SHOULD set content-encoding so adapters that strip headers stay honest.
"""

from __future__ import annotations

import gzip

import httpx
import pytest

from app.plugin_runtime import HostClientTransport


@pytest.mark.unit
def test_host_transport_strips_content_encoding_after_decompression() -> None:
    """A gzipped origin response passes through as decoded, header-free bytes."""

    payload = gzip.compress(b"<rss>real body</rss>")

    def gzip_origin(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=payload,
            headers={"content-encoding": "gzip", "content-type": "text/xml"},
        )

    inner = httpx.Client(transport=httpx.MockTransport(gzip_origin))
    transport = HostClientTransport(inner)

    request = httpx.Request("GET", "https://origin.example.com/feed")
    response = transport.handle_request(request)

    assert response.content == b"<rss>real body</rss>"
    assert "content-encoding" not in response.headers
    assert int(response.headers["content-length"]) == len(response.content)
