"""知识星球 (zsxq) collector: group topics via the v2 JSON API.

A P1 quasi-plugin ported from the reference `earss_source_zsxq` adapter
(which ports RSSHub's `lib/routes/zsxq/group.ts`). It produces ItemDraft and
nothing else — the cookie policy and envelope handling live in `client.py`,
normalisation/dedupe stay in core.

Source URLs are the human group pages an operator would paste, with an
optional scope query parameter:

    https://wx.zsxq.com/dweb2/index/group/<group_id>               -> scope=all
    https://wx.zsxq.com/dweb2/index/group/<group_id>?scope=digests

Scopes: all | digests | by_owner | questions | tasks. The bare URL is the
canonical form of `scope=all` — please do not subscribe with `?scope=all`
explicitly, it would create a second identity for the same feed.

One collection makes two API calls (group meta for the feed name, then the
topics list) and merges them into a single internal payload
`{"group": …, "topics": […]}` for `parse`, keeping the registry's one-payload
fetch-hook contract. Topic text is member-generated and untrusted; it passes
through here and the store-time sanitiser (ADR 0007) is the trust boundary.
"""

from __future__ import annotations

import html as html_module
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

import httpx

from adapters.http import FetchError
from adapters.raw_payload import ConditionalState, RawPayload
from core.enums import ErrorClass
from core.models import ItemDraft
from core.plugins.hooks import (
    HOOKIMPL,
    BackfillHooks,
    CollectorError,
    ConfigRequirement,
    Manifest,
)
from plugins.collectors.zsxq import client as zsxq_client

LIMIT_TOPICS: Final = 20
MAX_TOPICS: Final = 30

SCOPES: Final = ("all", "digests", "by_owner", "questions", "tasks")

_HOSTS: Final = frozenset({"wx.zsxq.com"})
# Both `/dweb2/index/group/<id>` (the app's own routing) and the bare
# `/group/<id>` people paste from the address bar address the same page.
_GROUP_PATH_RE: Final = re.compile(r"^/(?:dweb2/index/)?group/(?P<group_id>\d{3,32})$")
_TOPIC_URL_RE: Final = re.compile(
    r"^https://api\.zsxq\.com/v2/groups/(?P<group_id>\d+)/topics"
)

_TIME_RE: Final = re.compile(
    r"^(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})[T ]"
    r"(?P<hh>\d{2}):(?P<mm>\d{2}):(?P<ss>\d{2})"
)

# zsxq renders links/hashtags as pseudo-tags inside plain text; the two
# patterns and their order come from the reference Content renderer.
_WEB_TAG_RE: Final = re.compile(
    r'<e type="web" href="(.*?)" title="(.*?)"(?: style=".*?")? />', re.DOTALL
)
_HASHTAG_TAG_RE: Final = re.compile(
    r'<e type="hashtag"[^>]*?title="(.*?)" />', re.DOTALL
)

_TYPE_FALLBACK_TITLES: Final = {
    "talk": "文章",
    "q&a": "问答",
    "task": "作业",
    "solution": "写作业",
}


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceRoute:
    """Which group feed a source URL names, and at which scope."""

    group_id: str
    scope: str


# --------------------------------------------------------------------------
# URL routing
# --------------------------------------------------------------------------


@HOOKIMPL
def match_url(url: str) -> bool:
    """Whether this collector should handle `url`.

    URL-matching only: no network access and no database lookups, because it
    runs during source selection and discovery.
    """
    try:
        resolve_source(url)
    except ValueError:
        return False
    return True


def resolve_source(url: str) -> SourceRoute:
    """Resolve a group page URL into (group_id, scope).

    Raises ValueError for anything that is not a routable group page, so a
    malformed source fails at creation instead of at every fetch.
    """
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if host not in _HOSTS:
        raise ValueError(f"not a zsxq group page: {url!r}")

    match = _GROUP_PATH_RE.match(parts.path.rstrip("/"))
    if match is None:
        raise ValueError(f"not a routable zsxq group page: {url!r}")

    scope = parse_qs(parts.query).get("scope", ["all"])[0].strip() or "all"
    if scope not in SCOPES:
        raise ValueError(
            f"invalid zsxq scope {scope!r}; choose from {', '.join(SCOPES)}"
        )
    return SourceRoute(group_id=match.group("group_id"), scope=scope)


def topics_api_path(route: SourceRoute) -> str:
    """The topics endpoint for a route, with the scope always spelled out."""
    count = min(max(LIMIT_TOPICS, 1), MAX_TOPICS)
    query = urlencode({"scope": route.scope, "count": count})
    return f"/groups/{route.group_id}/topics?{query}"


# --------------------------------------------------------------------------
# The registry hooks
# --------------------------------------------------------------------------


