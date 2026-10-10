r"""CookieCloud client: fetch a site's cookies from a self-hosted sync server.

[CookieCloud](https://github.com/easychen/CookieCloud) -- this deployment runs a
Go server implementation of the API; the Elixir one is its frozen predecessor --
relays browser cookie backups. Its admin export endpoint returns the decrypted cookies
for one domain as JSON:

    GET /export/<uuid>?format=raw&domain=x.com
    Authorization: Bearer <ADMIN_TOKEN>

This lets a login-type collector (ADR 0012) pick up a refreshed session instead
of pinning a value in the environment that goes stale at the next login.

**Why `raw` and not `format=header`.** The server escapes the text formats so a
cookie cannot inject a field into whatever reads the export: `;` and `=` become
`\;` and `\=`. Cookie values are frequently base64 and end in `=`, so a header
taken from that format is a corrupt value. `raw` is the programmatic format, and
building the header here is also what lets this module drop expired cookies and
`secure` cookies aimed at a plaintext target.

**What the token grants.** `ADMIN_TOKEN` guards the whole admin surface, not
only this call: whoever holds it can export *any* uuid's *any* domain and can
`DELETE /records/<uuid>`. The `domain=` filter narrows what this call returns,
not what the token can ask for. Scope the deployment accordingly -- a dedicated
uuid holding only the sites a reader needs is better than pointing at a full
browser profile.

Consequences taken here: the token is read from the environment only, never
stored, never logged, and never included in an error detail; and a plaintext
http URL that is not loopback logs one warning, because the admin token crosses
the network on every call.

Three traps worth keeping in view:

1. Do not switch this back to `format=header` -- see above.
2. `GET /get/<uuid>` is not a fallback. It only returns ciphertext, and a uuid
   written by a v2 client has no legacy record at all, so it answers 404.
3. The token can delete records. Use a dedicated uuid.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Final
from urllib.parse import quote, urlsplit

import httpx

from adapters.http import FetchError, read_capped

LOGGER = logging.getLogger(__name__)

# A CookieCloud response is a small JSON document; anything larger is a mistake
# or a bomb (issue #85).
MAX_COOKIE_BYTES = 256 * 1024

# The error body is `{"error":{"code","message"}}` (design/protocol.md §10) and
# is read only to tell two 403s apart. A proxy in the path may answer with
# something else entirely, so this read is capped and its failure is tolerated:
# the status code still classifies every other refusal.
MAX_ERROR_BYTES = 8 * 1024

# These are environment variable *names*, not values: the secret lives in the
# environment and never in the repository. S105 matches on an identifier
# containing "token" and cannot tell the two apart.
BASE_URL_ENV = "PEARSS_COOKIECLOUD_URL"
UUID_ENV = "PEARSS_COOKIECLOUD_UUID"
ADMIN_TOKEN_ENV = "PEARSS_COOKIECLOUD_ADMIN_TOKEN"  # noqa: S105
CACHE_TTL_ENV = "PEARSS_COOKIECLOUD_CACHE_SECONDS"

# Short enough that a re-login on the desktop reaches the collector without a
# restart, long enough that a burst of paging does not hammer the server.
DEFAULT_CACHE_SECONDS: Final = 300.0

_LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "::1", "localhost"})

# Characters a `Cookie:` header cannot carry. A value containing `;` would
# inject another pair into the header, and CR/LF would inject a header line;
# no valid cookie value contains any of them (RFC 6265 cookie-octet).
_UNSAFE_IN_HEADER: Final = frozenset(";\r\n")


class CookieCloudError(Exception):
    """A CookieCloud fetch that failed, with a message safe to log.

    The message never contains the token or the cookie value: both are
    credentials, and error details reach the JobRun and the audit log.
    """


@dataclass(frozen=True, slots=True)
class _Cookie:
    """One cookie as `format=raw` renders it, reduced to what a header needs."""

    name: str
    value: str
    path: str
    secure: bool
    # Epoch seconds, or None for a session cookie (it expires with the browser).
    expires_at: float | None


@dataclass(frozen=True, slots=True, kw_only=True)
class Config:
    """Where to ask, and for which domain."""

    base_url: str
    uuid: str
    admin_token: str
    # No default. Which domain to export belongs to the site being collected,
    # so every caller states it; a shared default would hand one collector
    # another's cookies without saying so.
    domain: str
    cache_seconds: float = DEFAULT_CACHE_SECONDS

    @property
    def cache_key(self) -> tuple[str, str, str]:
        return (self.base_url, self.uuid, self.domain)


# Guarded by _CACHE_LOCK. The parsed cookies are cached rather than the finished
# header: the header depends on the caller's transport (`secure_ok`) and on the
# clock, so caching it would let one caller's policy leak into another's and
# would freeze expiry at fetch time.
#
# A module-level cache rather than instance state so a caller can construct a
# cheap provider per collection and still hit it -- the alternative was threading
# a singleton through the app, which buys nothing here because the cache key
# already includes everything that identifies the deployment (base, uuid,
# domain).
_CACHE: dict[tuple[str, str, str], tuple[tuple[_Cookie, ...], float]] = {}
_CACHE_LOCK = threading.Lock()


def _deployment() -> tuple[str, str, str] | None:
    """`(base, uuid, admin_token)` when a server is configured, else None.

    Split out so `enabled()` can ask "is there a server" without naming a
    domain -- that question is asked before any particular collector is chosen.
    """
    base = os.environ.get(BASE_URL_ENV, "").strip().rstrip("/")
    uuid = os.environ.get(UUID_ENV, "").strip()
    token = os.environ.get(ADMIN_TOKEN_ENV, "").strip()
    if not (base and uuid and token):
        return None
    return base, uuid, token


def config_from_env(*, domain: str) -> Config | None:
    """The provider for `domain`, or None when no server is configured.

    `domain` is a required keyword with no default, because which site to export
    is a property of the collector, not of the deployment: `x.com` for X,
    `zhihu.com` for Zhihu. A single global value -- or a shared default -- makes
    the second login-type collector silently receive the first one's cookies and
    then fail looking for cookie names that are not there.

    Returns None rather than raising when the server is only partly configured:
    an operator who set a URL and nothing else has not enabled this path, and
    failing loudly would break every collection for a feature nobody turned on.
    """
    deployment = _deployment()
    if deployment is None:
        return None
    base, uuid, token = deployment

    wanted = domain.strip()
    if not wanted:
        raise ValueError("a CookieCloud lookup needs the domain it is asking for")

    raw_ttl = os.environ.get(CACHE_TTL_ENV, "").strip()
    try:
        ttl = float(raw_ttl) if raw_ttl else DEFAULT_CACHE_SECONDS
    except ValueError:
        ttl = DEFAULT_CACHE_SECONDS

    return Config(
        base_url=base,
        uuid=uuid,
        admin_token=token,
        domain=wanted,
        cache_seconds=max(0.0, ttl),
    )


def missing_config_keys() -> list[str]:
    """Which of the three required settings are absent, for a clear message."""
    return [
        key
        for key in (BASE_URL_ENV, UUID_ENV, ADMIN_TOKEN_ENV)
        if not os.environ.get(key, "").strip()
    ]


def _warn_if_plaintext(config: Config) -> None:
    """One warning per plaintext non-loopback target, never the secret."""
    parts = urlsplit(config.base_url)
    if parts.scheme == "https":
        return
    host = (parts.hostname or "").lower()
    if host in _LOOPBACK_HOSTS:
        return
    LOGGER.warning(
        "CookieCloud admin token would be sent over plaintext http to %s; "
        "that token exports every synced domain and can delete any uuid's "
        "records, not just %s",
        f"{host}:{parts.port or 80}",
        config.domain,
    )


def fetch_cookie_header(
    client: httpx.Client,
    config: Config,
    *,
    secure_ok: bool,
    force: bool = False,
) -> str:
    """The `Cookie:` header value for `config.domain`.

    `secure_ok` says whether the request this header will be attached to
    travels over https. A browser does not send a `secure` cookie over http, so
    neither does this: pass False for a plaintext target and those cookies are
    dropped rather than leaked onto the wire. It is required rather than
    defaulted because the caller is the only one that knows the target's scheme,
    and defaulting to True would silently send `secure` cookies in cleartext.

    Raises CookieCloudError on any failure. Returns the cached value when one
    is fresh, unless `force` -- which is what a caller does after the origin
    rejected the cached session, so a rotated cookie is picked up without a
    restart.
    """
    if not force:
        cached = _cache_get(config.cache_key)
        if cached is not None:
            return _cookie_header(cached, secure_ok=secure_ok, now=time.time())

    _warn_if_plaintext(config)
    # The uuid goes in a path segment, so it must be percent-encoded: httpx
    # does not escape a "/" inside an already-assembled URL, and an unencoded
    # slash would address a different record (or 404) rather than failing.
    url = f"{config.base_url}/export/{quote(config.uuid, safe='')}"
    try:
        response = client.get(
            url,
            params={"format": "raw", "domain": config.domain},
            headers={
                "authorization": f"Bearer {config.admin_token}",
                "accept": "application/json",
            },
            timeout=httpx.Timeout(15.0, read=30.0),
        )
    except httpx.HTTPError as exc:
        # The exception repr can contain the URL, which contains the uuid.
        # Not the token -- that is a header -- but the uuid plus an admin token
        # is a complete credential, so nothing of it is echoed.
        raise CookieCloudError("CookieCloud server unreachable") from exc

    if response.status_code != 200:
        raise _error_for(response)

    try:
        body = read_capped(response, MAX_COOKIE_BYTES, str(response.url))
    except FetchError as exc:
        # The module's own error type, so callers keep one thing to catch; the
        # message says it was the size, because that is a different fix.
        raise CookieCloudError(str(exc)) from exc

    cookies = _parse_cookies(body)
    _cache_put(config.cache_key, cookies, config.cache_seconds)
    return _cookie_header(cookies, secure_ok=secure_ok, now=time.time())


def _error_for(response: httpx.Response) -> CookieCloudError:
    """The refusal, phrased as the action that fixes it.

    The code alone is not enough: 403 covers both "the token is wrong" and "the
    server has export disabled", which are two different fixes, and sending
    both readers the same instruction sends half of them the wrong way. So the
    server's message is *read* to tell them apart -- and never relayed, because
    it is untrusted input and because this text reaches JobRun and the audit
    log. For the same reason none of the messages below carries the token or
    the uuid.
    """
    status = response.status_code
    if status == 401:
        return CookieCloudError(
            "CookieCloud rejected the request: no admin token was sent "
            f"(set {ADMIN_TOKEN_ENV})"
        )
    if status == 403:
        reported = _server_message(response)
        if "disabled" in reported or "not set" in reported:
            return CookieCloudError(
                "CookieCloud has export disabled: the server has no ADMIN_TOKEN "
                "configured, so /export is off for every caller"
            )
        return CookieCloudError(
            "CookieCloud rejected the admin token; check "
            f"{ADMIN_TOKEN_ENV} against the server's ADMIN_TOKEN"
        )
    if status == 404:
        return CookieCloudError(
            "CookieCloud has no record for that uuid; it has not been synced "
            "from the browser yet"
        )
    if status == 409:
        return CookieCloudError(
            "CookieCloud holds no decryptable copy; re-sync from the browser"
        )
    if status == 429:
        return CookieCloudError(
            "CookieCloud is rate limiting this client; retry once the cached "
            "cookie expires"
        )
    return CookieCloudError(f"CookieCloud returned HTTP {status}")


def _server_message(response: httpx.Response) -> str:
    """The server's `error.message`, lowercased, or "" when there is none."""
    try:
        body = read_capped(response, MAX_ERROR_BYTES, str(response.url))
    except FetchError:
        return ""
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if not isinstance(error, dict):
        return ""
    message = error.get("message")
    return message.lower() if isinstance(message, str) else ""


