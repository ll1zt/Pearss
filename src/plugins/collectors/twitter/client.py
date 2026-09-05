"""X (Twitter) GraphQL client.

Sends the authenticated request for the operator's own Home timeline and turns
transport outcomes into `ErrorClass` values. Parsing lives in `collector.py`;
walking pages lives in `paging.py`.

Login-type access under ADR 0012: the credentials are the operator's own, read
from the environment, never from the database and never from a source row. They
are not sent unless the source was declared as this collector.

Two things here are load-bearing rather than incidental:

- **`seenTweetIds` is never sent.** The upstream implementation records a real
  regression from doing so: X reads it as "exclude these", which means the
  local stop-anchor can never reach an already-ingested id, and every refresh
  walks further into history ingesting hundreds of old tweets. The seen set is
  a local concept only.
- **A 200 with `{"data": null}` is not success.** X returns it when risk
  control suppresses the response. It is caught by parsing, not by status, so
  it surfaces as `blocked` instead of a healthy run with zero items.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, replace
from typing import Final

import httpx

from adapters.http import FetchError, FetchPolicy, fetch, post_json
from adapters.raw_payload import RawPayload

LOGGER = logging.getLogger(__name__)

API_BASE = "https://x.com/i/api/graphql"
OPERATION: Final = "HomeLatestTimeline"

# The public web client's bearer; it is not a secret, it is the value every
# browser sends. The credential is the cookie pair below.
PUBLIC_BEARER = (
    "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

# X's feature flags for this operation. It rejects the request outright when a
# flag it now requires is absent, so the set is pinned rather than minimal.
FEATURES: Final[dict[str, bool]] = {
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "tweetypie_unmention_optimization_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": False,
    "tweet_awards_web_tipping_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "rweb_video_timestamps_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "responsive_web_media_download_video_enabled": False,
    "responsive_web_enhance_cards_enabled": False,
}

# These are environment variable *names*, not values: the secrets live in the
# environment and never in the repository. S105 matches on the identifier
# containing "token", which cannot distinguish the two.
AUTH_TOKEN_ENV = "PEARSS_X_AUTH_TOKEN"  # noqa: S105
CT0_ENV = "PEARSS_X_CT0"
QUERY_ID_ENV = "PEARSS_X_QUERY_ID"
COUNT_ENV = "PEARSS_X_COUNT"
USER_AGENT_ENV = "PEARSS_X_USER_AGENT"

# X serves a different document to a client it does not recognise as a browser,
# so the UA is part of the request shape rather than a courtesy. Overridable so
# a deployment can present its own contact.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/141.0.0.0 Safari/537.36"
)

# Last known queryId, used when the environment does not pin one. X rotates it;
# a 400 triggers a re-extract rather than a permanent failure.
FALLBACK_QUERY_ID = "CN1ceqddxq3bz_YPM3olEQ"

_QUERY_ID_RE = re.compile(r'"HomeLatestTimeline","([^"]+)"')
_BUNDLE_RE = re.compile(r'src="([^"]*?/main\.[0-9a-f]+\.js)"')


class CredentialError(Exception):
    """No usable login credentials. Reported as `auth`, never worked around."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Credentials:
    """The operator's own session, read from the environment."""

    auth_token: str
    ct0: str

    @classmethod
    def from_env(cls) -> Credentials:
        auth = os.environ.get(AUTH_TOKEN_ENV, "").strip()
        ct0 = os.environ.get(CT0_ENV, "").strip()
        if not auth or not ct0:
            raise CredentialError(
                f"{AUTH_TOKEN_ENV} and {CT0_ENV} are both required for an "
                "authenticated X source"
            )
        return cls(auth_token=auth, ct0=ct0)

    def headers(self) -> dict[str, str]:
        """The header set that presents this as the logged-in web client.

        `x-csrf-token` must equal the `ct0` cookie: X checks that they match, so
        sending one without the other is a 403 rather than a partial success.
        """
        return {
            "authorization": PUBLIC_BEARER,
            "cookie": f"auth_token={self.auth_token}; ct0={self.ct0}",
            "x-csrf-token": self.ct0,
            "x-twitter-auth-type": "OAuth2Session",
            "x-twitter-active-user": "yes",
            "x-twitter-client-language": "en",
            "referer": "https://x.com/home",
            "origin": "https://x.com",
        }


def page_body(*, cursor: str | None, count: int) -> dict[str, object]:
    """The GraphQL request body.

    Note what is absent: no `seenTweetIds`. See the module docstring -- sending
    it turns every refresh into a deep crawl through history.
    """
    variables: dict[str, object] = {
        "count": count,
        # false is Following; true is the "For you" recommendation stream.
        "enableRanking": False,
        "includePromotedContent": False,
        "requestContext": "launch",
    }
    if cursor:
        variables["cursor"] = cursor
    return {"variables": variables, "features": FEATURES}