def fetch(
    url: str,
    *,
    client: httpx.Client,
    source_id: str,
    conditional: ConditionalState | None = None,
) -> RawPayload:
    """Fetch a group feed (the registry's fetch hook).

    Two API calls — the group meta names the feed, the topics list is the
    content — merged into one internal payload so `parse` keeps the
    one-payload contract. A CredentialError maps to FetchError(`auth`) here:
    the pipeline classifies FetchError, and an uncaught credential failure
    would surface as an unexplained 500 instead of a readable run.
    """
    del conditional
    route = resolve_source(url)
    topics_path = topics_api_path(route)
    try:
        group_raw = zsxq_client.get_json(
            f"/groups/{route.group_id}", client=client, source_id=source_id
        )
        topics_raw = zsxq_client.get_json(
            topics_path, client=client, source_id=source_id
        )
    except zsxq_client.CredentialError as exc:
        raise FetchError(ErrorClass.AUTH, str(exc)) from exc

    # The reference fetch_group unwraps `{"group": …}`; some deployments
    # answer with the group map directly. Accept both, prefer the wrapper.
    group_resp = json.loads(group_raw.text())
    group = (
        group_resp.get("group")
        if isinstance(group_resp.get("group"), dict)
        else group_resp
    )
    merged = {
        "group": group,
        "topics": json.loads(topics_raw.text()).get("topics") or [],
    }
    return RawPayload(
        url=f"{zsxq_client.BASE}{topics_path}",
        status_code=200,
        body=json.dumps(merged, ensure_ascii=False).encode(),
        source_id=source_id,
    )


