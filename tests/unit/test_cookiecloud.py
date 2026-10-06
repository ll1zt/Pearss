"""Scenario register (normative source: SCENARIOS.md, AC-314..317; issue #338).

AC-314: the request asks the Go server's `/export` endpoint for `format=raw`
        with an admin bearer token, and the uuid stays percent-encoded.
AC-315: the raw JSON array becomes a `Cookie:` header, preserving the order the
        server sent, and a duplicated name keeps its most specific path.
AC-316: expired cookies and `secure` cookies aimed at a plaintext target are
        dropped; a session cookie without an expiry is kept.
AC-317: an empty array, an all-expired response and a body that is not a JSON
        array all raise rather than yielding an empty header; a deployment that
        only sets the retired master password has not enabled the path.
AC-318: a refusal is classified by the action that fixes it -- 401 (no admin
        token configured), 403 (token rejected vs. export disabled on the
        server: both are 403 `forbidden`, only the server's message tells them
        apart), 404 (uuid never synced), 409 (re-sync), 429 (rate limited) -- and
        no message carries the token or the uuid.
AC-319: a 404 is not cached: "never synced" is a state the operator's next
        attempt is supposed to change.
AC-320: a 200 is cached: two calls cost one request, because every export is
        audited and rate limited on the server.

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

pytestmark = pytest.mark.unit

# Synthetic. Never real, and never must appear in a log line or error detail.
FAKE_TOKEN = "synthetic-admin-token"
FAKE_UUID = "synthetic-uuid-0001"
BASE = "http://127.0.0.1:4000"


def raw(*cookies: dict[str, object]) -> str:
    """A `format=raw` body: the flat JSON array the server renders."""
    return json.dumps(list(cookies))


def a_cookie(name: str, value: str, **overrides: object) -> dict[str, object]:
    """One element of a `format=raw` response, shaped like the server's."""
    element: dict[str, object] = {
        "name": name,
        "value": value,
        "domain": ".x.com",
        "path": "/",
        "secure": False,
        "httpOnly": True,
        "hostOnly": False,
    }
    element.update(overrides)
    return element


FAKE_COOKIES: tuple[dict[str, object], ...] = (
    a_cookie("auth_token", "synthetic-auth"),
    a_cookie("ct0", "synthetic-csrf"),
    a_cookie("other", "keep"),
)
FAKE_BODY = raw(*FAKE_COOKIES)
FAKE_COOKIE = "auth_token=synthetic-auth; ct0=synthetic-csrf; other=keep"


