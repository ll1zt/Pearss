"""Tests for the HTTP fetch adapter.

The transport is replaced via httpx's MockTransport rather than by patching
httpx internals: the transport is the boundary we own, so swapping it keeps
the client's own behaviour (redirects, header handling, timeouts) in play
instead of testing a mock of the library.
"""

from __future__ import annotations

import httpx
import pytest

from adapters.http import (
    DEFAULT_MAX_BYTES,
    ConditionalState,
    FetchError,
    FetchPolicy,
    fetch,
)
from core.enums import ErrorClass

URL = "https://feed.example.com/rss"
FEED_BODY = b'<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>'


def client_for(handler: httpx.MockTransport) -> httpx.Client:
    return httpx.Client(transport=handler)


def ok_transport(
    body: bytes = FEED_BODY,
    headers: dict[str, str] | None = None,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=body,
            headers={
                "content-type": "application/rss+xml",
                "etag": '"abc123"',
                "last-modified": "Mon, 05 Jan 2026 10:00:00 GMT",
                "set-cookie": "session=secret",
                **(headers or {}),
            },
        )

    return httpx.MockTransport(handler)


@pytest.mark.unit
def test_successful_fetch_returns_the_body() -> None:
    with client_for(ok_transport()) as client:
        payload = fetch(URL, client=client)
    assert payload.body == FEED_BODY
    assert payload.status_code == 200
    assert payload.ok


@pytest.mark.unit
def test_conditional_headers_are_sent_when_state_exists() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=FEED_BODY)

    with client_for(httpx.MockTransport(handler)) as client:
        fetch(
            URL,
            client=client,
            conditional=ConditionalState(
                etag='"abc123"', last_modified="Mon, 05 Jan 2026 10:00:00 GMT"
            ),
        )

    assert seen[0].headers["if-none-match"] == '"abc123"'
    assert seen[0].headers["if-modified-since"] == "Mon, 05 Jan 2026 10:00:00 GMT"


@pytest.mark.unit
def test_no_conditional_headers_on_a_first_fetch() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=FEED_BODY)

    with client_for(httpx.MockTransport(handler)) as client:
        fetch(URL, client=client)

    assert "if-none-match" not in seen[0].headers
    assert "if-modified-since" not in seen[0].headers


@pytest.mark.unit
def test_not_modified_is_not_an_error() -> None:
    # 304 is the origin telling us there is nothing new. Treating it as a
    # failure would mark every healthy source as broken on its second poll.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(304, headers={"etag": '"abc123"'})

    with client_for(httpx.MockTransport(handler)) as client:
        payload = fetch(URL, client=client)

    assert payload.not_modified
    assert payload.etag == '"abc123"'
    assert not payload.ok


@pytest.mark.unit
def test_user_agent_is_sent() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=FEED_BODY)

    with client_for(httpx.MockTransport(handler)) as client:
        fetch(URL, client=client)

    assert "Pearss" in seen[0].headers["user-agent"]


@pytest.mark.unit
def test_custom_user_agent_is_honoured() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=FEED_BODY)

    with client_for(httpx.MockTransport(handler)) as client:
        fetch(
            URL,
            client=client,
            policy=FetchPolicy(user_agent="MyReader/1.0 (contact@example.com)"),
        )

    assert seen[0].headers["user-agent"] == "MyReader/1.0 (contact@example.com)"


@pytest.mark.unit
def test_etag_and_last_modified_are_captured() -> None:
    with client_for(ok_transport()) as client:
        payload = fetch(URL, client=client)
    assert payload.etag == '"abc123"'
    assert payload.last_modified == "Mon, 05 Jan 2026 10:00:00 GMT"


@pytest.mark.unit
def test_sensitive_headers_are_dropped() -> None:
    # Set-Cookie must never reach RawFetch or a fixture. An allow-list is used
    # precisely so an unexpected header cannot leak by omission.
    with client_for(ok_transport()) as client:
        payload = fetch(URL, client=client)
    assert "set-cookie" not in payload.headers
    assert "authorization" not in payload.headers
    assert payload.headers["content-type"] == "application/rss+xml"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, ErrorClass.AUTH),
        (403, ErrorClass.BLOCKED),
        (429, ErrorClass.BLOCKED),
        (500, ErrorClass.NETWORK),
        (503, ErrorClass.NETWORK),
    ],
)
def test_http_errors_are_classified(status: int, expected: ErrorClass) -> None:
    # Classification is core's job; the adapter supplies the evidence.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status)

    with (
        client_for(httpx.MockTransport(handler)) as client,
        pytest.raises(FetchError) as excinfo,
    ):
        fetch(URL, client=client)
    assert excinfo.value.error_class is expected


@pytest.mark.unit
def test_oversized_response_is_rejected() -> None:
    # A size cap guards against a hostile or misconfigured origin; without it a
    # single response could exhaust memory.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 4096)

    with (
        client_for(httpx.MockTransport(handler)) as client,
        pytest.raises(FetchError) as excinfo,
    ):
        fetch(URL, client=client, policy=FetchPolicy(max_bytes=1024))
    assert excinfo.value.error_class is ErrorClass.NETWORK


@pytest.mark.unit
def test_body_within_the_cap_is_accepted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 1024)

    with client_for(httpx.MockTransport(handler)) as client:
        payload = fetch(URL, client=client, policy=FetchPolicy(max_bytes=1024))
    assert len(payload.body) == 1024


@pytest.mark.unit
def test_default_cap_is_generous_for_real_feeds() -> None:
    # The default must not reject an ordinary feed.
    assert DEFAULT_MAX_BYTES >= 1024 * 1024


@pytest.mark.unit
def test_timeout_is_classified_as_network() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    with (
        client_for(httpx.MockTransport(handler)) as client,
        pytest.raises(FetchError) as excinfo,
    ):
        fetch(URL, client=client)
    assert excinfo.value.error_class is ErrorClass.NETWORK


@pytest.mark.unit
def test_connect_error_is_classified_as_network() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    with (
        client_for(httpx.MockTransport(handler)) as client,
        pytest.raises(FetchError) as excinfo,
    ):
        fetch(URL, client=client)
    assert excinfo.value.error_class is ErrorClass.NETWORK


@pytest.mark.unit
def test_error_detail_does_not_leak_the_query_string() -> None:
    # Feed URLs routinely carry tokens, and AGENTS.md forbids logging them.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with (
        client_for(httpx.MockTransport(handler)) as client,
        pytest.raises(FetchError) as excinfo,
    ):
        fetch("https://feed.example.com/rss?token=hunter2", client=client)
    assert "hunter2" not in excinfo.value.detail
    assert "token" not in excinfo.value.detail


@pytest.mark.unit
def test_redirects_are_followed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rss":
            return httpx.Response(
                301, headers={"location": "https://feed.example.com/final"}
            )
        return httpx.Response(200, content=FEED_BODY)

    with client_for(httpx.MockTransport(handler)) as client:
        payload = fetch(URL, client=client)
    assert payload.status_code == 200
    assert payload.body == FEED_BODY


@pytest.mark.unit
def test_source_and_raw_fetch_are_carried_through() -> None:
    with client_for(ok_transport()) as client:
        payload = fetch(URL, client=client, source_id="src-9", raw_fetch_id="raw-9")
    assert payload.source_id == "src-9"
    assert payload.raw_fetch_id == "raw-9"


@pytest.mark.unit
def test_correlation_id_is_carried_through() -> None:
    with client_for(ok_transport()) as client:
        payload = fetch(URL, client=client, correlation_id="corr-1")
    assert payload.correlation_id == "corr-1"