def _parse_cookies(body: bytes) -> tuple[_Cookie, ...]:
    """The `format=raw` body as cookies, or CookieCloudError.

    `raw` is a flat JSON array; the server has already merged the per-domain
    shards and applied deletions, so nothing here dedupes or decrypts.
    Elements that are not shaped like a cookie are skipped: the server owns the
    element kind, and one odd entry must not poison the whole header. An empty
    result is an error rather than an empty header, because an empty header
    reads downstream as "the site has no session" -- a different fix.
    """
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise CookieCloudError("CookieCloud returned undecodable JSON") from exc
    if not isinstance(payload, list):
        raise CookieCloudError("CookieCloud did not return a list of cookies")

    parsed = tuple(
        cookie for cookie in (_as_cookie(element) for element in payload) if cookie
    )
    if not parsed:
        raise CookieCloudError("CookieCloud returned no cookies for that domain")
    return parsed


def _as_cookie(element: object) -> _Cookie | None:
    """One `raw` element as a cookie, or None when it is not one."""
    if not isinstance(element, dict):
        return None
    name = element.get("name")
    if not isinstance(name, str) or not name:
        return None

    value = element.get("value")
    path = element.get("path")
    expiry = element.get("expirationDate")
    return _Cookie(
        name=name,
        value=value if isinstance(value, str) else "",
        path=path if isinstance(path, str) and path else "/",
        secure=element.get("secure") is True,
        # A bool is an int in Python; `true` here would be a JSON type error,
        # not an expiry, so it is treated as "no expiry" like a missing field.
        expires_at=(
            float(expiry)
            if isinstance(expiry, int | float) and not isinstance(expiry, bool)
            else None
        ),
    )