def fetch_page(
    *,
    client: httpx.Client,
    credentials: Credentials,
    query_id: str,
    cursor: str | None = None,
    count: int = 20,
    source_id: str = "",
    raw_fetch_id: str | None = None,
    correlation_id: str = "",
    policy: FetchPolicy | None = None,
) -> RawPayload:
    """Fetch one timeline page.

    Raises FetchError with the classification the collector guide asks for:
    401/403 is `auth`, 429 is `blocked`, a timeout is `network`.
    """
    limits = policy or FetchPolicy()
    # Credentials are layered on top of whatever policy the caller supplied,
    # never instead of it. A caller passing its own policy -- to widen a
    # timeout, say -- must not be able to silently drop the session, which
    # would send an unauthenticated request and fail as a confusing 403.
    caller_changed_ua = (
        policy is not None and limits.user_agent != FetchPolicy().user_agent
    )
    limits = replace(
        limits,
        accept=limits.accept or "application/json",
        user_agent=(
            limits.user_agent
            if caller_changed_ua
            else os.environ.get(USER_AGENT_ENV, DEFAULT_USER_AGENT)
        ),
        extra_headers={**limits.extra_headers, **credentials.headers()},
    )
    url = f"{API_BASE}/{query_id}/{OPERATION}"
    try:
        return post_json(
            url,
            body=page_body(cursor=cursor, count=count),
            client=client,
            source_id=source_id,
            raw_fetch_id=raw_fetch_id,
            policy=limits,
            correlation_id=correlation_id,
        )
    except FetchError as exc:
        if exc.detail.startswith("HTTP 400"):
            # A 400 from this endpoint almost always means the queryId rotated,
            # which is recoverable. 401/403/429 keep the classification the
            # taxonomy already gives them.
            raise QueryIdRejected(detail=exc.detail) from exc
        raise


class QueryIdRejected(Exception):
    """X answered 400, which for this endpoint means the queryId is stale."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def current_query_id() -> str:
    """The configured queryId, or the last known one."""
    return os.environ.get(QUERY_ID_ENV, "").strip() or FALLBACK_QUERY_ID


def refresh_query_id(client: httpx.Client) -> str | None:
    """Recover a live queryId from x.com's own JavaScript bundle.

    Returns None rather than raising when the bundle cannot be read: the caller
    still has the previous value, and a failure to discover a new one should not
    look like a credential problem.
    """
    try:
        home = fetch(
            "https://x.com/home",
            client=client,
            policy=FetchPolicy(
                accept="text/html,application/xhtml+xml",
                user_agent=os.environ.get(USER_AGENT_ENV, DEFAULT_USER_AGENT),
            ),
        )
    except FetchError as exc:
        LOGGER.info("queryId recovery: x.com unreachable (%s)", exc.detail)
        return None

    html_text = home.text()
    bundle = _BUNDLE_RE.search(html_text)
    if not bundle:
        LOGGER.info("queryId recovery: no main bundle reference in the document")
        return None

    script_url = bundle.group(1)
    if script_url.startswith("//"):
        script_url = "https:" + script_url
    elif script_url.startswith("/"):
        script_url = "https://x.com" + script_url

    try:
        script = fetch(
            script_url,
            client=client,
            policy=FetchPolicy(
                accept="*/*",
                max_bytes=20 * 1024 * 1024,
                user_agent=os.environ.get(USER_AGENT_ENV, DEFAULT_USER_AGENT),
            ),
        )
    except FetchError as exc:
        LOGGER.info("queryId recovery: bundle fetch failed (%s)", exc.detail)
        return None

    found = _QUERY_ID_RE.search(script.text())
    if not found:
        LOGGER.info("queryId recovery: HomeLatestTimeline not present in bundle")
        return None
    return found.group(1)


def configured_count() -> int:
    """Tweets per page. X caps what it will return; above ~40 it just answers 400."""
    raw = os.environ.get(COUNT_ENV, "").strip()
    if raw.isdigit():
        return max(1, min(int(raw), 40))
    return 20


def credentials_configured() -> bool:
    """Whether an authenticated X source could run at all.

    Used to report `auth` at collection time with a clear message rather than
    letting a missing credential surface as an opaque HTTP 403.
    """
    return bool(
        os.environ.get(AUTH_TOKEN_ENV, "").strip()
        and os.environ.get(CT0_ENV, "").strip()
    )


__all__ = [
    "AUTH_TOKEN_ENV",
    "CT0_ENV",
    "CredentialError",
    "Credentials",
    "QueryIdRejected",
    "configured_count",
    "credentials_configured",
    "current_query_id",
    "fetch_page",
    "page_body",
    "refresh_query_id",
]
