"""Tests for the HTTP fetch adapter.

The transport is replaced via httpx's MockTransport rather than by patching
httpx internals: the transport is the boundary we own, so swapping it keeps
the client's own behaviour (redirects, header handling, timeouts) in play
instead of testing a mock of the library.
"""

from __future__ import annotations

from collections.abc import Callable

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


# --------------------------------------------------------------------------
# Private-address guard (ADR 0016)
# --------------------------------------------------------------------------


def _resolve_answer(answers: dict[str, set[str]]) -> Callable[[str], set[str]]:
    """A resolver stub keyed by host; unknown hosts answer public."""

    def resolve(host: str, *, deadline: float | None = None) -> set[str]:
        return answers.get(host, {"93.184.216.34"})

    return resolve


@pytest.mark.unit
def test_private_literal_ip_is_refused_without_a_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from adapters import http as pearss_http

    requests: list[httpx.Request] = []
    monkeypatch.setattr(
        pearss_http, "resolve_host", _resolve_answer({"127.0.0.1": {"127.0.0.1"}})
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)
    with (
        httpx.Client(transport=transport) as http_client,
        pytest.raises(FetchError) as exc_info,
    ):
        fetch("http://127.0.0.1:8901/feeds", client=http_client)

    assert exc_info.value.error_class is ErrorClass.BLOCKED
    assert requests == [], "the guard must refuse before touching a socket"


@pytest.mark.unit
def test_cloud_metadata_address_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    from adapters import http as pearss_http

    monkeypatch.setattr(
        pearss_http,
        "resolve_host",
        _resolve_answer({"169.254.169.254": {"169.254.169.254"}}),
    )
    transport = httpx.MockTransport(lambda request: httpx.Response(200))

    with (
        httpx.Client(transport=transport) as http_client,
        pytest.raises(FetchError) as exc_info,
    ):
        fetch("http://169.254.169.254/latest/meta-data/", client=http_client)

    assert exc_info.value.error_class is ErrorClass.BLOCKED


@pytest.mark.unit
def test_an_unresolvable_host_is_refused_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from adapters import http as pearss_http

    monkeypatch.setattr(
        pearss_http, "resolve_host", lambda host, *, deadline=None: set()
    )
    transport = httpx.MockTransport(lambda request: httpx.Response(200))

    with (
        httpx.Client(transport=transport) as http_client,
        pytest.raises(FetchError) as exc_info,
    ):
        fetch("https://gone.example.com/rss", client=http_client)

    assert exc_info.value.error_class is ErrorClass.BLOCKED


@pytest.mark.unit
def test_a_redirect_to_a_private_host_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from adapters import http as pearss_http

    # The classic SSRF chain: a public URL that 302s to the metadata service.
    monkeypatch.setattr(
        pearss_http,
        "resolve_host",
        _resolve_answer({"metadata.internal": {"169.254.169.254"}}),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "public.example.com":
            return httpx.Response(
                302, headers={"location": "http://metadata.internal/latest"}
            )
        return httpx.Response(200, content=FEED_BODY)

    transport = httpx.MockTransport(handler)
    with (
        httpx.Client(transport=transport) as http_client,
        pytest.raises(FetchError) as exc_info,
    ):
        fetch("https://public.example.com/rss", client=http_client)

    assert exc_info.value.error_class is ErrorClass.BLOCKED


@pytest.mark.unit
def test_a_cross_host_redirect_leaves_credentials_behind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from adapters import http as pearss_http

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "public.example.com":
            return httpx.Response(
                302, headers={"location": "https://other.example.com/feed"}
            )
        return httpx.Response(200, content=FEED_BODY)

    monkeypatch.setattr(pearss_http, "resolve_host", _resolve_answer({}))
    policy = FetchPolicy(extra_headers={"cookie": "session=1"})
    transport = httpx.MockTransport(handler)

    with httpx.Client(transport=transport) as http_client:
        fetch("https://public.example.com/rss", client=http_client, policy=policy)

    assert seen[0].headers["cookie"] == "session=1"
    assert "cookie" not in seen[1].headers, (
        "the redirect target was chosen by the previous host; credentials are "
        "for the previous host only"
    )


@pytest.mark.unit
def test_a_same_host_redirect_keeps_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    from adapters import http as pearss_http

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/moved":
            return httpx.Response(
                302, headers={"location": "https://feed.example.com/rss"}
            )
        return httpx.Response(200, content=FEED_BODY)

    monkeypatch.setattr(pearss_http, "resolve_host", _resolve_answer({}))
    policy = FetchPolicy(extra_headers={"cookie": "session=1"})
    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as http_client:
        fetch("https://feed.example.com/moved", client=http_client, policy=policy)

    assert seen[1].headers["cookie"] == "session=1"


@pytest.mark.unit
def test_redirect_hops_are_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    from adapters import http as pearss_http

    monkeypatch.setattr(pearss_http, "resolve_host", _resolve_answer({}))
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            302, headers={"location": "https://feed.example.com/next"}
        )
    )
    with (
        httpx.Client(transport=transport) as http_client,
        pytest.raises(FetchError) as exc_info,
    ):
        fetch(URL, client=http_client)

    assert exc_info.value.error_class is ErrorClass.NETWORK


@pytest.mark.unit
def test_allow_private_fetch_env_disables_the_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from adapters import http as pearss_http

    monkeypatch.setenv(pearss_http.ALLOW_PRIVATE_FETCH_ENV, "1")
    monkeypatch.setattr(
        pearss_http, "resolve_host", _resolve_answer({"127.0.0.1": {"127.0.0.1"}})
    )
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"ok"))

    with httpx.Client(transport=transport) as http_client:
        payload = fetch("http://127.0.0.1:8901/local-feed", client=http_client)

    assert payload.body == b"ok"
