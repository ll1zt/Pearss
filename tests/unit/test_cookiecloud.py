"""Tests for the CookieCloud adapter: config, cache, and secret hygiene.

Every case runs against httpx's MockTransport. The synthetic values below are
marked as such and exist so a test can assert where they land -- and, more
importantly, where they must not.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from adapters import cookiecloud as cc

# Synthetic. Never real, and never must appear in a log line or error detail.
FAKE_TOKEN = "synthetic-master-password"
FAKE_UUID = "synthetic-uuid-0001"
FAKE_COOKIE = "auth_token=synthetic-auth; ct0=synthetic-csrf; other=keep"
BASE = "http://127.0.0.1:4000"


def config(**overrides: object) -> cc.Config:
    values: dict[str, object] = {
        "base_url": BASE,
        "uuid": FAKE_UUID,
        "token": FAKE_TOKEN,
        "domain": "x.com",
        "cache_seconds": 300.0,
    }
    values.update(overrides)
    return cc.Config(**values)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def clear_cache() -> Callable[[], None]:
    cc.invalidate()

    def done() -> None:
        cc.invalidate()

    return done


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setenv(cc.BASE_URL_ENV, BASE)
    monkeypatch.setenv(cc.UUID_ENV, FAKE_UUID)
    monkeypatch.setenv(cc.TOKEN_ENV, FAKE_TOKEN)
    monkeypatch.delenv(cc.CACHE_TTL_ENV, raising=False)
    return monkeypatch


def client_with(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def fetch_with(
    handler: Callable[[httpx.Request], httpx.Response],
    made: cc.Config,
    **kwargs: object,
) -> str:
    """Fetch through a mocked transport, asserting the call succeeds."""
    with client_with(handler) as client:
        return cc.fetch_cookie_header(client, made, **kwargs)  # type: ignore[arg-type]


def fetch_raises(
    handler: Callable[[httpx.Request], httpx.Response],
    made: cc.Config,
) -> str:
    """Fetch through a mocked transport, returning the error message."""
    with (
        client_with(handler) as client,
        pytest.raises(cc.CookieCloudError) as excinfo,
    ):
        cc.fetch_cookie_header(client, made)
    return str(excinfo.value)


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_absent_configuration_is_none_rather_than_an_error() -> None:
    """A half-configured deployment has not enabled this path.

    Raising would break every collection for a feature nobody turned on.
    """
    made = cc.config_from_env(domain="x.com")
    assert made is None or not made.base_url


@pytest.mark.unit
def test_partial_configuration_is_ignored(env: pytest.MonkeyPatch) -> None:
    monkeypatch = env
    monkeypatch.delenv(cc.TOKEN_ENV)
    assert cc.config_from_env(domain="x.com") is None
    assert cc.TOKEN_ENV in cc.missing_config_keys()


@pytest.mark.unit
def test_full_configuration_is_read(env: pytest.MonkeyPatch) -> None:
    made = cc.config_from_env(domain="x.com")
    assert made is not None
    assert made.domain == "x.com"
    assert made.cache_seconds == cc.DEFAULT_CACHE_SECONDS
    assert cc.enabled()


@pytest.mark.unit
def test_trailing_slash_does_not_double_up_in_the_url(env: pytest.MonkeyPatch) -> None:
    env.setenv(cc.BASE_URL_ENV, BASE + "/")
    made = cc.config_from_env(domain="x.com")
    assert made is not None and made.base_url == BASE


@pytest.mark.unit
def test_a_nonsense_ttl_falls_back_rather_than_crashing(
    env: pytest.MonkeyPatch,
) -> None:
    env.setenv(cc.CACHE_TTL_ENV, "soon")
    made = cc.config_from_env(domain="x.com")
    assert made is not None and made.cache_seconds == cc.DEFAULT_CACHE_SECONDS


@pytest.mark.unit
def test_the_domain_is_required_rather_than_defaulted(
    env: pytest.MonkeyPatch,
) -> None:
    """No default domain, on purpose.

    A shared default would let a second login-type collector ask for cookies
    without saying whose, silently receive the first collector's, and then fail
    looking for cookie names that are not there. Requiring the argument moves
    that from a runtime mystery to a call-site error.
    """
    with pytest.raises(TypeError):
        cc.config_from_env()  # type: ignore[call-arg]


@pytest.mark.unit
def test_an_empty_domain_is_rejected_rather_than_exporting_everything(
    env: pytest.MonkeyPatch,
) -> None:
    """The server returns *all* buckets when the filter is empty.

    An unset domain must not quietly widen the request to every synced site.
    """
    with pytest.raises(ValueError):
        cc.config_from_env(domain="   ")


@pytest.mark.unit
def test_two_collectors_get_their_own_cookies_from_one_server(
    env: pytest.MonkeyPatch,
) -> None:
    """The case the global setting could not serve.

    X and a hypothetical Zhihu collector ask the same server and must each
    receive only their own domain's cookies, with independent cache entries.
    """
    asked: list[str] = []

    def capture(request: httpx.Request) -> httpx.Response:
        domain = request.url.params["domain"]
        asked.append(domain)
        return httpx.Response(200, text=f"session=for-{domain}")

    with client_with(capture) as client:
        x = cc.fetch_cookie_header(
            client, cc.config_from_env(domain="x.com") or config()
        )
        zhihu = cc.fetch_cookie_header(
            client, cc.config_from_env(domain="zhihu.com") or config()
        )

    assert x == "session=for-x.com"
    assert zhihu == "session=for-zhihu.com"
    assert asked == ["x.com", "zhihu.com"]


@pytest.mark.unit
def test_a_collector_declares_its_own_domain() -> None:
    """The domain lives with the collector, not in the deployment config."""
    from plugins.collectors.twitter import client as x_client

    assert x_client.COOKIE_DOMAIN == "x.com"


# --------------------------------------------------------------------------
# Request shape
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_request_is_scoped_to_one_domain() -> None:
    seen: list[httpx.Request] = []

    def capture(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=FAKE_COOKIE)

    with client_with(capture) as client:
        cc.fetch_cookie_header(client, config(domain="example.org"))

    request = seen[0]
    assert request.url.path == f"/get/{FAKE_UUID}"
    assert request.url.params["format"] == "header"
    assert request.url.params["domain"] == "example.org"
    assert request.headers["authorization"] == f"Bearer {FAKE_TOKEN}"


@pytest.mark.unit
def test_the_uuid_is_url_encoded() -> None:
    """A uuid with a slash would otherwise address a different record.

    httpx does not escape a "/" in an already-assembled URL, so the adapter
    has to: without encoding, "a/b" reaches /get/a/b -- a different path, and
    a silent wrong-record read rather than an error.
    """
    seen: list[httpx.Request] = []

    def capture(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=FAKE_COOKIE)

    with client_with(capture) as client:
        cc.fetch_cookie_header(client, config(uuid="a/b"))

    assert "/get/a%2Fb" in str(seen[0].url)


# --------------------------------------------------------------------------
# Response shapes
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_a_bare_header_is_returned_as_is() -> None:
    assert (
        fetch_with(lambda r: httpx.Response(200, text=FAKE_COOKIE), config())
        == FAKE_COOKIE
    )


@pytest.mark.unit
@pytest.mark.parametrize("key", ["cookie", "header", "cookies"])
def test_json_wrapped_variants_are_unwrapped(key: str) -> None:
    body = json.dumps({key: FAKE_COOKIE})
    assert fetch_with(lambda r: httpx.Response(200, text=body), config()) == FAKE_COOKIE


@pytest.mark.unit
@pytest.mark.parametrize(
    "body",
    ["", "   ", "no equals sign here", json.dumps({"unexpected": "shape"})],
)
def test_unusable_bodies_raise_rather_than_returning_empty(
    body: str,
) -> None:
    """An empty cookie would look like "the site has no session"."""
    fetch_raises(lambda r: httpx.Response(200, text=body), config())


# --------------------------------------------------------------------------
# Status classification
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("status", "needle"),
    [
        (401, "password"),
        (404, "uuid"),
        (409, "re-sync"),
        (500, "http 500"),
    ],
)
def test_server_errors_say_what_to_do(status: int, needle: str) -> None:
    """Each needs a different operator action, so each says which.

    A 409 in particular means the server holds only ciphertext it cannot open:
    the fix is a browser re-sync, not a password change.
    """
    message = fetch_raises(lambda r: httpx.Response(status), config())
    assert needle in message.lower()


@pytest.mark.unit
def test_a_transport_failure_does_not_echo_the_url() -> None:
    """The URL carries the uuid; uuid + master password is a full credential."""

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused at secret-host.internal", request=request)

    message = fetch_raises(boom, config())
    assert FAKE_UUID not in message
    assert "secret-host.internal" not in message


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_a_fresh_cache_avoids_a_second_request() -> None:
    calls: list[httpx.Request] = []

    def capture(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, text=FAKE_COOKIE)

    with client_with(capture) as client:
        first = cc.fetch_cookie_header(client, config())
        second = cc.fetch_cookie_header(client, config())

    assert first == second == FAKE_COOKIE
    assert len(calls) == 1


@pytest.mark.unit
def test_domains_do_not_share_a_cache_entry() -> None:
    """Serving x.com's cookies for a request about another site is a leak."""
    calls: list[str] = []

    def capture(request: httpx.Request) -> httpx.Response:
        domain = request.url.params["domain"]
        calls.append(domain)
        return httpx.Response(200, text=f"site={domain}")

    with client_with(capture) as client:
        x = cc.fetch_cookie_header(client, config(domain="x.com"))
        other = cc.fetch_cookie_header(client, config(domain="example.org"))

    assert x == "site=x.com" and other == "site=example.org"
    assert calls == ["x.com", "example.org"]


