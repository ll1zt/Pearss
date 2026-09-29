"""CookieCloud client: fetch a site's cookies from a self-hosted sync server.

[cookie_cloud_server](https://github.com/ll1zt/cookie_cloud_server) relays
browser cookie backups. Its admin export endpoint returns a ready-made
`Cookie:` header for one domain:

    GET /get/<uuid>?format=header&domain=x.com
    Authorization: Bearer <COOKIE_CLOUD_SERVER_PASSWORD>

This lets a login-type collector (ADR 0012) pick up a refreshed session instead
of pinning a value in the environment that goes stale at the next login.

**What the token actually grants.** `COOKIE_CLOUD_SERVER_PASSWORD` is the
server's master secret: it decrypts and exports *any* uuid's *any* domain. The
`domain=` filter narrows what this call returns, not what the token can ask
for. Anyone holding it can take your banking and mail cookies too, so it is a
wider credential than the single session it fetches, and the deployment should
be scoped accordingly -- a dedicated uuid containing only the sites a reader
needs is better than pointing at a full browser profile.

Consequences taken here: the token is read from the environment only, never
stored, never logged, and never included in an error detail; and a plaintext
http URL that is not loopback logs one warning, because the master secret
crosses the network on every call.
"""

from __future__ import annotations

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

# These are environment variable *names*, not values: the secret lives in the
# environment and never in the repository. S105 matches on an identifier
# containing "token" and cannot tell the two apart.
BASE_URL_ENV = "PEARSS_COOKIECLOUD_URL"
UUID_ENV = "PEARSS_COOKIECLOUD_UUID"
TOKEN_ENV = "PEARSS_COOKIECLOUD_TOKEN"  # noqa: S105
CACHE_TTL_ENV = "PEARSS_COOKIECLOUD_CACHE_SECONDS"

# Short enough that a re-login on the desktop reaches the collector without a
# restart, long enough that a burst of paging does not hammer the server.
DEFAULT_CACHE_SECONDS: Final = 300.0

_LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "::1", "localhost"})

# Guarded by _CACHE_LOCK. A module-level cache rather than instance state so a
# caller can construct a cheap provider per collection and still hit it -- the
# alternative was threading a singleton through the app, which buys nothing
# here because the cache key already includes everything that identifies the
# deployment (base, uuid, domain).
_CACHE: dict[tuple[str, str, str], tuple[str, float]] = {}
_CACHE_LOCK = threading.Lock()


class CookieCloudError(Exception):
    """A CookieCloud fetch that failed, with a message safe to log.

    The message never contains the token or the cookie value: both are
    credentials, and error details reach the JobRun and the audit log.
    """


@dataclass(frozen=True, slots=True, kw_only=True)
class Config:
    """Where to ask, and for which domain."""

    base_url: str
    uuid: str
    token: str
    # No default. Which domain to export belongs to the site being collected,
    # so every caller states it; a shared default would hand one collector
    # another's cookies without saying so.
    domain: str
    cache_seconds: float = DEFAULT_CACHE_SECONDS

    @property
    def cache_key(self) -> tuple[str, str, str]:
        return (self.base_url, self.uuid, self.domain)


def _deployment() -> tuple[str, str, str] | None:
    """`(base, uuid, token)` when a server is configured, else None.

    Split out so `enabled()` can ask "is there a server" without naming a
    domain -- that question is asked before any particular collector is chosen.
    """
    base = os.environ.get(BASE_URL_ENV, "").strip().rstrip("/")
    uuid = os.environ.get(UUID_ENV, "").strip()
    token = os.environ.get(TOKEN_ENV, "").strip()
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
        token=token,
        domain=wanted,
        cache_seconds=max(0.0, ttl),
    )


def missing_config_keys() -> list[str]:
    """Which of the three required settings are absent, for a clear message."""
    return [
        key
        for key in (BASE_URL_ENV, UUID_ENV, TOKEN_ENV)
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
        "CookieCloud master password would be sent over plaintext http to %s; "
        "that password exports every synced domain, not just %s",
        f"{host}:{parts.port or 80}",
        config.domain,
    )