@HOOKIMPL
def parse(raw: RawPayload) -> list[ItemDraft]:
    """Turn a merged group+topics payload into drafts.

    Raises CollectorError when the shape moved: "the API changed" and "the
    group is quiet" are different situations. An empty topics list is
    legitimate.
    """
    if "/topics" not in raw.url:
        raise ValueError(
            f"not a zsxq topics payload this collector fetches: {raw.url!r}"
        )
    try:
        payload = json.loads(raw.text())
    except ValueError as exc:
        raise CollectorError(ErrorClass.PARSE, f"undecodable JSON: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("topics"), list):
        raise CollectorError(
            ErrorClass.STRUCTURE_CHANGED,
            "expected a merged payload with a `topics` array",
        )

    topics = payload["topics"]
    if not topics:
        return []

    group = payload.get("group") or {}
    group_id = _group_id_of(raw.url)
    scope = parse_qs(urlsplit(raw.url).query).get("scope", ["all"])[0].strip() or "all"
    drafts = [
        d
        for t in topics
        if (d := _topic_draft(t, group, raw=raw, group_id=group_id, scope=scope))
        is not None
    ]
    if not drafts:
        raise CollectorError(
            ErrorClass.STRUCTURE_CHANGED,
            f"all {len(topics)} topics lacked a usable shape",
        )
    return drafts


def _group_id_of(url: str) -> str:
    match = _TOPIC_URL_RE.match(url)
    return match.group("group_id") if match else ""


def _topic_draft(
    topic: Any,
    group: dict[str, Any],
    *,
    raw: RawPayload,
    group_id: str,
    scope: str,
) -> ItemDraft | None:
    if not isinstance(topic, dict):
        return None
    topic_id = topic.get("topic_id")
    if isinstance(topic_id, bool) or topic_id is None:
        return None

    name = _text(group.get("name")) or group_id or "知识星球"
    feed_title = (
        f"知识星球 - {name}" if scope == "all" else f"知识星球 - {name} ({scope})"
    )

    title, content, author = _render_by_type(topic)
    explicit = _text(topic.get("title"))
    if explicit:
        title = explicit

    return ItemDraft(
        source_id=raw.source_id,
        external_id=f"topic:{topic_id}",
        title=title,
        canonical_url=f"https://wx.zsxq.com/topic/{topic_id}",
        published_at=_parse_time(topic.get("create_time")),
        content=content,
        metadata={"feed_title": feed_title, "author": author or ""},
    )


def _render_by_type(topic: dict[str, Any]) -> tuple[str, str, str | None]:
    """(title, html, author) for one topic, per the reference shaping."""
    kind = _text(topic.get("type"))

    if kind == "talk":
        talk = topic.get("talk") or {}
        text = _text_or_empty(talk.get("text"))
        title = _first_line(text) or _TYPE_FALLBACK_TITLES["talk"]
        return (
            title,
            _render(text, _images(talk.get("images"))),
            _name_of(talk.get("owner")),
        )

    if kind == "q&a":
        question = topic.get("question") or {}
        answer = topic.get("answer") or {}
        q_text = _text_or_empty(question.get("text"))
        q_owner = _name_of(question.get("owner")) or "匿名用户"
        title = _first_line(q_text) or _TYPE_FALLBACK_TITLES["q&a"]
        q_html = _render(q_text, _images(question.get("images")))
        body = f"<blockquote>{_escape(q_owner)} 提问：{q_html}</blockquote>"
        if topic.get("answered") in (True, "true"):
            a_owner = _name_of(answer.get("owner")) or "回答者"
            body = (
                body
                + "<br>"
                + f"{_escape(a_owner)} 回答：<br><br>"
                + _render(
                    _text_or_empty(answer.get("text")), _images(answer.get("images"))
                )
            )
            return title, body, a_owner
        return title, body, q_owner

    if kind == "task":
        task = topic.get("task") or {}
        text = _text_or_empty(task.get("text"))
        return (
            _first_line(text) or _TYPE_FALLBACK_TITLES["task"],
            _render(text, _images(task.get("images"))),
            _name_of(task.get("owner")),
        )

    if kind == "solution":
        solution = topic.get("solution") or {}
        text = _text_or_empty(solution.get("text"))
        return (
            _first_line(text) or _TYPE_FALLBACK_TITLES["solution"],
            _render(text, _images(solution.get("images"))),
            _name_of(solution.get("owner")),
        )

    # Unknown types: best-effort title only, no invented content.
    return _text(topic.get("title")) or "动态", "", None


# --------------------------------------------------------------------------
# Content rendering (port of the reference Content module)
# --------------------------------------------------------------------------


def _render(text: str, images: list[str]) -> str:
    """Topic body text plus image list to simple HTML.

    The text is member-generated and passes through unescaped — zsxq's
    pseudo-tags are real markup to decode first, and the store-time
    sanitiser (ADR 0007) is the trust boundary for everything else.
    """
    body = text.replace("\n", "<br>")
    body = _WEB_TAG_RE.sub(_web_tag, body)
    body = _HASHTAG_TAG_RE.sub(_hashtag_tag, body)
    image_html = "".join(f'<br><img src="{_escape_attr(url)}">' for url in images)
    if body and image_html:
        return body + image_html
    return body or image_html


def _web_tag(match: re.Match[str]) -> str:
    href = unquote(match.group(1))
    title = unquote(match.group(2))
    return f'<a href="{_escape_attr(href)}">{_escape_text(title)}</a>'


def _hashtag_tag(match: re.Match[str]) -> str:
    return f"<span>{_escape_text(unquote(match.group(1)))}</span>"


def _images(raw: Any) -> list[str]:
    """Image URLs in original → large → thumbnail preference order."""
    urls: list[str] = []
    if not isinstance(raw, list):
        return urls
    for image in raw:
        if not isinstance(image, dict):
            continue
        for size in ("original", "large", "thumbnail"):
            variant = image.get(size)
            if isinstance(variant, dict):
                url = variant.get("url")
                if isinstance(url, str) and url:
                    urls.append(url)
                    break
    return urls


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _parse_time(value: Any) -> datetime | None:
    """ISO-8601 create_time, tolerating a missing offset.

    An aware string parses as-is; a naive one falls through to the regex and
    returns naive, letting core's normalize attach UTC — the same end state
    as the reference adapter's "append Z" fallback, through the documented
    mechanism instead of a silent claim.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text)
        return parsed
    except ValueError:
        pass
    match = _TIME_RE.match(text)
    if match is None:
        return None
    parts = tuple(int(part) for part in match.groups())
    y, mo, d, hh, mm, ss = parts
    try:
        return datetime(y, mo, d, hh, mm, ss)
    except ValueError:
        return None


def _first_line(text: str) -> str | None:
    line = text.split("\n", 1)[0].strip()
    return line or None


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _text_or_empty(value: Any) -> str:
    return "" if value is None else str(value)


def _name_of(owner: Any) -> str | None:
    if isinstance(owner, dict):
        name = owner.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


def _escape(value: Any) -> str:
    return html_module.escape(str(value if value is not None else ""), quote=True)


def _escape_attr(value: Any) -> str:
    return html_module.escape(str(value if value is not None else ""), quote=True)


def _escape_text(value: Any) -> str:
    return html_module.escape(str(value if value is not None else ""), quote=False)


@HOOKIMPL
def source_config() -> Manifest:
    """The zsxq collector's manifest."""
    return Manifest(
        id="zsxq",
        version="0.1.0",
        name="知识星球群组",
        description="知识星球群组主题（登录型，ADR 0015）。",
        example_url="https://wx.zsxq.com/dweb2/index/group/88855458825252",
        features=frozenset({"fetch", "cookie"}),
        required_config=(
            ConfigRequirement(
                env_name="PEARSS_ZSXQ_ACCESS_TOKEN",
                description="zsxq_access_token（或 PEARSS_ZSXQ_COOKIE/CookieCloud）",
            ),
        ),
    )


@HOOKIMPL
def collector_id() -> str:
    """The stable registration key (ADR 0017)."""
    return "zsxq"


@HOOKIMPL
def backfill() -> BackfillHooks | None:
    """This origin has no backward walk."""
    return None
