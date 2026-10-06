"""HTTP client for `www.zhihu.com` JSON APIs with cookie + x-zse-96 signing.

Cookies come from a static `PEARSS_ZHIHU_COOKIES` **or** CookieCloud
(the shared `PEARSS_COOKIECLOUD_*` settings, domain `zhihu.com`); static wins
because an operator who pinned a value asked for exactly that value. The
cookie must carry `d_c0` — the signature is bound to it, so a cookie without
one is as useless as no cookie.

The transport goes through `adapters.http.fetch`, so the size cap, timeouts
and header redaction are inherited rather than reimplemented; this module
supplies only the per-request signed headers. A 401 retries once against a
forced CookieCloud refresh (X rotates `ct0` on login; Zhihu rotates session
cookies the same way), mirroring the reference adapter's client — but only
when CookieCloud is the source, because re-reading a pinned static value
would loop. A 403 is `blocked`, not auth: retrying an explicit refusal is
wrong under either reading, so it surfaces as-is.
"""

from __future__ import annotations

import os
from typing import Final

import httpx

from adapters import cookiecloud
from adapters.cookiecloud import parse_cookie_header
from adapters.http import FetchError, FetchPolicy, fetch
from adapters.raw_payload import RawPayload
from core.enums import ErrorClass
from plugins.collectors.zhihu import zse96

BASE: Final = "https://www.zhihu.com"
COOKIE_DOMAIN: Final = "zhihu.com"

# Environment variable *names*, not values: the cookie lives in the
# environment and never in the repository (S105 matches the identifier).
COOKIES_ENV: Final = "PEARSS_ZHIHU_COOKIES"
USER_AGENT_ENV: Final = "PEARSS_ZHIHU_USER_AGENT"

DEFAULT_USER_AGENT: Final = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


class CredentialError(Exception):
    """No usable Zhihu session. Reported as `auth`, never worked around."""


def user_agent() -> str:
    """The UA to present; Zhihu's web API expects a browser-shaped one."""
    return os.environ.get(USER_AGENT_ENV, "").strip() or DEFAULT_USER_AGENT


def cookie_source() -> str:
    """Where the cookie would come from: `static`, `cookie_cloud`, or `none`."""
    if os.environ.get(COOKIES_ENV, "").strip():
        return "static"
    return "cookie_cloud" if cookiecloud.enabled() else "none"


def credentials_configured() -> bool:
    """Whether a signed Zhihu request could run at all.

    CookieCloud counts without probing it: a network call here would run
    before every source's collection, and an unreachable server is reported
    by the resolution that follows.
    """
    return cookie_source() != "none"


def resolve_cookie(client: httpx.Client, *, force: bool = False) -> str:
    """The Cookie header to send: static wins, CookieCloud is the fallback."""
    static = os.environ.get(COOKIES_ENV, "").strip()
    if static:
        return static

    config = cookiecloud.config_from_env(domain=COOKIE_DOMAIN)
    if config is None:
        raise CredentialError(
            f"{COOKIES_ENV} is not set and no CookieCloud server is configured; "
            "Zhihu's web API requires a logged-in cookie containing d_c0"
        )
    try:
        # https only: a `secure` cookie is part of this session, not something
        # to drop the way a plaintext target would require.
        header = cookiecloud.fetch_cookie_header(
            client, config, secure_ok=True, force=force
        )
    except cookiecloud.CookieCloudError as exc:
        raise CredentialError(
            f"CookieCloud did not yield a Zhihu cookie: {exc}"
        ) from exc
    if not header:
        raise CredentialError("CookieCloud returned an empty cookie header")
    return header


def _require_dc0(cookie: str) -> str:
    dc0 = parse_cookie_header(cookie).get("d_c0", "")
    if not dc0:
        raise CredentialError(
            "the Zhihu cookie carries no d_c0; the x-zse-96 signature is bound "
            "to it, so the session is unusable"
        )
    return dc0


def get_json(
    api_path: str,
    *,
    client: httpx.Client,
    source_id: str = "",
    referer: str = "",
    force_refresh: bool = False,
    _retried: bool = False,
) -> RawPayload:
    """GET one signed API call, returning the RawPayload or raising FetchError.

    `api_path` must start with `/api/` and is signed exactly as sent — the
    server verifies the signature against the request target it receives, so
    the percent-encoding decided here is load-bearing.
    """
    if not api_path.startswith("/api/"):
        raise ValueError(f"a Zhihu API path must start with /api/, got {api_path!r}")

    cookie = resolve_cookie(client, force=force_refresh)
    dc0 = _require_dc0(cookie)

    headers = zse96.signed_headers(
        api_path, dc0=dc0, cookie=cookie, user_agent=user_agent()
    )
    headers["x-requested-with"] = "fetch"
    headers["accept"] = "application/json, text/plain, */*"
    if referer:
        headers["referer"] = referer

    policy = FetchPolicy(accept=headers.pop("accept"), extra_headers=headers)
    try:
        return fetch(
            f"{BASE}{api_path}",
            client=client,
            source_id=source_id,
            policy=policy,
        )
    except FetchError as exc:
        # One refresh-and-retry when the session was rejected and a fresher
        # cookie may exist. A second rejection stops: looping on auth would
        # hammer the origin while the real problem is the session itself.
        if (
            exc.error_class is ErrorClass.AUTH
            and not _retried
            and cookie_source() == "cookie_cloud"
        ):
            return get_json(
                api_path,
                client=client,
                source_id=source_id,
                referer=referer,
                force_refresh=True,
                _retried=True,
            )
        raise
