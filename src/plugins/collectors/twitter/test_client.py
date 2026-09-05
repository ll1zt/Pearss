"""Tests for the X client: request shape, credential handling, classification.

Every case runs against httpx's MockTransport. No request leaves the process,
and no fixture contains a credential -- the values below are synthetic and
marked as such.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from adapters.http import FetchPolicy
from core.enums import ErrorClass
from plugins.collectors.twitter import client as x

# Synthetic, never real: these exist so a test can assert where they land.
FAKE_AUTH = "synthetic-auth-token-not-a-secret"
FAKE_CT0 = "synthetic-ct0-not-a-secret"


def handler_for(
    *responses: httpx.Response,
) -> Callable[[httpx.Request], httpx.Response]:
    queue = list(responses)

    def handle(request: httpx.Request) -> httpx.Response:
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return handle


def client_with(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def credentials() -> x.Credentials:
    return x.Credentials(auth_token=FAKE_AUTH, ct0=FAKE_CT0)


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_missing_credentials_raise_rather_than_trying_anonymously() -> None:
    """ADR 0012: a missing credential stops as `auth`, it is not worked around."""
    import os

    for key in (x.AUTH_TOKEN_ENV, x.CT0_ENV):
        os.environ.pop(key, None)
    with pytest.raises(x.CredentialError):
        x.Credentials.from_env()
    assert not x.credentials_configured()


@pytest.mark.unit
def test_partial_credentials_are_still_missing() -> None:
    import os

    os.environ[x.AUTH_TOKEN_ENV] = FAKE_AUTH
    os.environ.pop(x.CT0_ENV, None)
    assert not x.credentials_configured()
    with pytest.raises(x.CredentialError):
        x.Credentials.from_env()


@pytest.mark.unit
def test_credentials_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(x.AUTH_TOKEN_ENV, FAKE_AUTH)
    monkeypatch.setenv(x.CT0_ENV, FAKE_CT0)
    loaded = x.Credentials.from_env()
    assert loaded.auth_token == FAKE_AUTH
    assert loaded.ct0 == FAKE_CT0
    assert x.credentials_configured()


@pytest.mark.unit
def test_csrf_header_must_equal_the_ct0_cookie() -> None:
    """X rejects the pair when they disagree, so both are derived from one."""
    headers = credentials().headers()
    assert headers["x-csrf-token"] == FAKE_CT0
    assert f"ct0={FAKE_CT0}" in headers["cookie"]
    assert f"auth_token={FAKE_AUTH}" in headers["cookie"]


# --------------------------------------------------------------------------
# Request shape
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_seen_tweet_ids_are_never_sent() -> None:
    """Sending them turns every refresh into a crawl through history.

    X reads `seenTweetIds` as "exclude these from the response", so the local
    stop-anchor can never reach an already-ingested id and paging runs to the
    budget or the end of the account's history. The upstream implementation
    records this as a regression it actually shipped; the assertion keeps the
    port from reintroducing it.
    """
    body = x.page_body(cursor=None, count=20)
    assert "seenTweetIds" not in json.dumps(body)
    assert "seen" not in json.dumps(body).lower()


@pytest.mark.unit
def test_cursor_is_passed_through_only_when_present() -> None:
    def variables(cursor: str | None) -> dict[str, object]:
        body = x.page_body(cursor=cursor, count=20)
        assert isinstance(body["variables"], dict)
        return body["variables"]

    without = variables(None)
    assert "cursor" not in without
    assert without["count"] == 20
    # false is Following, not the "For you" recommendation stream.
    assert without["enableRanking"] is False

    with_cursor = variables("ABC")
    assert with_cursor["cursor"] == "ABC"
    assert {k: v for k, v in with_cursor.items() if k != "cursor"} == without


@pytest.mark.unit
def test_request_targets_the_graphql_operation_with_the_session_headers() -> None:
    seen: list[httpx.Request] = []

    def capture(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": {"home": {}}})

    with client_with(capture) as client:
        x.fetch_page(
            client=client,
            credentials=credentials(),
            query_id="QID123",
            policy=FetchPolicy(accept="application/json"),
        )

    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert "/graphql/QID123/HomeLatestTimeline" in str(request.url)
    assert request.headers["x-csrf-token"] == FAKE_CT0
    assert FAKE_AUTH in request.headers["cookie"]
    assert request.headers["referer"] == "https://x.com/home"


@pytest.mark.unit
def test_count_is_clamped_to_what_x_will_answer() -> None:
    import os

    os.environ[x.COUNT_ENV] = "500"
    assert x.configured_count() == 40
    os.environ[x.COUNT_ENV] = "not-a-number"
    assert x.configured_count() == 20


@pytest.mark.unit
def test_a_caller_supplied_policy_cannot_drop_the_credentials() -> None:
    """Passing a policy to widen a timeout must not send an anonymous request.

    The first version built the credential headers only when no policy was
    given, so a caller that supplied one -- the paging layer setting a longer
    read timeout, say -- would silently issue an unauthenticated request and
    fail as a confusing 403 rather than an obvious missing-session error.
    """
    seen: list[httpx.Request] = []

    def capture(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": {"home": {}}})

    with client_with(capture) as client:
        x.fetch_page(
            client=client,
            credentials=credentials(),
            query_id="QID",
            policy=FetchPolicy(read_timeout=42.0),
        )

    request = seen[0]
    assert FAKE_AUTH in request.headers["cookie"]
    assert request.headers["x-csrf-token"] == FAKE_CT0
    # The caller's own bound survives the merge.
    assert request.headers["accept"] == "application/json"


# --------------------------------------------------------------------------
# Status classification
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, ErrorClass.AUTH),
        (403, ErrorClass.BLOCKED),
        (429, ErrorClass.BLOCKED),
        (500, ErrorClass.NETWORK),
    ],
)
def test_status_codes_keep_the_taxonomy_classification(
    status: int, expected: ErrorClass
) -> None:
    """No special-casing for this origin.

    A 403 means X refused, which is `blocked`; only a failed authentication
    (401) is `auth`. The two have different remedies -- wait versus re-supply
    credentials -- and collapsing them would send the operator the wrong one.
    """
    from adapters.http import FetchError

    with (
        client_with(handler_for(httpx.Response(status))) as client,
        pytest.raises(FetchError) as excinfo,
    ):
        x.fetch_page(
            client=client,
            credentials=credentials(),
            query_id="QID",
            policy=FetchPolicy(accept="application/json"),
        )
    assert excinfo.value.error_class is expected


@pytest.mark.unit
def test_a_400_becomes_a_query_id_recovery_signal() -> None:
    with (
        client_with(handler_for(httpx.Response(400))) as client,
        pytest.raises(x.QueryIdRejected),
    ):
        x.fetch_page(
            client=client,
            credentials=credentials(),
            query_id="STALE",
            policy=FetchPolicy(accept="application/json"),
        )


# --------------------------------------------------------------------------
# queryId recovery
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_query_id_is_recovered_from_the_site_bundle() -> None:
    def route(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://x.com/home":
            return httpx.Response(
                200,
                text='<script src="/assets/main.abc123.js"></script>',
            )
        if url.endswith("/main.abc123.js"):
            return httpx.Response(
                200, text='var x={"HomeLatestTimeline","NEWQUERYID"};'
            )
        return httpx.Response(404)

    with client_with(route) as client:
        assert x.refresh_query_id(client) == "NEWQUERYID"


@pytest.mark.unit
def test_recovery_returns_none_rather_than_inventing_an_id() -> None:
    """A failed discovery keeps the previous id; it is not a credential error."""

    def route(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>no bundle here</html>")

    with client_with(route) as client:
        assert x.refresh_query_id(client) is None


@pytest.mark.unit
def test_unreachable_site_recovers_nothing() -> None:
    def route(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    with client_with(route) as client:
        assert x.refresh_query_id(client) is None


@pytest.mark.unit
def test_configured_query_id_overrides_the_built_in_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(x.QUERY_ID_ENV, raising=False)
    assert x.current_query_id() == x.FALLBACK_QUERY_ID
    monkeypatch.setenv(x.QUERY_ID_ENV, "PINNED")
    assert x.current_query_id() == "PINNED"


# --------------------------------------------------------------------------
# Secret hygiene
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_credentials_never_appear_in_a_fetch_error_detail() -> None:
    """Error details reach the audit log and the JobRun; cookies must not."""
    from adapters.http import FetchError

    with (
        client_with(handler_for(httpx.Response(403))) as client,
        pytest.raises(FetchError) as excinfo,
    ):
        x.fetch_page(
            client=client,
            credentials=credentials(),
            query_id="QID",
            policy=FetchPolicy(accept="application/json"),
        )
    assert FAKE_AUTH not in excinfo.value.detail
    assert FAKE_CT0 not in excinfo.value.detail


@pytest.mark.unit
def test_policy_headers_are_redacted_by_the_audit_allowlist() -> None:
    """The credential-bearing names this client sends are all on the list."""
    from adapters.audit import redact_headers

    sent = credentials().headers()
    redacted = redact_headers(sent)
    for name in ("cookie", "authorization", "x-csrf-token"):
        assert name in sent, name
        assert redacted[name] == "<redacted>", f"{name} would be stored in AuditLog"
