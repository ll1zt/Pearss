"""HTTP client for `https://api.zsxq.com/v2` with the zsxq session cookie.

No signing here — unlike Zhihu, the API authenticates with the
`zsxq_access_token` cookie alone. What this module adds over the plain
adapter is the response envelope and the credential policy:

- every successful payload is wrapped as `{"succeeded": true, "resp_data":
  …}`; the wrapper is unwrapped here so collectors see `resp_data` directly;
- business code `1059` (risk-control rate limiting) retries up to three
  times with a growing pause, mirroring the reference adapter and RSSHub;
- the cookie comes from a static `PEARSS_ZSXQ_COOKIE` (or bare
  `PEARSS_ZSXQ_ACCESS_TOKEN`) or the shared CookieCloud (`zsxq.com` domain),
  static first, per ADR 0012. Whatever the source, the cookie must carry
  `zsxq_access_token` or the session is unusable;
- a 401 retries once against a forced CookieCloud refresh; a 403 is
  `blocked` and is never retried (same discipline as the Zhihu client).

Cookies live in the environment and memory only — never in the database,
fixtures or logs (the audit redact list already covers the `cookie` header).
"""

from __future__ import annotations

import json
import os
import time
from typing import Final

import httpx

from adapters import cookiecloud
from adapters.cookiecloud import parse_cookie_header
from adapters.http import FetchError, FetchPolicy, fetch
from adapters.raw_payload import RawPayload
from core.enums import ErrorClass

BASE: Final = "https://api.zsxq.com/v2"
COOKIE_DOMAIN: Final = "zsxq.com"
API_REFERER: Final = "https://wx.zsxq.com/"

# Environment variable *names*, not values: the token lives in the
# environment and never in the repository (S105 matches the identifier).
COOKIE_ENV: Final = "PEARSS_ZSXQ_COOKIE"
TOKEN_ENV: Final = "PEARSS_ZSXQ_ACCESS_TOKEN"  # noqa: S105
USER_AGENT_ENV: Final = "PEARSS_ZSXQ_USER_AGENT"

DEFAULT_USER_AGENT: Final = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# Risk-control code 1059: retry with a growing pause before giving up,
# exactly as the reference adapter and RSSHub do.
MAX_1059_RETRIES: Final = 3
_RETRY_PAUSE_BASE_SECONDS: Final = 0.15

# The cookie name is a wire-format fact, not a secret.
TOKEN_COOKIE_NAME: Final = "zsxq_access_token"  # noqa: S105


class CredentialError(Exception):
    """No usable zsxq session. Reported as `auth`, never worked around."""


def user_agent() -> str:
    return os.environ.get(USER_AGENT_ENV, "").strip() or DEFAULT_USER_AGENT


def cookie_source() -> str:
    """Where the cookie would come from: `static`, `cookie_cloud`, or `none`."""
    if os.environ.get(COOKIE_ENV, "").strip() or os.environ.get(TOKEN_ENV, "").strip():
        return "static"
    return "cookie_cloud" if cookiecloud.enabled() else "none"


def credentials_configured() -> bool:
    """Whether a zsxq request could run at all (no network probe)."""
    return cookie_source() != "none"


def static_cookie_header() -> str | None:
    """The cookie from static env only: full header wins over a bare token."""
    cookie = os.environ.get(COOKIE_ENV, "").strip()
    if cookie:
        return cookie
    token = os.environ.get(TOKEN_ENV, "").strip()
    if token:
        return f"{TOKEN_COOKIE_NAME}={token};"
    return None


def resolve_cookie(client: httpx.Client, *, force: bool = False) -> str:
    """The Cookie header to send: static first, CookieCloud as fallback."""
    static = static_cookie_header()
    if static is not None:
        _require_token(static)
        return static

    config = cookiecloud.config_from_env(domain=COOKIE_DOMAIN)
    if config is None:
        raise CredentialError(
            f"{COOKIE_ENV} or {TOKEN_ENV} is not set and no CookieCloud server "
            f"is configured; the zsxq API requires a {TOKEN_COOKIE_NAME} cookie"
        )
    try:
        header = cookiecloud.fetch_cookie_header(client, config, force=force)
    except cookiecloud.CookieCloudError as exc:
        raise CredentialError(
            f"CookieCloud did not yield a zsxq cookie: {exc}"
        ) from exc
    _require_token(header)
    return header


def _require_token(cookie: str) -> None:
    if not parse_cookie_header(cookie).get(TOKEN_COOKIE_NAME):
        raise CredentialError(
            f"the zsxq cookie carries no {TOKEN_COOKIE_NAME}; the API is "
            "unusable without it"
        )


def get_json(
    path: str,
    *,
    client: httpx.Client,
    source_id: str = "",
    force_refresh: bool = False,
    _retried: bool = False,
    _business_retries: int = 0,
) -> RawPayload:
    """GET one API call and return a payload carrying `resp_data`.

    The hook contract is FetchError: transport and HTTP failures come from
    the plain adapter already classified; envelope failures are classified
    here (`PARSE` for garbage, `BLOCKED` for business refusals, with 1059
    retried upstream-style before giving up).
    """
    if not path.startswith("/"):
        raise ValueError(f"an API path must start with /, got {path!r}")

    cookie = resolve_cookie(client, force=force_refresh)
    policy = FetchPolicy(
        accept="application/json, text/plain, */*",
        user_agent=user_agent(),
        extra_headers={"cookie": cookie, "referer": API_REFERER},
    )
    try:
        raw = fetch(
            f"{BASE}{path}",
            client=client,
            source_id=source_id,
            policy=policy,
        )
    except FetchError as exc:
        # One refresh-and-retry when the session was rejected and a fresher
        # cookie may exist (CookieCloud source only); 403-classified refusals
        # are not auth and are never retried.
        if (
            exc.error_class is ErrorClass.AUTH
            and not _retried
            and cookie_source() == "cookie_cloud"
        ):
            return get_json(
                path,
                client=client,
                source_id=source_id,
                force_refresh=True,
                _retried=True,
                _business_retries=_business_retries,
            )
        raise

    try:
        envelope = json.loads(raw.text())
    except ValueError as exc:
        raise FetchError(ErrorClass.PARSE, f"undecodable zsxq envelope: {exc}") from exc
    if not isinstance(envelope, dict):
        raise FetchError(ErrorClass.PARSE, "zsxq envelope is not a JSON object")

    if envelope.get("succeeded") is True:
        resp_data = envelope.get("resp_data")
        body = json.dumps(resp_data if isinstance(resp_data, dict) else {}).encode()
        return RawPayload(url=raw.url, status_code=raw.status_code, body=body)

    if envelope.get("code") == 1059 and _business_retries < MAX_1059_RETRIES:
        _pause(_RETRY_PAUSE_BASE_SECONDS * (_business_retries + 1))
        return get_json(
            path,
            client=client,
            source_id=source_id,
            force_refresh=force_refresh,
            _retried=_retried,
            _business_retries=_business_retries + 1,
        )

    if envelope.get("succeeded") is False:
        info = envelope.get("info") or envelope.get("error") or ""
        raise FetchError(
            ErrorClass.BLOCKED,
            f"zsxq api code {envelope.get('code')}: {info}".strip(),
        )

    raise FetchError(ErrorClass.PARSE, "unrecognised zsxq envelope shape")


def _pause(seconds: float) -> None:
    """The 1059 backoff pause; a seam so tests do not actually sleep."""
    time.sleep(seconds)