def config(**overrides: object) -> cc.Config:
    values: dict[str, object] = {
        "base_url": BASE,
        "uuid": FAKE_UUID,
        "admin_token": FAKE_TOKEN,
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
    monkeypatch.setenv(cc.ADMIN_TOKEN_ENV, FAKE_TOKEN)
    monkeypatch.delenv(cc.CACHE_TTL_ENV, raising=False)
    return monkeypatch


def client_with(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def fetch_with(
    handler: Callable[[httpx.Request], httpx.Response],
    made: cc.Config,
    *,
    secure_ok: bool = True,
    **kwargs: object,
) -> str:
    """Fetch through a mocked transport, asserting the call succeeds."""
    with client_with(handler) as client:
        return cc.fetch_cookie_header(
            client,
            made,
            secure_ok=secure_ok,
            **kwargs,  # type: ignore[arg-type]
        )


def fetch_raises(
    handler: Callable[[httpx.Request], httpx.Response],
    made: cc.Config,
    *,
    secure_ok: bool = True,
) -> str:
    """Fetch through a mocked transport, returning the error message."""
    with (
        client_with(handler) as client,
        pytest.raises(cc.CookieCloudError) as excinfo,
    ):
        cc.fetch_cookie_header(client, made, secure_ok=secure_ok)
    return str(excinfo.value)


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def test_absent_configuration_is_none_rather_than_an_error() -> None:
    """A half-configured deployment has not enabled this path.

    Raising would break every collection for a feature nobody turned on.
    """
    made = cc.config_from_env(domain="x.com")
    assert made is None or not made.base_url


def test_partial_configuration_is_ignored(env: pytest.MonkeyPatch) -> None:
    monkeypatch = env
    monkeypatch.delenv(cc.ADMIN_TOKEN_ENV)
    assert cc.config_from_env(domain="x.com") is None
    assert cc.ADMIN_TOKEN_ENV in cc.missing_config_keys()


def test_the_retired_master_password_does_not_enable_the_path(
    env: pytest.MonkeyPatch,
) -> None:
    """The old credential name is not an alias: two sources would be two truths.

    An operator who upgrades the code but not the secret must land on the
    "not configured" path (a skip), not on a request that carries a token the
    new server rejects.
    """
    env.delenv(cc.ADMIN_TOKEN_ENV)
    env.setenv("PEARSS_COOKIECLOUD_TOKEN", "synthetic-master-password")
    assert cc.config_from_env(domain="x.com") is None
    assert cc.ADMIN_TOKEN_ENV in cc.missing_config_keys()


def test_full_configuration_is_read(env: pytest.MonkeyPatch) -> None:
    made = cc.config_from_env(domain="x.com")
    assert made is not None
    assert made.domain == "x.com"
    assert made.cache_seconds == cc.DEFAULT_CACHE_SECONDS
    assert cc.enabled()


def test_trailing_slash_does_not_double_up_in_the_url(env: pytest.MonkeyPatch) -> None:
    env.setenv(cc.BASE_URL_ENV, BASE + "/")
    made = cc.config_from_env(domain="x.com")
    assert made is not None and made.base_url == BASE


def test_a_nonsense_ttl_falls_back_rather_than_crashing(
    env: pytest.MonkeyPatch,
) -> None:
    env.setenv(cc.CACHE_TTL_ENV, "soon")
    made = cc.config_from_env(domain="x.com")
    assert made is not None and made.cache_seconds == cc.DEFAULT_CACHE_SECONDS


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


def test_an_empty_domain_is_rejected_rather_than_exporting_everything(
    env: pytest.MonkeyPatch,
) -> None:
    """The server returns *all* buckets when the filter is empty.

    An unset domain must not quietly widen the request to every synced site.
    """
    with pytest.raises(ValueError):
        cc.config_from_env(domain="   ")


def test_two_collectors_get_their_own_cookies_from_one_server(
    env: pytest.MonkeyPatch,
) -> None:
    """The case the global setting could not serve.

    X and a hypothetical Zhihu collector ask the same server and must each
    receive only their own domain's cookies, with independent cache entries.
    """

    def capture(request: httpx.Request) -> httpx.Response:
        domain = request.url.params["domain"]
        return httpx.Response(
            200, text=raw(a_cookie(f"session_{domain}", "for-" + domain))
        )

    with client_with(capture) as client:
        x = cc.fetch_cookie_header(
            client, cc.config_from_env(domain="x.com") or config(), secure_ok=True
        )
        zhihu = cc.fetch_cookie_header(
            client, cc.config_from_env(domain="zhihu.com") or config(), secure_ok=True
        )

    assert x == "session_x.com=for-x.com"
    assert zhihu == "session_zhihu.com=for-zhihu.com"


def test_a_collector_declares_its_own_domain() -> None:
    """The domain lives with the collector, not in the deployment config."""
    from plugins.collectors.twitter import client as x_client

    assert x_client.COOKIE_DOMAIN == "x.com"


# --------------------------------------------------------------------------
# Request shape (AC-314)
# --------------------------------------------------------------------------


def test_the_request_asks_the_export_endpoint_for_raw_json() -> None:
    seen: list[httpx.Request] = []

    def capture(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=FAKE_BODY)

    with client_with(capture) as client:
        cc.fetch_cookie_header(client, config(domain="example.org"), secure_ok=True)

    request = seen[0]
    assert request.url.path == f"/export/{FAKE_UUID}"
    assert request.url.params["format"] == "raw"
    assert request.url.params["domain"] == "example.org"
    assert request.headers["authorization"] == f"Bearer {FAKE_TOKEN}"
    assert request.headers["accept"] == "application/json"


def test_the_uuid_is_url_encoded() -> None:
    """A uuid with a slash would otherwise address a different record.

    httpx does not escape a "/" in an already-assembled URL, so the adapter
    has to: without encoding, "a/b" reaches /export/a/b -- a different path, and
    a silent wrong-record read rather than an error.
    """
    seen: list[httpx.Request] = []

    def capture(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=FAKE_BODY)

    with client_with(capture) as client:
        cc.fetch_cookie_header(client, config(uuid="a/b"), secure_ok=True)

    assert "/export/a%2Fb" in str(seen[0].url)


# --------------------------------------------------------------------------
# Response shape (AC-315)
# --------------------------------------------------------------------------


def test_the_raw_cookie_array_becomes_a_cookie_header() -> None:
    assert fetch_with(lambda r: httpx.Response(200, text=FAKE_BODY), config()) == (
        FAKE_COOKIE
    )


def test_cookies_keep_the_order_the_server_sent() -> None:
    """The server already sorted them (by domain, then name); do not reorder.

    Re-sorting here would be a second ordering rule to keep in sync, and the
    header's order is visible to the origin.
    """
    body = raw(
        a_cookie("b", "2"),
        a_cookie("a", "1"),
        a_cookie("c", "3"),
    )
    assert fetch_with(lambda r: httpx.Response(200, text=body), config()) == (
        "b=2; a=1; c=3"
    )


def test_the_most_specific_path_wins_for_a_duplicated_name() -> None:
    """A browser sends the narrowest matching scope, so that value is the one.

    The adapter cannot see the request path, so "narrowest" is the closest
    stand-in; on equal paths the later entry wins, matching `parse_cookie_header`.
    """
    body = raw(
        a_cookie("ct0", "broad", path="/"),
        a_cookie("ct0", "narrow", path="/api/v4"),
        a_cookie("ct0", "same", path="/other"),
    )
    assert fetch_with(lambda r: httpx.Response(200, text=body), config()) == (
        "ct0=narrow"
    )


def test_a_session_cookie_with_an_empty_value_is_still_sent() -> None:
    """`name=` is a real cookie; dropping it would look like "no session"."""
    body = raw(a_cookie("empty", ""), a_cookie("ct0", "x"))
    assert fetch_with(lambda r: httpx.Response(200, text=body), config()) == (
        "empty=; ct0=x"
    )


def test_an_element_that_is_not_a_cookie_is_skipped_not_fatal() -> None:
    """The server owns the shape; one odd element must not poison the header."""
    body = json.dumps([{"not": "a cookie"}, a_cookie("ct0", "x"), "nonsense"])
    assert fetch_with(lambda r: httpx.Response(200, text=body), config()) == "ct0=x"


def test_a_cookie_that_would_break_the_header_is_dropped() -> None:
    """`raw` does not escape, so a `;` in a value would inject another pair.

    No valid cookie value carries `;` or a newline (RFC 6265 cookie-octet), so
    dropping it is safer than writing a header that says something else.
    """
    body = raw(a_cookie("ct0", "ok"), a_cookie("evil", "x; auth_token=stolen"))
    assert fetch_with(lambda r: httpx.Response(200, text=body), config()) == "ct0=ok"


# --------------------------------------------------------------------------
# Expiry and transport security (AC-316)
# --------------------------------------------------------------------------


def test_an_expired_cookie_is_dropped() -> None:
    body = raw(
        a_cookie("stale", "old", expirationDate=1.0),
        a_cookie("fresh", "new", expirationDate=4102444800.0),
    )
    assert fetch_with(lambda r: httpx.Response(200, text=body), config()) == "fresh=new"


def test_a_cookie_without_an_expiry_is_a_session_cookie_and_is_kept() -> None:
    body = raw(a_cookie("session", "x"))
    assert fetch_with(lambda r: httpx.Response(200, text=body), config()) == (
        "session=x"
    )


def test_a_secure_cookie_is_dropped_for_a_plaintext_target() -> None:
    """A browser would not send a `secure` cookie over http://."""
    body = raw(a_cookie("s", "secure", secure=True), a_cookie("p", "plain"))
    assert fetch_with(
        lambda r: httpx.Response(200, text=body), config(), secure_ok=False
    ) == ("p=plain")


def test_a_secure_cookie_is_kept_for_an_https_target() -> None:
    body = raw(a_cookie("s", "secure", secure=True), a_cookie("p", "plain"))
    assert fetch_with(
        lambda r: httpx.Response(200, text=body), config(), secure_ok=True
    ) == ("s=secure; p=plain")


def test_a_cookie_that_expires_while_cached_is_not_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expiry is evaluated per call, so a warm cache cannot outlive the cookie."""
    import time as time_module

    clock = {"now": 1000.0}
    monkeypatch.setattr(time_module, "time", lambda: clock["now"])

    body = raw(a_cookie("ct0", "x", expirationDate=1500.0))
    with client_with(lambda r: httpx.Response(200, text=body)) as client:
        assert cc.fetch_cookie_header(client, config(), secure_ok=True) == "ct0=x"
        clock["now"] = 2000.0
        with pytest.raises(cc.CookieCloudError):
            cc.fetch_cookie_header(client, config(), secure_ok=True)


# --------------------------------------------------------------------------
# Empty and malformed bodies (AC-317)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("body", ["", "   ", "not json", json.dumps({"a": 1}), "[]"])
def test_unusable_bodies_raise_rather_than_returning_empty(body: str) -> None:
    """An empty header would look like "the site has no session"."""
    fetch_raises(lambda r: httpx.Response(200, text=body), config())


def test_a_response_whose_cookies_all_expired_raises() -> None:
    body = raw(a_cookie("stale", "old", expirationDate=1.0))
    message = fetch_raises(lambda r: httpx.Response(200, text=body), config())
    assert "expired" in message.lower()


def test_an_oversized_response_is_refused_by_size() -> None:
    """Issue #85: this fetch is capped like every other one.

    CookieCloud answers with a small JSON document, so anything larger is a
    mistake or a bomb -- and the message has to say which, because "too big" is
    a different fix from "rejected the token".
    """
    message = fetch_raises(
        lambda r: httpx.Response(200, content=b"x" * (cc.MAX_COOKIE_BYTES + 1)),
        config(),
    )
    assert "exceeded" in message


# --------------------------------------------------------------------------
# Status classification
# --------------------------------------------------------------------------


def _server_error(status: int, *, message: str | None = None) -> httpx.Response:
    """What the server answers when it refuses: `{"error":{code,message}}`."""
    if message is None:
        return httpx.Response(status)
    return httpx.Response(
        status, json={"error": {"code": "forbidden", "message": message}}
    )


@pytest.mark.parametrize(
    ("status", "server_message", "needle"),
    [
        (401, None, "admin token"),
        (403, "the admin token is wrong", "reject"),
        (403, "admin endpoints are disabled because ADMIN_TOKEN is not set", "disabl"),
        (404, None, "sync"),
        (409, None, "re-sync"),
        (429, None, "rate limit"),
        (500, None, "http 500"),
    ],
)
def test_a_refusal_says_which_action_fixes_it(
    status: int, server_message: str | None, needle: str
) -> None:
    """403 has two meanings, and only the server's own message tells them apart.

    "token is wrong" is a credential to replace; "export is disabled" is a
    server-side setting the operator has to add -- sending both readers to the
    same instruction sends half of them the wrong way. 404 means the uuid was
    never synced, 409 means re-sync, and 429 means slow down rather than fix.
    """
    message = fetch_raises(
        lambda r: _server_error(status, message=server_message), config()
    )
    assert needle in message.lower()


@pytest.mark.parametrize(
    ("status", "server_message"),
    [
        (401, None),
        (403, "the admin token is wrong"),
        (403, "admin endpoints are disabled"),
        (404, None),
        (409, None),
        (429, None),
    ],
)
def test_no_refusal_message_carries_the_token_or_the_uuid(
    status: int, server_message: str | None
) -> None:
    """Both are credentials, and both of these messages reach job_run."""
    message = fetch_raises(
        lambda r: _server_error(status, message=server_message), config()
    )
    assert FAKE_TOKEN not in message
    assert FAKE_UUID not in message


def test_the_403_body_is_classified_not_relayed() -> None:
    """The server's message is untrusted input: it is read, never echoed."""
    message = fetch_raises(
        lambda r: _server_error(403, message="disabled -- token LEAK-CANARY-42"),
        config(),
    )
    assert "LEAK-CANARY-42" not in message
    assert "disabl" in message.lower()


def test_a_non_json_error_body_still_classifies_by_status() -> None:
    """A proxy in the path answers HTML; that must not become a crash."""
    message = fetch_raises(
        lambda r: httpx.Response(404, text="<html>not found</html>"), config()
    )
    assert "sync" in message.lower()


def test_a_404_is_not_cached() -> None:
    """AC-319: "never synced" changes the moment the browser syncs.

    Caching it would pin the failure for a whole TTL, and the operator's next
    attempt is exactly the thing that should fix it.
    """
    calls: list[httpx.Request] = []

    def capture(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(404)
        return httpx.Response(200, text=FAKE_BODY)

    with client_with(capture) as client:
        with pytest.raises(cc.CookieCloudError):
            cc.fetch_cookie_header(client, config(), secure_ok=True)
        recovered = cc.fetch_cookie_header(client, config(), secure_ok=True)

    assert recovered == FAKE_COOKIE
    assert len(calls) == 2, "the 404 must not have been cached"


def test_a_transport_failure_does_not_echo_the_url() -> None:
    """The URL carries the uuid; uuid + admin token is a full credential."""

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused at secret-host.internal", request=request)

    message = fetch_raises(boom, config())
    assert FAKE_UUID not in message
    assert "secret-host.internal" not in message


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------


def test_a_fresh_cache_avoids_a_second_request() -> None:
    """AC-320: every export is audited and rate limited, so do not re-ask."""
    calls: list[httpx.Request] = []

    def capture(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, text=FAKE_BODY)

    with client_with(capture) as client:
        first = cc.fetch_cookie_header(client, config(), secure_ok=True)
        second = cc.fetch_cookie_header(client, config(), secure_ok=True)

    assert first == second == FAKE_COOKIE
    assert len(calls) == 1


def test_domains_do_not_share_a_cache_entry() -> None:
    """Serving x.com's cookies for a request about another site is a leak."""
    calls: list[str] = []

    def capture(request: httpx.Request) -> httpx.Response:
        domain = request.url.params["domain"]
        calls.append(domain)
        return httpx.Response(200, text=raw(a_cookie("site", domain)))

    with client_with(capture) as client:
        x = cc.fetch_cookie_header(client, config(domain="x.com"), secure_ok=True)
        other = cc.fetch_cookie_header(
            client, config(domain="example.org"), secure_ok=True
        )

    assert x == "site=x.com" and other == "site=example.org"
    assert calls == ["x.com", "example.org"]


def test_force_bypasses_the_cache() -> None:
    """This is what a rejected session uses to pick up a rotated cookie."""
    calls: list[httpx.Request] = []

    def capture(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, text=raw(a_cookie("ct0", f"v{len(calls)}")))

    with client_with(capture) as client:
        assert cc.fetch_cookie_header(client, config(), secure_ok=True) == "ct0=v1"
        assert cc.fetch_cookie_header(client, config(), secure_ok=True) == "ct0=v1"
        assert (
            cc.fetch_cookie_header(client, config(), secure_ok=True, force=True)
            == "ct0=v2"
        )


def test_an_expired_entry_is_refetched(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[httpx.Request] = []

    def capture(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, text=raw(a_cookie("ct0", f"v{len(calls)}")))

    import time as time_module

    clock = {"now": 1000.0}
    monkeypatch.setattr(time_module, "monotonic", lambda: clock["now"])

    with client_with(capture) as client:
        assert (
            cc.fetch_cookie_header(client, config(cache_seconds=60.0), secure_ok=True)
            == "ct0=v1"
        )
        clock["now"] += 61.0
        assert (
            cc.fetch_cookie_header(client, config(cache_seconds=60.0), secure_ok=True)
            == "ct0=v2"
        )


def test_invalidate_targets_one_deployment_only() -> None:
    calls: list[httpx.Request] = []

    def capture(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, text=FAKE_BODY)

    one = config(uuid="uuid-one")
    two = config(uuid="uuid-two")
    with client_with(capture) as client:
        cc.fetch_cookie_header(client, one, secure_ok=True)
        cc.fetch_cookie_header(client, two, secure_ok=True)
        assert len(calls) == 2
        cc.invalidate(one)
        cc.fetch_cookie_header(client, one, secure_ok=True)
        cc.fetch_cookie_header(client, two, secure_ok=True)

    assert len(calls) == 3, "only the invalidated deployment should refetch"


# --------------------------------------------------------------------------
# Plaintext warning
# --------------------------------------------------------------------------


def test_plaintext_non_loopback_warns_without_leaking_the_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING"):
        fetch_with(
            lambda r: httpx.Response(200, text=FAKE_BODY),
            config(base_url="http://192.168.1.10:4000"),
        )

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert warnings, "a plaintext admin-token hop should be visible"
    joined = " ".join(r.getMessage() for r in warnings)
    assert FAKE_TOKEN not in joined
    assert "token" in joined.lower()


def test_loopback_and_https_do_not_warn(caplog: pytest.LogCaptureFixture) -> None:
    for base in (BASE, "https://cookies.example.org"):
        with caplog.at_level("WARNING"):
            fetch_with(
                lambda r: httpx.Response(200, text=FAKE_BODY), config(base_url=base)
            )
        assert not [r for r in caplog.records if r.levelname == "WARNING"], base


# --------------------------------------------------------------------------
# Header parsing
# --------------------------------------------------------------------------


def test_cookie_header_parses_and_keeps_values_intact() -> None:
    pairs = cc.parse_cookie_header(FAKE_COOKIE)
    assert pairs["auth_token"] == "synthetic-auth"
    assert pairs["ct0"] == "synthetic-csrf"
    assert pairs["other"] == "keep"


def test_a_value_containing_an_equals_sign_survives() -> None:
    """Cookie values are frequently base64 and end in `=`."""
    pairs = cc.parse_cookie_header("ct0=YWJjMTIz==; auth_token=x")
    assert pairs["ct0"] == "YWJjMTIz=="
    assert pairs["auth_token"] == "x"


def test_a_duplicated_name_takes_the_last_one() -> None:
    """A browser sends the later, more specific scope last."""
    pairs = cc.parse_cookie_header("ct0=old; ct0=new")
    assert pairs["ct0"] == "new"


def test_malformed_chunks_are_ignored_not_fatal() -> None:
    pairs = cc.parse_cookie_header("novalue; ; ct0=x; =emptname")
    assert pairs == {"ct0": "x"}
