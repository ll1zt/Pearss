"""HTTP fetch adapter.

Implements the rules in docs/guides/collector-guide.md: bounded timeouts, a
hard response-size cap, conditional requests, and a contactable User-Agent.
It knows nothing about feeds or items — it returns a `RawPayload` and lets the
collector decide what it means.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import socket
from collections.abc import Mapping
from dataclasses import dataclass, field
from time import monotonic
from typing import Final
from urllib.parse import urljoin, urlsplit

import httpx

from adapters import deadline_transport
from adapters.raw_payload import ConditionalState, RawPayload

# Re-exported: fetch() takes one, so it is part of this module's public surface
# even though the type lives in raw_payload to keep the repository off httpx.
__all__ = [
    "ALLOW_PRIVATE_FETCH_ENV",
    "ConditionalState",
    "FetchError",
    "FetchPolicy",
    "check_destination",
    "fetch",
    "post_json",
]
from core.enums import ErrorClass
from core.errors import classify

LOGGER = logging.getLogger(__name__)

# Read/connect/write are bounded separately, plus a total budget. An unbounded
# read is the classic way a single slow origin exhausts a worker pool.
#
# CONNECT covers the TCP handshake *and* the TLS handshake, so it must be
# generous enough for a slow handshake. 5 seconds proved too tight in
# practice: roughly a third of requests to hnrss.org failed with a handshake
# timeout, and because each failure burns the whole window, raising it made a
# batch finish faster as well as reliably (16.4s -> 12.9s over eight
# requests). A generous connect timeout is not a lax one: the read timeout is
# what bounds a slow origin once the connection already exists.
DEFAULT_CONNECT_TIMEOUT = 15.0
DEFAULT_READ_TIMEOUT = 15.0
DEFAULT_WRITE_TIMEOUT = 15.0
DEFAULT_TOTAL_TIMEOUT = 60.0

# 5 MB. Feeds are small; this is a guard against a misconfigured origin or a
# hostile response, not a limit anyone should routinely hit.
DEFAULT_MAX_BYTES = 5 * 1024 * 1024

# A contactable agent, per the collector guide. Overridable so a deployment can
# supply its own contact address.
DEFAULT_USER_AGENT = "Pearss/0.1 (+https://github.com/pearss/pearss; RSS reader)"

# ADR 0016: on a public deployment a leaked write token must not buy the
# attacker a probe of the server's own network (cloud metadata, RFC1918,
# loopback). Fetches to private addresses are therefore refused by default;
# an operator who legitimately subscribes to internal sources can opt out.
ALLOW_PRIVATE_FETCH_ENV: Final = "PEARSS_ALLOW_PRIVATE_FETCH"
MAX_MANUAL_REDIRECT_HOPS: Final = 5

# Headers that must not follow a cross-host redirect: the redirect target is
# chosen by whatever sent the 3xx, and these authenticate us to the *previous*
# host only.
# httpx 0.28's `is_redirect` also matches 304 and location-less 302s, which
# would misroute a legitimate not-modified answer into the redirect loop.
REDIRECT_STATUSES: Final[frozenset[int]] = frozenset({301, 302, 303, 307, 308})

_CREDENTIAL_HEADERS: Final[frozenset[str]] = frozenset(
    {"cookie", "authorization", "x-csrf-token", "proxy-authorization"}
)


def allow_private_fetch() -> bool:
    """Whether private-address targets are permitted (default: refused)."""
    return os.environ.get(ALLOW_PRIVATE_FETCH_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


# A test-only override for `resolve_host`'s answer, expressed as JSON
# (`{"host": ["1.2.3.4"]}`) so a sandboxed collector child can be made hermetic
# too. The root conftest monkeypatches `resolve_host` directly, which only
# reaches the in-process callers: a child is a separate interpreter that
# resolves for itself, so before this existed the child answered real DNS and
# refused every sandboxed collector's fixture host as a private address.
#
# A dedicated variable rather than PEARSS_ALLOW_PRIVATE_FETCH: that switch means
# "this deployment serves internal sources", and using it here would turn the
# guard off instead of pinning the answer, leaving the SSRF tests unable to
# distinguish "allowed" from "resolved elsewhere".
TEST_DNS_ENV: Final = "PEARSS_TEST_DNS"


def resolve_host(host: str) -> set[str]:
    """Every address `host` resolves to; empty means unresolvable.

    Module-level so tests can substitute a hermetic answer — the guard must
    not make the test suite depend on real DNS. `PEARSS_TEST_DNS` extends that
    substitution to sandboxed children, which cannot be monkeypatched.
    """
    override = os.environ.get(TEST_DNS_ENV, "")
    if override:
        try:
            table = json.loads(override)
        except ValueError:
            LOGGER.warning(
                "%s is not valid JSON; falling back to real DNS", TEST_DNS_ENV
            )
        else:
            if isinstance(table, dict):
                # An exact entry wins; "*" is the fallback for every other host,
                # which is what lets one variable make a whole child hermetic.
                addresses = table.get(host, table.get("*"))
                if isinstance(addresses, list):
                    return {str(address) for address in addresses}
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, OSError):
        return set()
    return {str(info[4][0]) for info in infos}


def _is_private_ip(address_text: str) -> bool:
    try:
        address = ipaddress.ip_address(address_text)
    except ValueError:
        return True  # an unparseable answer is not one we can vouch for
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def _resolves_private(url: str) -> bool:
    """Whether `url`'s host is (or resolves to) a private address.

    Fail-closed on resolution errors: a name the server cannot resolve is not
    one it should fetch, and DNS is exactly the layer an SSRF chain plays
    with. Known limitation, recorded in ADR 0016: the check resolves once and
    the connection re-resolves, so textbook DNS rebinding remains possible;
    the threat here is a leaked write token probing an internal network, not
    a hostile resolver racing the socket.
    """
    host = urlsplit(url).hostname
    if not host:
        return True
    addresses = resolve_host(host)
    if not addresses:
        return True  # unresolvable → refused, fail-closed
    return any(_is_private_ip(address) for address in addresses)


def _strip_credentials(headers: dict[str, str]) -> dict[str, str]:
    return {
        name: value
        for name, value in headers.items()
        if name not in _CREDENTIAL_HEADERS
    }


class FetchError(Exception):
    """A fetch that failed, carrying its ErrorClass.

    Raised rather than returned so a caller cannot ignore the classification
    and treat a failure as usable content.
    """

    def __init__(self, error_class: ErrorClass, detail: str) -> None:
        super().__init__(f"{error_class.value}: {detail}")
        self.error_class = error_class
        self.detail = detail


# Headers httpx owns. A policy may add browser-shaped headers, but overriding
# these would leave the headers contradicting the request actually sent.
_TRANSPORT_HEADERS: Final[frozenset[str]] = frozenset(
    {"content-length", "host", "transfer-encoding", "connection"}
)


@dataclass(frozen=True, slots=True, kw_only=True)
class FetchPolicy:
    """Bounds applied to a fetch."""

    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    read_timeout: float = DEFAULT_READ_TIMEOUT
    write_timeout: float = DEFAULT_WRITE_TIMEOUT
    total_timeout: float = DEFAULT_TOTAL_TIMEOUT
    max_bytes: int = DEFAULT_MAX_BYTES
    user_agent: str = DEFAULT_USER_AGENT
    # Sent after the defaults, verbatim. Some origins only answer a request
    # shaped like a browser's. These are treated as potentially secret: they
    # are never logged, and the names that carry credentials are on the audit
    # redaction list.
    extra_headers: Mapping[str, str] = field(default_factory=dict)
    # Overrides the feed-oriented accept header, for JSON endpoints.
    accept: str | None = None


def fetch(
    url: str,
    *,
    client: httpx.Client,
    source_id: str = "",
    raw_fetch_id: str | None = None,
    policy: FetchPolicy | None = None,
    conditional: ConditionalState | None = None,
    correlation_id: str = "",
) -> RawPayload:
    """GET `url`, returning a RawPayload or raising FetchError."""
    return _request(
        "GET",
        url,
        client=client,
        source_id=source_id,
        raw_fetch_id=raw_fetch_id,
        policy=policy,
        conditional=conditional,
        correlation_id=correlation_id,
    )


def post_json(
    url: str,
    *,
    body: Mapping[str, object],
    client: httpx.Client,
    source_id: str = "",
    raw_fetch_id: str | None = None,
    policy: FetchPolicy | None = None,
    correlation_id: str = "",
) -> RawPayload:
    """POST `body` as JSON, under the same bounds as a GET.

    Shares `_request` with `fetch` rather than reimplementing it, so a caller
    that needs a POST cannot accidentally opt out of the timeout and size cap.
    Conditional state is not offered: an origin does not hand out an ETag for a
    request with a body, and pretending otherwise would imply a cache that does
    not exist.
    """
    return _request(
        "POST",
        url,
        client=client,
        source_id=source_id,
        raw_fetch_id=raw_fetch_id,
        policy=policy,
        correlation_id=correlation_id,
        json_body=dict(body),
        follow_redirects=False,
    )


def _request(
    method: str,
    url: str,
    *,
    client: httpx.Client,
    source_id: str,
    raw_fetch_id: str | None,
    policy: FetchPolicy | None,
    correlation_id: str,
    conditional: ConditionalState | None = None,
    json_body: dict[str, object] | None = None,
    follow_redirects: bool = True,
) -> RawPayload:
    """Fetch `url`, returning a RawPayload or raising FetchError.

    `client` is injected rather than created here so it can be shared across
    requests: rebuilding a client per call re-does the TLS handshake and
    discards the connection pool.
    """
    limits = policy or FetchPolicy()
    timeout = httpx.Timeout(
        connect=limits.connect_timeout,
        read=limits.read_timeout,
        write=limits.write_timeout,
        # pool is how long a request waits for a free connection, not how long
        # a connection takes to establish: reusing connect_timeout meant the
        # very first request (empty pool) gave up mid-TLS-handshake on a slow
        # origin, and concurrent fetches past the pool limit failed after 5s
        # even though nothing was broken. It gets the total budget instead, so
        # a busy pool queues rather than erroring.
        pool=limits.total_timeout,
    )

    headers = _request_headers(limits, conditional, json_body=json_body is not None)
    deadline = monotonic() + limits.total_timeout

    # The guard runs before the socket: an unresolvable or private target is
    # refused without spending a connection. When the guard is active,
    # redirects are followed manually so every hop is classified too — the
    # classic SSRF bypass is a public URL that 302s to the metadata service.
    guard_private = not allow_private_fetch()
    if guard_private and _resolves_private(url):
        raise FetchError(
            ErrorClass.BLOCKED,
            f"private-address fetch refused for {_redact(url)}",
        )
    manual_redirects = guard_private and follow_redirects
    hops = 0

    try:
        while True:
            extensions = {deadline_transport.DEADLINE_EXTENSION: deadline}
            # Streaming so an oversized response is abandoned mid-body instead
            # of being buffered in full before the cap can be applied.
            with client.stream(
                method,
                url,
                timeout=timeout,
                headers=headers,
                follow_redirects=follow_redirects and not manual_redirects,
                json=json_body,
                extensions=extensions,
            ) as response:
                status = response.status_code

                if manual_redirects and status in REDIRECT_STATUSES:
                    hops += 1
                    if hops > MAX_MANUAL_REDIRECT_HOPS:
                        raise FetchError(
                            ErrorClass.NETWORK,
                            f"redirect loop past {MAX_MANUAL_REDIRECT_HOPS} "
                            f"hops for {_redact(url)}",
                        )
                    location = response.headers.get("location")
                    if not location:
                        raise FetchError(
                            ErrorClass.NETWORK,
                            f"redirect without location for {_redact(url)}",
                        )
                    next_url = urljoin(url, location)
                    if _resolves_private(next_url):
                        raise FetchError(
                            ErrorClass.BLOCKED,
                            "redirect to a private address refused for "
                            f"{_redact(next_url)}",
                        )
                    if urlsplit(next_url).hostname != urlsplit(url).hostname:
                        # The new host was chosen by the old host; credentials
                        # are for the old host and must not travel.
                        headers = _strip_credentials(headers)
                    url = next_url
                    continue

                if status == 304:
                    # Not modified: a legitimate outcome, not an error. The
                    # caller records it and skips parsing.
                    return RawPayload(
                        url=url,
                        status_code=status,
                        body=b"",
                        source_id=source_id,
                        raw_fetch_id=raw_fetch_id,
                        headers=_safe_headers(response.headers),
                        not_modified=True,
                        etag=response.headers.get("etag"),
                        last_modified=response.headers.get("last-modified"),
                        correlation_id=correlation_id,
                    )

                if status != 200:
                    error_class = classify(http_status=status) or ErrorClass.NETWORK
                    raise FetchError(error_class, f"HTTP {status} for {_redact(url)}")

                body = _read_capped(response, limits.max_bytes, url)
                response_headers = _safe_headers(response.headers)
                etag = response.headers.get("etag")
                last_modified = response.headers.get("last-modified")
                break

    except FetchError:
        raise
    except httpx.TimeoutException as exc:
        raise FetchError(ErrorClass.NETWORK, f"timeout: {exc!r}") from exc
    except httpx.TooManyRedirects as exc:
        raise FetchError(ErrorClass.NETWORK, f"redirect loop: {exc!r}") from exc
    except httpx.HTTPError as exc:
        # Covers connect errors, TLS failures and protocol errors.
        raise FetchError(ErrorClass.NETWORK, f"transport: {exc!r}") from exc

    return RawPayload(
        url=url,
        status_code=status,
        body=body,
        source_id=source_id,
        raw_fetch_id=raw_fetch_id,
        headers=response_headers,
        etag=etag,
        last_modified=last_modified,
        correlation_id=correlation_id,
    )


def check_destination(url: str) -> None:
    """Refuse unsupported/private HTTP destinations before host egress."""
    if urlsplit(url).scheme not in {"http", "https"}:
        raise FetchError(ErrorClass.BLOCKED, "non-HTTP destination refused")
    if not allow_private_fetch() and _resolves_private(url):
        raise FetchError(ErrorClass.BLOCKED, "private-address fetch refused")


def _request_headers(
    limits: FetchPolicy,
    conditional: ConditionalState | None,
    *,
    json_body: bool = False,
) -> dict[str, str]:
    """Headers for a request: defaults, then conditional state, then policy.

    The policy's extras go last so a source that must look like a browser can
    set its own `accept` or `referer`, but never its `content-length` or host:
    those belong to the transport and httpx sets them.
    """
    headers = {
        "user-agent": limits.user_agent,
        "accept": limits.accept or _accept_header(),
    }
    if json_body:
        headers["content-type"] = "application/json"
    prior = conditional or ConditionalState()
    if prior.etag:
        headers["if-none-match"] = prior.etag
    if prior.last_modified:
        headers["if-modified-since"] = prior.last_modified
    for name, value in limits.extra_headers.items():
        lowered = name.lower()
        if lowered in _TRANSPORT_HEADERS:
            # Left to httpx: a hand-set value here would contradict the request
            # the transport is actually about to send.
            continue
        headers[lowered] = value
    return headers


def _accept_header() -> str:
    """What we ask for, in preference order."""
    return (
        "application/rss+xml, application/atom+xml, application/xml, "
        "text/xml, application/feed+json, */*;q=0.1"
    )


def _read_capped(response: httpx.Response, max_bytes: int, url: str) -> bytes:
    """Read a response body, refusing to exceed `max_bytes`."""
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        total += len(chunk)
        if total > max_bytes:
            raise FetchError(
                ErrorClass.NETWORK,
                f"response exceeded {max_bytes} bytes for {_redact(url)}",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _safe_headers(headers: httpx.Headers) -> dict[str, str]:
    """Keep only headers we may store and log.

    Everything else is dropped rather than filtered by a deny-list: a
    deny-list has to be right about every header an origin might send, whereas
    an allow-list is right by construction. Set-Cookie and Authorization
    especially must never reach RawFetch or a fixture.
    """
    allowed = (
        "content-type",
        "content-length",
        "etag",
        "last-modified",
        "retry-after",
        "date",
    )
    return {key: headers[key] for key in allowed if key in headers}


def _redact(url: str) -> str:
    """Strip the query before a URL reaches a log.

    Feed URLs routinely carry tokens in their query string, and AGENTS.md
    forbids writing private URLs to logs.
    """
    return url.split("?", 1)[0]