def fetch_cookie_header(
    client: httpx.Client,
    config: Config,
    *,
    force: bool = False,
) -> str:
    """The `Cookie:` header value for `config.domain`.

    Raises CookieCloudError on any failure. Returns the cached value when one
    is fresh, unless `force` -- which is what a caller does after the origin
    rejected the cached session, so a rotated cookie is picked up without a
    restart.
    """
    if not force:
        cached = _cache_get(config.cache_key)
        if cached is not None:
            return cached

    _warn_if_plaintext(config)
    # The uuid goes in a path segment, so it must be percent-encoded: httpx
    # does not escape a "/" inside an already-assembled URL, and an unencoded
    # slash would address a different record (or 404) rather than failing.
    url = f"{config.base_url}/get/{quote(config.uuid, safe='')}"
    try:
        response = client.get(
            url,
            params={"format": "header", "domain": config.domain},
            headers={
                "authorization": f"Bearer {config.token}",
                "accept": "text/plain, application/json, */*",
            },
            timeout=httpx.Timeout(15.0, read=30.0),
        )
    except httpx.HTTPError as exc:
        # The exception repr can contain the URL, which contains the uuid.
        # Not the token -- that is a header -- but the uuid plus a master
        # password is a complete credential, so nothing of it is echoed.
        raise CookieCloudError("CookieCloud server unreachable") from exc

    if response.status_code == 401:
        raise CookieCloudError("CookieCloud rejected the server password")
    if response.status_code == 404:
        raise CookieCloudError("CookieCloud has no record for that uuid")
    if response.status_code == 409:
        raise CookieCloudError(
            "CookieCloud holds no decryptable copy; re-sync from the browser"
        )
    if response.status_code != 200:
        raise CookieCloudError(f"CookieCloud returned HTTP {response.status_code}")

    try:
        body = read_capped(response, MAX_COOKIE_BYTES, str(response.url))
    except FetchError as exc:
        # The module's own error type, so callers keep one thing to catch; the
        # message says it was the size, because that is a different fix.
        raise CookieCloudError(str(exc)) from exc
    cookie = _normalise(body.decode("utf-8", errors="replace"))
    _cache_put(config.cache_key, cookie, config.cache_seconds)
    return cookie


def _normalise(body: str) -> str:
    """Accept a bare header or the JSON wrappers some deployments return."""
    trimmed = body.strip()
    if not trimmed:
        raise CookieCloudError("CookieCloud returned an empty cookie")

    if trimmed.startswith("{"):
        import json

        try:
            payload = json.loads(trimmed)
        except json.JSONDecodeError as exc:
            raise CookieCloudError("CookieCloud returned undecodable JSON") from exc
        for key in ("cookie", "header", "cookies"):
            value = payload.get(key) if isinstance(payload, dict) else None
            if isinstance(value, str) and value.strip():
                trimmed = value.strip()
                break
        else:
            raise CookieCloudError("CookieCloud JSON carried no cookie field")

    if "=" not in trimmed:
        raise CookieCloudError(
            "CookieCloud returned something that is not a cookie header"
        )
    return trimmed


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


def _cache_get(key: tuple[str, str, str]) -> str | None:
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if expires_at <= time.monotonic():
            _CACHE.pop(key, None)
            return None
        return value


def _cache_put(key: tuple[str, str, str], value: str, ttl_seconds: float) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = (value, time.monotonic() + ttl_seconds)


def enabled() -> bool:
    """Whether a CookieCloud-backed source could run at all.

    Takes no domain on purpose: this is a "is a server configured" probe that
    runs before any particular collector has been chosen.
    """
    return _deployment() is not None


__all__ = [
    "BASE_URL_ENV",
    "TOKEN_ENV",
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