def _cookie_header(cookies: tuple[_Cookie, ...], *, secure_ok: bool, now: float) -> str:
    """Join the cookies that may travel, one value per name.

    Order is the server's (it sorts by domain, then name); re-sorting here would
    be a second ordering rule to keep in sync, and the origin sees this order.

    A name that appears more than once keeps its narrowest path, which is the
    scope a browser would send for the site's own pages, and the later entry on
    equal paths -- matching `parse_cookie_header`.
    """
    live = [
        cookie
        for cookie in cookies
        if (cookie.expires_at is None or cookie.expires_at > now)
        and (secure_ok or not cookie.secure)
        and _header_safe(cookie)
    ]
    if not live:
        raise CookieCloudError(
            "every cookie CookieCloud holds for that domain is expired, or "
            "secure-only for a plaintext target"
        )

    best: dict[str, _Cookie] = {}
    for cookie in live:
        current = best.get(cookie.name)
        if current is None or len(cookie.path) >= len(current.path):
            best[cookie.name] = cookie
    return "; ".join(f"{cookie.name}={cookie.value}" for cookie in best.values())


def _header_safe(cookie: _Cookie) -> bool:
    """Whether this cookie can be written into a header without breaking it."""
    return not (
        _UNSAFE_IN_HEADER.intersection(cookie.name)
        or _UNSAFE_IN_HEADER.intersection(cookie.value)
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in cookie.name)
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in cookie.value)
    )