@pytest.mark.unit
def test_force_bypasses_the_cache() -> None:
    """This is what a rejected session uses to pick up a rotated cookie."""
    calls: list[httpx.Request] = []

    def capture(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, text=f"ct0=v{len(calls)}")

    with client_with(capture) as client:
        assert cc.fetch_cookie_header(client, config()) == "ct0=v1"
        assert cc.fetch_cookie_header(client, config()) == "ct0=v1"
        assert cc.fetch_cookie_header(client, config(), force=True) == "ct0=v2"


@pytest.mark.unit
def test_an_expired_entry_is_refetched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[httpx.Request] = []

    def capture(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, text=f"ct0=v{len(calls)}")

    import time as time_module

    clock = {"now": 1000.0}
    monkeypatch.setattr(time_module, "monotonic", lambda: clock["now"])

    with client_with(capture) as client:
        assert cc.fetch_cookie_header(client, config(cache_seconds=60.0)) == "ct0=v1"
        clock["now"] += 61.0
        assert cc.fetch_cookie_header(client, config(cache_seconds=60.0)) == "ct0=v2"


@pytest.mark.unit
def test_invalidate_targets_one_deployment_only() -> None:
    calls: list[httpx.Request] = []

    def capture(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, text=FAKE_COOKIE)

    one = config(uuid="uuid-one")
    two = config(uuid="uuid-two")
    with client_with(capture) as client:
        cc.fetch_cookie_header(client, one)
        cc.fetch_cookie_header(client, two)
        assert len(calls) == 2
        cc.invalidate(one)
        cc.fetch_cookie_header(client, one)
        cc.fetch_cookie_header(client, two)

    assert len(calls) == 3, "only the invalidated deployment should refetch"


