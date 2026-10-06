"""zsxq client tests: envelope unwrapping, credential policy, retries.

A MockTransport serves the reference-shaped API fixtures, so the envelope
handling and retry policy are exercised against real response bytes.
"""

from __future__ import annotations

import json
import pathlib

import httpx
import pytest

from adapters import cookiecloud
from core.enums import ErrorClass
from plugins.collectors.zsxq import client

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

COOKIECLOUD_ENV = (
    "PEARSS_COOKIECLOUD_URL",
    "PEARSS_COOKIECLOUD_UUID",
    "PEARSS_COOKIECLOUD_ADMIN_TOKEN",
)


@pytest.fixture
def no_cookies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(client.COOKIE_ENV, raising=False)
    monkeypatch.delenv(client.TOKEN_ENV, raising=False)
    for name in COOKIECLOUD_ENV:
        monkeypatch.delenv(name, raising=False)


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _transport(
    responses: list[httpx.Response], seen: list[httpx.Request]
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return responses.pop(0)

    return httpx.MockTransport(handler)


def _cookiecloud_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure a CookieCloud source whose network layer is stubbed out."""
    monkeypatch.setenv("PEARSS_COOKIECLOUD_URL", "http://127.0.0.1:4000")
    monkeypatch.setenv("PEARSS_COOKIECLOUD_UUID", "uuid")
    monkeypatch.setenv("PEARSS_COOKIECLOUD_ADMIN_TOKEN", "token")

    def fake_config(domain: str) -> object:
        return f"config:{domain}"

    def fake_fetch(
        http_client: httpx.Client, config: object, *, secure_ok: bool, force: bool
    ) -> str:
        return "zsxq_access_token=fresh;" if force else "zsxq_access_token=stale;"

    monkeypatch.setattr(cookiecloud, "config_from_env", fake_config)
    monkeypatch.setattr(cookiecloud, "fetch_cookie_header", fake_fetch)


@pytest.mark.unit
def test_envelope_is_unwrapped_to_resp_data(
    monkeypatch: pytest.MonkeyPatch, no_cookies: None
) -> None:
    monkeypatch.setenv(client.TOKEN_ENV, "token-1")
    seen: list[httpx.Request] = []
    transport = _transport([httpx.Response(200, content=_fixture("group.json"))], seen)

    with httpx.Client(transport=transport) as http_client:
        payload = client.get_json("/groups/88855458825252", client=http_client)

    assert payload.ok
    # The wrapper is gone; what remains is resp_data — the group itself sits
    # under its own "group" key, exactly as the reference client delivers it.
    assert json.loads(payload.text())["group"]["name"] == "示例星球"
    request = seen[0]
    assert request.url.host == "api.zsxq.com"
    assert request.headers["cookie"] == "zsxq_access_token=token-1;"
    assert request.headers["referer"] == client.API_REFERER
    assert request.headers["user-agent"] == client.user_agent()


@pytest.mark.unit
def test_bare_token_env_becomes_the_cookie_header(
    monkeypatch: pytest.MonkeyPatch, no_cookies: None
) -> None:
    monkeypatch.setenv(client.TOKEN_ENV, "token-1")
    assert client.static_cookie_header() == "zsxq_access_token=token-1;"


@pytest.mark.unit
def test_full_static_cookie_wins_over_the_bare_token(
    monkeypatch: pytest.MonkeyPatch, no_cookies: None
) -> None:
    monkeypatch.setenv(client.TOKEN_ENV, "token-1")
    monkeypatch.setenv(client.COOKIE_ENV, "zsxq_access_token=token-2; other=1")
    assert client.static_cookie_header() == "zsxq_access_token=token-2; other=1"


@pytest.mark.unit
def test_a_static_cookie_without_the_token_is_unusable(
    monkeypatch: pytest.MonkeyPatch, no_cookies: None
) -> None:
    monkeypatch.setenv(client.COOKIE_ENV, "other=1")
    with (
        httpx.Client() as http_client,
        pytest.raises(client.CredentialError, match="zsxq_access_token"),
    ):
        client.get_json("/groups/1", client=http_client)


@pytest.mark.unit
def test_no_credentials_is_a_clear_auth_error(no_cookies: None) -> None:
    with (
        httpx.Client() as http_client,
        pytest.raises(client.CredentialError, match="zsxq_access_token"),
    ):
        client.get_json("/groups/1", client=http_client)


@pytest.mark.unit
def test_business_code_1059_retries_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, no_cookies: None
) -> None:
    monkeypatch.setenv(client.TOKEN_ENV, "token-1")
    pauses: list[float] = []
    monkeypatch.setattr(client, "_pause", pauses.append)

    seen: list[httpx.Request] = []
    responses = [
        httpx.Response(200, content=_fixture("topics_business_error.json")),
        httpx.Response(200, content=_fixture("topics_business_error.json")),
        httpx.Response(200, content=_fixture("topics_empty.json")),
    ]
    transport = _transport(responses, seen)

    with httpx.Client(transport=transport) as http_client:
        payload = client.get_json(
            "/groups/88855458825252/topics?scope=all&count=20", client=http_client
        )

    assert payload.ok
    assert len(seen) == 3
    assert pauses == [0.15, 0.3]


@pytest.mark.unit
def test_exhausted_1059_is_blocked(
    monkeypatch: pytest.MonkeyPatch, no_cookies: None
) -> None:
    monkeypatch.setenv(client.TOKEN_ENV, "token-1")
    monkeypatch.setattr(client, "_pause", lambda seconds: None)

    seen: list[httpx.Request] = []
    transport = _transport(
        [httpx.Response(200, content=_fixture("topics_business_error.json"))] * 4,
        seen,
    )

    with (
        httpx.Client(transport=transport) as http_client,
        pytest.raises(Exception) as exc_info,
    ):
        client.get_json("/groups/1/topics", client=http_client)

    assert getattr(exc_info.value, "error_class", None) is ErrorClass.BLOCKED
    assert len(seen) == 1 + client.MAX_1059_RETRIES


@pytest.mark.unit
def test_a_business_refusal_is_blocked_not_retried(
    monkeypatch: pytest.MonkeyPatch, no_cookies: None
) -> None:
    monkeypatch.setenv(client.TOKEN_ENV, "token-1")
    seen: list[httpx.Request] = []
    transport = _transport(
        [httpx.Response(200, content=_fixture("topics_refused.json"))], seen
    )

    with (
        httpx.Client(transport=transport) as http_client,
        pytest.raises(Exception) as exc_info,
    ):
        client.get_json("/groups/1/topics", client=http_client)

    assert getattr(exc_info.value, "error_class", None) is ErrorClass.BLOCKED
    assert "4031" in str(exc_info.value)
    assert len(seen) == 1


@pytest.mark.unit
def test_a_401_retries_once_with_a_fresh_cookiecloud_cookie(
    monkeypatch: pytest.MonkeyPatch, no_cookies: None
) -> None:
    seen: list[httpx.Request] = []
    transport = _transport(
        [
            httpx.Response(401, json={}),
            httpx.Response(200, content=_fixture("group.json")),
        ],
        seen,
    )
    _cookiecloud_source(monkeypatch)

    with httpx.Client(transport=transport) as http_client:
        payload = client.get_json("/groups/1", client=http_client)

    assert payload.ok
    assert len(seen) == 2
    assert seen[0].headers["cookie"] == "zsxq_access_token=stale;"
    assert seen[1].headers["cookie"] == "zsxq_access_token=fresh;"


@pytest.mark.unit
def test_a_static_401_is_not_retried(
    monkeypatch: pytest.MonkeyPatch, no_cookies: None
) -> None:
    monkeypatch.setenv(client.TOKEN_ENV, "token-1")
    seen: list[httpx.Request] = []
    transport = _transport([httpx.Response(401, json={})], seen)

    with (
        httpx.Client(transport=transport) as http_client,
        pytest.raises(Exception) as exc_info,
    ):
        client.get_json("/groups/1", client=http_client)

    assert len(seen) == 1, "a pinned token has nothing fresher to refresh to"
    assert getattr(exc_info.value, "error_class", None) is ErrorClass.AUTH
