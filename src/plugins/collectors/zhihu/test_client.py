"""Zhihu client tests: cookie resolution, signing, and the auth retry.

A real httpx client over a MockTransport captures what would go on the wire,
so the tests can assert that the signature the module computes is bound to
the cookie the request actually carries.
"""

from __future__ import annotations

import httpx
import pytest

from adapters import cookiecloud
from core.enums import ErrorClass
from plugins.collectors.zhihu import client, zse96

API_PATH = "/api/v4/members/example/answers?limit=10"

COOKIECLOUD_ENV = (
    "PEARSS_COOKIECLOUD_URL",
    "PEARSS_COOKIECLOUD_UUID",
    "PEARSS_COOKIECLOUD_TOKEN",
)


@pytest.fixture
def no_cookies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(client.COOKIES_ENV, raising=False)
    for name in COOKIECLOUD_ENV:
        monkeypatch.delenv(name, raising=False)


def _transport(
    responses: list[httpx.Response], seen: list[httpx.Request]
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return responses.pop(0)

    return httpx.MockTransport(handler)


def _json_response(payload: dict[str, object]) -> httpx.Response:
    return httpx.Response(200, json=payload)


# --------------------------------------------------------------------------
# Signing and headers
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_the_signature_is_bound_to_the_cookie_on_the_wire(
    monkeypatch: pytest.MonkeyPatch, no_cookies: None
) -> None:
    monkeypatch.setenv(client.COOKIES_ENV, "d_c0=testdc0; z_c0=static")
    seen: list[httpx.Request] = []
    transport = _transport([_json_response({"data": []})], seen)

    # The leading byte of a signature is random per request, so the wire value
    # cannot be recomputed. What must hold instead: the path the signer was
    # given is byte-identical to the path the transport sent, and it is bound
    # to the d_c0 the request actually carries.
    signed: list[tuple[str, str]] = []
    real_header_value = zse96.header_value

    def capturing_header_value(api_path: str, dc0: str, **kwargs: int) -> str:
        signed.append((api_path, dc0))
        return real_header_value(api_path, dc0, **kwargs)

    monkeypatch.setattr(zse96, "header_value", capturing_header_value)

    with httpx.Client(transport=transport) as http_client:
        client.get_json(API_PATH, client=http_client, referer="https://www.zhihu.com/")

    request = seen[0]
    assert request.url.host == "www.zhihu.com"
    assert request.url.path == "/api/v4/members/example/answers"
    assert request.headers["cookie"] == "d_c0=testdc0; z_c0=static"
    assert request.headers["x-zse-93"] == zse96.XZSE93
    assert request.headers["x-app-za"] == "OS=Web"
    assert request.headers["x-requested-with"] == "fetch"
    assert request.headers["referer"] == "https://www.zhihu.com/"
    assert request.headers["user-agent"] == client.user_agent()
    assert request.headers["x-zse-96"].startswith("2.0_")
    sent_path = request.url.path + "?" + request.url.query.decode()
    assert signed == [(sent_path, "testdc0")]


@pytest.mark.unit
def test_non_api_paths_are_refused_before_any_request(no_cookies: None) -> None:
    with httpx.Client() as http_client, pytest.raises(ValueError, match="/api/"):
        client.get_json("/people/example", client=http_client)


# --------------------------------------------------------------------------
# Cookie resolution
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_no_credentials_is_a_clear_auth_error(no_cookies: None) -> None:
    with (
        httpx.Client() as http_client,
        pytest.raises(client.CredentialError, match="d_c0"),
    ):
        client.get_json(API_PATH, client=http_client)


@pytest.mark.unit
def test_a_cookie_without_dc0_is_unusable(
    monkeypatch: pytest.MonkeyPatch, no_cookies: None
) -> None:
    monkeypatch.setenv(client.COOKIES_ENV, "z_c0=session-only")
    with (
        httpx.Client() as http_client,
        pytest.raises(client.CredentialError, match="d_c0"),
    ):
        client.get_json(API_PATH, client=http_client)


@pytest.mark.unit
def test_static_cookies_win_over_cookiecloud(
    monkeypatch: pytest.MonkeyPatch, no_cookies: None
) -> None:
    monkeypatch.setenv(client.COOKIES_ENV, "d_c0=static-wins")
    seen: list[httpx.Request] = []
    transport = _transport([_json_response({"data": []})], seen)

    def unexpected(*args: object, **kwargs: object) -> str:
        raise AssertionError("CookieCloud must not be consulted when static is set")

    monkeypatch.setattr(cookiecloud, "fetch_cookie_header", unexpected)

    with httpx.Client(transport=transport) as http_client:
        client.get_json(API_PATH, client=http_client)
    assert seen[0].headers["cookie"] == "d_c0=static-wins"


# --------------------------------------------------------------------------
# The auth retry
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_a_401_retries_once_with_a_fresh_cookiecloud_cookie(
    monkeypatch: pytest.MonkeyPatch, no_cookies: None
) -> None:
    seen: list[httpx.Request] = []
    transport = _transport(
        [httpx.Response(401, json={}), _json_response({"data": []})], seen
    )
    _cookiecloud_source(monkeypatch)

    with httpx.Client(transport=transport) as http_client:
        payload = client.get_json(API_PATH, client=http_client)

    assert payload.ok
    assert len(seen) == 2
    assert seen[0].headers["cookie"] == "d_c0=stale"
    assert seen[1].headers["cookie"] == "d_c0=fresh"
    # The second signature is bound to the fresh cookie, not the stale one.
    assert seen[0].headers["x-zse-96"] != seen[1].headers["x-zse-96"]


@pytest.mark.unit
def test_a_second_401_stops_retrying(
    monkeypatch: pytest.MonkeyPatch, no_cookies: None
) -> None:
    seen: list[httpx.Request] = []
    transport = _transport(
        [httpx.Response(401, json={}), httpx.Response(401, json={})], seen
    )
    _cookiecloud_source(monkeypatch)

    with (
        httpx.Client(transport=transport) as http_client,
        pytest.raises(Exception) as exc_info,
    ):
        client.get_json(API_PATH, client=http_client)

    assert len(seen) == 2, "exactly one refresh retry, then stop"
    assert getattr(exc_info.value, "error_class", None) is ErrorClass.AUTH


def _cookiecloud_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure a CookieCloud source whose network layer is stubbed out."""
    monkeypatch.setenv("PEARSS_COOKIECLOUD_URL", "http://127.0.0.1:4000")
    monkeypatch.setenv("PEARSS_COOKIECLOUD_UUID", "uuid")
    monkeypatch.setenv("PEARSS_COOKIECLOUD_TOKEN", "token")

    def fake_config(domain: str) -> object:
        return f"config:{domain}"

    def fake_fetch(http_client: httpx.Client, config: object, *, force: bool) -> str:
        return "d_c0=fresh" if force else "d_c0=stale"

    monkeypatch.setattr(cookiecloud, "config_from_env", fake_config)
    monkeypatch.setattr(cookiecloud, "fetch_cookie_header", fake_fetch)


@pytest.mark.unit
def test_a_static_401_is_not_retried(
    monkeypatch: pytest.MonkeyPatch, no_cookies: None
) -> None:
    monkeypatch.setenv(client.COOKIES_ENV, "d_c0=pinned")
    seen: list[httpx.Request] = []
    transport = _transport([httpx.Response(401, json={})], seen)

    with (
        httpx.Client(transport=transport) as http_client,
        pytest.raises(Exception) as exc_info,
    ):
        client.get_json(API_PATH, client=http_client)

    assert len(seen) == 1, "a pinned cookie has nothing fresher to refresh to"
    assert getattr(exc_info.value, "error_class", None) is ErrorClass.AUTH


@pytest.mark.unit
def test_a_403_is_blocked_not_auth(
    monkeypatch: pytest.MonkeyPatch, no_cookies: None
) -> None:
    seen: list[httpx.Request] = []
    transport = _transport([httpx.Response(403, json={})], seen)
    _cookiecloud_source(monkeypatch)

    with (
        httpx.Client(transport=transport) as http_client,
        pytest.raises(Exception) as exc_info,
    ):
        client.get_json(API_PATH, client=http_client)

    assert getattr(exc_info.value, "error_class", None) is ErrorClass.BLOCKED
    assert len(seen) == 1, "retrying an explicit refusal is wrong under either reading"


@pytest.mark.unit
def test_rate_limiting_surfaces_as_blocked(
    monkeypatch: pytest.MonkeyPatch, no_cookies: None
) -> None:
    monkeypatch.setenv(client.COOKIES_ENV, "d_c0=pinned")
    seen: list[httpx.Request] = []
    transport = _transport(
        [httpx.Response(429, json={}, headers={"retry-after": "7"})], seen
    )

    with (
        httpx.Client(transport=transport) as http_client,
        pytest.raises(Exception) as exc_info,
    ):
        client.get_json(API_PATH, client=http_client)

    assert getattr(exc_info.value, "error_class", None) is ErrorClass.BLOCKED
    assert len(seen) == 1