def parse_cookie_header(header: str) -> dict[str, str]:
    """Split a `Cookie:` value into name/value pairs.

    Last occurrence wins, matching what a browser sends when a name is scoped
    twice: the one that actually reaches the origin is the later one.
    """
    pairs: dict[str, str] = {}
    for chunk in header.split(";"):
        name, sep, value = chunk.strip().partition("=")
        if sep and name:
            pairs[name] = value.strip()
    return pairs


def invalidate(config: Config | None = None) -> None:
    """Drop cached cookies, or just one deployment's."""
    with _CACHE_LOCK:
        if config is None:
            _CACHE.clear()
        else:
            _CACHE.pop(config.cache_key, None)


def _cache_get(key: tuple[str, str, str]) -> tuple[_Cookie, ...] | None:
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if expires_at <= time.monotonic():
            _CACHE.pop(key, None)
            return None
        return value


def _cache_put(
    key: tuple[str, str, str], value: tuple[_Cookie, ...], ttl_seconds: float
) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = (value, time.monotonic() + ttl_seconds)


def enabled() -> bool:
    """Whether a CookieCloud-backed source could run at all.

    Takes no domain on purpose: this is a "is a server configured" probe that
    runs before any particular collector has been chosen.
    """
    return _deployment() is not None


__all__ = [
    "ADMIN_TOKEN_ENV",
    "BASE_URL_ENV",
    "UUID_ENV",
    "Config",
    "CookieCloudError",
    "config_from_env",
    "enabled",
    "fetch_cookie_header",
    "invalidate",
    "missing_config_keys",
    "parse_cookie_header",
]