# --------------------------------------------------------------------------
# Plaintext warning
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_plaintext_non_loopback_warns_without_leaking_the_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING"):
        fetch_with(
            lambda r: httpx.Response(200, text=FAKE_COOKIE),
            config(base_url="http://192.168.1.10:4000"),
        )

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert warnings, "a plaintext master-secret hop should be visible"
    joined = " ".join(r.getMessage() for r in warnings)
    assert FAKE_TOKEN not in joined
    assert "master" in joined.lower()


@pytest.mark.unit
def test_loopback_and_https_do_not_warn(caplog: pytest.LogCaptureFixture) -> None:
    for base in (BASE, "https://cookies.example.org"):
        with caplog.at_level("WARNING"):
            fetch_with(
                lambda r: httpx.Response(200, text=FAKE_COOKIE), config(base_url=base)
            )
        assert not [r for r in caplog.records if r.levelname == "WARNING"], base


# --------------------------------------------------------------------------
# Header parsing
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_cookie_header_parses_and_keeps_values_intact() -> None:
    pairs = cc.parse_cookie_header(FAKE_COOKIE)
    assert pairs["auth_token"] == "synthetic-auth"
    assert pairs["ct0"] == "synthetic-csrf"
    assert pairs["other"] == "keep"


@pytest.mark.unit
def test_a_value_containing_an_equals_sign_survives() -> None:
    """Cookie values are frequently base64 and end in `=`."""
    pairs = cc.parse_cookie_header("ct0=YWJjMTIz==; auth_token=x")
    assert pairs["ct0"] == "YWJjMTIz=="
    assert pairs["auth_token"] == "x"


@pytest.mark.unit
def test_a_duplicated_name_takes_the_last_one() -> None:
    """A browser sends the later, more specific scope last."""
    pairs = cc.parse_cookie_header("ct0=old; ct0=new")
    assert pairs["ct0"] == "new"


@pytest.mark.unit
def test_malformed_chunks_are_ignored_not_fatal() -> None:
    pairs = cc.parse_cookie_header("novalue; ; ct0=x; =emptname")
    assert pairs == {"ct0": "x"}
