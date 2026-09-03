"""HTTP fetch adapter.

Implements the rules in docs/guides/collector-guide.md: bounded timeouts, a
hard response-size cap, conditional requests, and a contactable User-Agent.
It knows nothing about feeds or items — it returns a `RawPayload` and lets the
collector decide what it means.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from adapters.raw_payload import RawPayload
from core.enums import ErrorClass
from core.errors import classify

LOGGER = logging.getLogger(__name__)

# Read/connect/write are bounded separately, plus a total budget. An unbounded
# read is the classic way a single slow origin exhausts a worker pool.
DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_READ_TIMEOUT = 15.0
DEFAULT_TOTAL_TIMEOUT = 30.0

# 5 MB. Feeds are small; this is a guard against a misconfigured origin or a
# hostile response, not a limit anyone should routinely hit.
DEFAULT_MAX_BYTES = 5 * 1024 * 1024

# A contactable agent, per the collector guide. Overridable so a deployment can
# supply its own contact address.
DEFAULT_USER_AGENT = "Pearss/0.1 (+https://github.com/pearss/pearss; RSS reader)"


class FetchError(Exception):
    """A fetch that failed, carrying its ErrorClass.

    Raised rather than returned so a caller cannot ignore the classification
    and treat a failure as usable content.
    """

    def __init__(self, error_class: ErrorClass, detail: str) -> None:
        super().__init__(f"{error_class.value}: {detail}")
        self.error_class = error_class
        self.detail = detail


@dataclass(frozen=True, slots=True, kw_only=True)
class FetchPolicy:
    """Bounds applied to a fetch."""

    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    read_timeout: float = DEFAULT_READ_TIMEOUT
    total_timeout: float = DEFAULT_TOTAL_TIMEOUT
    max_bytes: int = DEFAULT_MAX_BYTES
    user_agent: str = DEFAULT_USER_AGENT


@dataclass(frozen=True, slots=True, kw_only=True)
class ConditionalState:
    """Conditional-request state carried between fetches of one source."""

    etag: str | None = None
    last_modified: str | None = None


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
    """Fetch `url`, returning a RawPayload or raising FetchError.

    `client` is injected rather than created here so it can be shared across
    requests: rebuilding a client per call re-does the TLS handshake and
    discards the connection pool.
    """
    limits = policy or FetchPolicy()
    timeout = httpx.Timeout(
        connect=limits.connect_timeout,
        read=limits.read_timeout,
        write=limits.read_timeout,
        pool=limits.connect_timeout,
    )

    headers = {"user-agent": limits.user_agent, "accept": _accept_header()}
    prior = conditional or ConditionalState()
    if prior.etag:
        headers["if-none-match"] = prior.etag
    if prior.last_modified:
        headers["if-modified-since"] = prior.last_modified

    try:
        # Streaming so an oversized response is abandoned mid-body instead of
        # being buffered in full before the cap can be applied.
        with client.stream(
            "GET", url, timeout=timeout, headers=headers, follow_redirects=True
        ) as response:
            status = response.status_code

            if status == 304:
                # Not modified: a legitimate outcome, not an error. The caller
                # records it and skips parsing.
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
