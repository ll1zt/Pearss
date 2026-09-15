"""Zhihu collector: user activities / answers / posts via the web JSON APIs.

A P1 quasi-plugin, ported from the reference `earss_source_zhihu` adapter
(which itself follows RSSHub's `lib/routes/zhihu/*` routes). It produces
ItemDraft and nothing else — the signed fetch lives in `client.py`, the
x-zse-96 signature in `zse96.py`, and normalisation/dedupe stay in core.

Source URLs are the human pages an operator would paste:

    https://www.zhihu.com/people/<url_token>              -> activities (动态)
    https://www.zhihu.com/people/<url_token>/answers      -> answers
    https://www.zhihu.com/people/<url_token>/posts        -> posts
    https://www.zhihu.com/org/<url_token>/posts           -> org posts

Zhihu's activity feed mixes content with noise, so shaping is opinionated and
matches the reference: 关注了… and 提出了问题 activities are dropped, remaining
entries are tagged with their action ([赞同] [创作] [收藏] [动态]), and pin
(想法) titles are the body excerpt truncated to 60 chars. One deliberate
divergence: pin videos become links, because the store-time sanitiser's
allowlist (ADR 0007) does not carry `video` and re-opening it for one
collector is the wrong trade.
"""

from __future__ import annotations

import html as html_module
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from lxml import etree
from lxml import html as lxml_html

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
from plugins.collectors.zhihu import client as zhihu_client

LOGGER = logging.getLogger(__name__)

BASE: Final = zhihu_client.BASE

LIMIT_ACTIVITIES: Final = 10
LIMIT_ANSWERS: Final = 10
LIMIT_POSTS: Final = 20
PIN_TITLE_MAX: Final = 60

# data[*] fields the articles endpoint needs for full content, as the web
# client requests them. Percent-encoded when the query is assembled, and the
# encoded form is what gets signed.
ARTICLES_INCLUDE: Final = (
    "data[*].comment_count,suggest_edit,is_normal,thumbnail_extra_info,thumbnail,"
    "can_comment,comment_permission,admin_closed_comment,content,voteup_count,"
    "created,updated,upvoted_followees,voting,review_info,reaction_instruction,"
    "is_labeled,label_info;data[*].author.badge[?(type=best_answerer)].topics;"
    "data[*].author.vip_info;"
)

_HOSTS: Final = frozenset({"zhihu.com", "www.zhihu.com"})

# Zhihu url_token: letters, digits, underscore, hyphen, dot — but no run of
# two dots (the reference adapter rejects `marisa..moe`).
_TOKEN_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

_ACTION_TEXTS: Final = {
    "like": ("赞同", "赞", "喜欢", "VOTEUP", "_LIKE"),
    "collect": ("收藏", "FAVOR", "COLLECT"),
    "create": ("回答", "发表", "发布", "创作", "写了", "CREATE", "ANSWER", "WRITE"),
}
_FOLLOW_MARKERS: Final = ("关注", "FOLLOW", "SUBSCRIBED")
_QUESTION_CREATED_MARKERS: Final = ("提出了问题", "提出问题", "QUESTION_CREAT")
# "other" is the fall-through for an action the marker tables do not know:
# the action name is unknown, but the target is still content worth showing,
# so the draft renders with a neutral tag rather than crashing on the lookup
# (seen live: one unknown action type took a source's whole collection down).
_ACTION_TAGS: Final = {
    "like": "赞同",
    "collect": "收藏",
    "create": "创作",
    "other": "动态",
}


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceRoute:
    """Which feed a source URL names, and whose it is."""

    kind: str  # activities | answers | posts
    url_token: str
    usertype: str = "people"  # people | org; meaningful for posts


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
    """Resolve a human Zhihu page URL into the feed it names.

    Raises ValueError for anything that is not a routable Zhihu user page, so
    a malformed source fails at creation instead of at every fetch.
    """
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if host not in _HOSTS:
        raise ValueError(f"not a Zhihu page: {url!r}")

    segments = [segment for segment in parts.path.split("/") if segment]
    if len(segments) >= 2 and segments[0] == "people":
        token = _valid_token(segments[1], url)
        if len(segments) == 2:
            return SourceRoute(kind="activities", url_token=token)
        if segments[2] in ("activities", "answers", "posts"):
            return SourceRoute(kind=segments[2], url_token=token)
        raise ValueError(f"unsupported Zhihu people route: {url!r}")
    if len(segments) >= 3 and segments[0] == "org" and segments[2] == "posts":
        token = _valid_token(segments[1], url)
        return SourceRoute(kind="posts", url_token=token, usertype="org")
    raise ValueError(f"not a routable Zhihu user page: {url!r}")


def _valid_token(token: str, url: str) -> str:
    if not _TOKEN_RE.match(token) or ".." in token:
        raise ValueError(f"invalid Zhihu url_token in {url!r}")
    return token


def api_path_for(route: SourceRoute) -> tuple[str, str]:
    """The (signed api_path, referer) pair one collection call needs.

    The query is percent-encoded here and the encoded form is what gets
    signed: the server verifies against the request target it receives, so
    signing and sending must agree byte for byte.
    """
    token = route.url_token
    if route.kind == "activities":
        query = urlencode(
            {"limit": LIMIT_ACTIVITIES, "desktop": "true", "ws_qiangzhisafe": "0"}
        )
        path = f"/api/v3/moments/{token}/activities?{query}"
        referer = f"{BASE}/people/{token}"
    elif route.kind == "answers":
        query = urlencode(
            {"limit": LIMIT_ANSWERS, "include": "data[*].is_normal,content"}
        )
        path = f"/api/v4/members/{token}/answers?{query}"
        referer = f"{BASE}/people/{token}/activities"
    elif route.kind == "posts":
        query = urlencode(
            {
                "include": ARTICLES_INCLUDE,
                "offset": 0,
                "limit": LIMIT_POSTS,
                "sort_by": "created",
            }
        )
        path = f"/api/v4/members/{token}/articles?{query}"
        referer = f"{BASE}/{route.usertype}/{token}/posts"
    else:  # pragma: no cover - resolve_source only produces the three kinds
        raise ValueError(f"unknown route kind {route.kind!r}")
    return path, referer


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
    """Fetch the API page a source URL names (the registry's fetch hook).

    The hook's failure contract is FetchError, so a missing session maps to
    `auth` here — the pipeline classifies FetchError, and an uncaught
    CredentialError would surface as an unexplained 500 instead of a run
    a diagnostic can read. `conditional` is accepted for hook-signature
    uniformity and ignored: a signed API answers no 304.
    """
    del conditional
    route = resolve_source(url)
    api_path, referer = api_path_for(route)
    try:
        return zhihu_client.get_json(
            api_path, client=client, source_id=source_id, referer=referer
        )
    except zhihu_client.CredentialError as exc:
        raise FetchError(ErrorClass.AUTH, str(exc)) from exc


@HOOKIMPL
def parse(raw: RawPayload) -> list[ItemDraft]:
    """Turn a fetched API payload into drafts.

    Raises CollectorError with a classified reason rather than returning an
    empty list when the shape moved: "the API changed" and "the user has no
    content" are different situations, and collapsing them is how a broken
    parser goes unnoticed. An empty `data` array is legitimate.
    """
    kind = _kind_of(raw.url)
    try:
        payload = json.loads(raw.text())
    except ValueError as exc:
        raise CollectorError(ErrorClass.PARSE, f"undecodable JSON: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise CollectorError(
            ErrorClass.STRUCTURE_CHANGED,
            "expected a JSON object with a `data` array",
        )
    data = payload["data"]
    if not data:
        return []

    if kind == "activities":
        drafts = _activities(data, raw.source_id)
    elif kind == "answers":
        drafts = _entries(data, raw.source_id, _answer_draft)
    else:
        drafts = _entries(data, raw.source_id, _article_draft)

    if not drafts:
        raise CollectorError(
            ErrorClass.STRUCTURE_CHANGED,
            f"all {len(data)} entries lacked a usable shape",
        )
    return drafts


def _kind_of(url: str) -> str:
    if "/activities" in url:
        return "activities"
    if "/answers" in url:
        return "answers"
    if "/articles" in url:
        return "posts"
    raise ValueError(f"not a Zhihu API URL this collector fetches: {url!r}")


def _entries(data: list[Any], source_id: str, shaper: Any) -> list[ItemDraft]:
    drafts = [draft for item in data if (draft := shaper(item, source_id)) is not None]
    return drafts


# --------------------------------------------------------------------------
# Answers and articles
# --------------------------------------------------------------------------


def _answer_draft(item: Any, source_id: str) -> ItemDraft | None:
    if not isinstance(item, dict):
        return None
    answer_id = _id_of(item)
    question = item.get("question")
    if answer_id is None or not isinstance(question, dict):
        return None
    question_id = question.get("id")
    if question_id is None:
        return None

    author = _name_of(item.get("author"))
    return ItemDraft(
        source_id=source_id,
        external_id=f"answer:{answer_id}",
        title=_text(question.get("title")) or "回答",
        canonical_url=f"https://www.zhihu.com/question/{question_id}/answer/{answer_id}",
        published_at=_unix_to_dt(item.get("created_time")),
        content=_process_html(item.get("content")) or "",
        metadata={
            "feed_title": _feed_title("answers", item, fallback=item.get("url_token")),
            "author": author or "",
        },
    )


def _article_draft(item: Any, source_id: str) -> ItemDraft | None:
    if not isinstance(item, dict):
        return None
    article_id = _id_of(item)
    if article_id is None:
        return None

    author = _name_of(item.get("author"))
    return ItemDraft(
        source_id=source_id,
        external_id=f"article:{article_id}",
        title=_text(item.get("title")) or "文章",
        canonical_url=f"https://zhuanlan.zhihu.com/p/{article_id}",
        published_at=_unix_to_dt(item.get("created") or item.get("created_time")),
        content=_process_html(item.get("content")) or "",
        metadata={
            "feed_title": _feed_title("posts", item, fallback=item.get("url_token")),
            "author": author or "",
        },
    )


# --------------------------------------------------------------------------
# Activities
# --------------------------------------------------------------------------


def _activities(data: list[Any], source_id: str) -> list[ItemDraft]:
    drafts: list[ItemDraft] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        target = item.get("target")
        if not isinstance(target, dict):
            continue
        classifier = _classify(item)
        if classifier is None:
            continue  # follow/question-created noise, per the reference adapter

        mapped = _map_target(target)
        if mapped is None:
            # A target type this collector cannot render honestly. Dropping it
            # is correct -- rendering an unknown shape as a title-only stub
            # would be worse -- but dropping it *silently* is not: if Zhihu
            # starts emitting a content type (a new module, a renamed type), the
            # loss is partial, so `parse`'s all-unusable guard never fires and
            # the feed just quietly thins out. Logged so that is diagnosable,
            # the same lesson the `other` action fall-through encodes.
            LOGGER.warning(
                "zhihu: dropped an activity whose target type %r is not "
                "renderable (id=%r); the feed will be missing it",
                _text(target.get("type")),
                target.get("id"),
            )
            continue  # a target type we cannot render honestly

        action = _text(item.get("action_text"))
        activity_id = _id_of(item)
        external = (
            f"activity:{activity_id}"
            if activity_id is not None
            else f"activity:{mapped.guid}"
        )
        actor_name = _name_of(item.get("actor")) or ""
        drafts.append(
            ItemDraft(
                source_id=source_id,
                external_id=external,
                title=f"[{_ACTION_TAGS[classifier]}] {mapped.title}",
                canonical_url=mapped.link,
                published_at=_unix_to_dt(item.get("created_time")),
                content=_process_html(
                    _actor_card(item.get("actor"), action) + (mapped.content or "")
                )
                or "",
                metadata={
                    "feed_title": _feed_title(
                        "activities", item, fallback=actor_name or None
                    ),
                    "author": mapped.author or actor_name,
                },
            )
        )
    return drafts


@dataclass(frozen=True, slots=True, kw_only=True)
class _Target:
    """One mapped activity target, ready to become a draft."""

    title: str
    link: str
    content: str | None
    author: str | None
    guid: str


def _map_target(target: dict[str, Any]) -> _Target | None:
    kind = _text(target.get("type")) or ""
    target_id = _id_of(target)

    if kind == "answer":
        question = target.get("question") or {}
        question_id = question.get("id") if isinstance(question, dict) else None
        if target_id is None or question_id is None:
            return None
        return _Target(
            title=_text(question.get("title")) or "回答",
            link=f"https://www.zhihu.com/question/{question_id}/answer/{target_id}",
            content=_process_html(target.get("content")),
            author=_name_of(target.get("author")),
            guid=f"answer:{target_id}",
        )
    if kind == "article":
        if target_id is None:
            return None
        return _Target(
            title=_text(target.get("title")) or "文章",
            link=f"https://zhuanlan.zhihu.com/p/{target_id}",
            content=_process_html(target.get("content")),
            author=_name_of(target.get("author")),
            guid=f"article:{target_id}",
        )
    if kind == "question":
        if target_id is None:
            return None
        return _Target(
            title=_text(target.get("title")) or "问题",
            link=f"https://www.zhihu.com/question/{target_id}",
            content=_process_html(target.get("detail")),
            author=_name_of(target.get("author")),
            guid=f"question:{target_id}",
        )
    if kind == "pin":
        if target_id is None:
            return None
        content = _pin_content(target.get("content") or [])
        return _Target(
            title=_pin_title(target, content),
            link=f"https://www.zhihu.com/pin/{target_id}",
            content=content,
            author=_name_of(target.get("author")),
            guid=f"pin:{target_id}",
        )
    if kind == "collection":
        if target_id is None:
            return None
        return _Target(
            title=_text(target.get("title")) or "收藏夹",
            link=f"https://www.zhihu.com/collection/{target_id}",
            content=None,
            author=None,
            guid=f"collection:{target_id}",
        )
    if kind == "column":
        if target_id is None:
            return None
        intro = _escape(target.get("intro"))
        image = target.get("image_url")
        content = f"<p>{intro}</p>" + (
            f'<p><img src="{_escape(str(image))}"/></p>' if image else ""
        )
        return _Target(
            title=_text(target.get("title")) or "专栏",
            link=f"https://zhuanlan.zhihu.com/{target_id}",
            content=content,
            author=None,
            guid=f"column:{target_id}",
        )
    if kind == "topic":
        if target_id is None:
            return None
        followers = target.get("followers_count")
        return _Target(
            title=_text(target.get("name")) or "话题",
            link=f"https://www.zhihu.com/topic/{target_id}",
            content=(
                f"<p>{_escape(target.get('introduction'))}</p>"
                f"<p>话题关注者人数：{_escape(followers)}</p>"
            ),
            author=None,
            guid=f"topic:{target_id}",
        )
    if kind == "live":
        if target_id is None:
            return None
        description = _text(target.get("description"))
        return _Target(
            title=_text(target.get("subject")) or "Live",
            link=f"https://www.zhihu.com/lives/{target_id}",
            content=(
                "<p>" + html_module.escape(description).replace("\n", "<br/>") + "</p>"
            )
            if description
            else None,
            author=None,
            guid=f"live:{target_id}",
        )
    if kind == "roundtable":
        if target_id is None:
            return None
        return _Target(
            title=_text(target.get("name")) or "圆桌",
            link=f"https://www.zhihu.com/roundtable/{target_id}",
            content=_process_html(target.get("description")),
            author=None,
            guid=f"roundtable:{target_id}",
        )
    return None


def _classify(item: dict[str, Any]) -> str | None:
    """Which feed-worthy action this activity is, or None for noise.

    关注了… (question/topic/column/collection/roundtable) and 提出了问题 are
    dropped entirely — they say the person looked at something, which is not
    content a reader subscribed for.
    """
    text = f"{_text(item.get('action_text'))} {_text(item.get('verb'))}"
    if _contains_any(text, _FOLLOW_MARKERS):
        return None
    if _contains_any(text, _QUESTION_CREATED_MARKERS):
        return None
    for name in ("like", "collect", "create"):
        if _contains_any(text, _ACTION_TEXTS[name]):
            return name
    return "other"


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _actor_card(actor: Any, action: str) -> str:
    """Who performed the action, so 赞同/收藏 entries cannot be misread.

    The content author (target.author) is the original author and names the
    item; the actor merely acted on it. Deliberately text-only: the avatar
    would hotlink zhimg CDN images whose URLs drift, and the store-time
    sanitiser strips the style attribute the reference card used anyway.
    """
    name = _name_of(actor) or ""
    action = action.strip()
    if name and action:
        return f"<p><strong>{_escape(name)}</strong> {_escape(action)}</p>"
    if name:
        return f"<p><strong>{_escape(name)}</strong></p>"
    if action:
        return f"<p>{_escape(action)}</p>"
    return ""


def _pin_content(content_items: Any) -> str:
    """Assemble a pin (想法) body from its typed content parts."""
    if not isinstance(content_items, list):
        return ""
    parts: list[str] = []
    for item in content_items:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            parts.append(_pin_text(item.get("own_text")))
        elif kind == "image":
            url = str(item.get("url") or "").replace("xl", "r")
            if url:
                parts.append(f'<p><img src="{_escape(url)}"/></p>')
        elif kind == "link":
            url = str(item.get("url") or "")
            if url:
                title = _escape(item.get("title") or url)
                parts.append(f'<p><a href="{_escape(url)}">{title}</a></p>')
        elif kind == "video":
            playlist = item.get("playlist")
            video_url = _video_url(playlist)
            if video_url:
                # `video` is not in the sanitiser allowlist (ADR 0007); a link
                # survives it and still reaches the content.
                parts.append(f'<p><a href="{_escape(video_url)}">视频</a></p>')
        elif kind == "link_card":
            url = str(item.get("url") or "").split("?", 1)[0]
            if url:
                parts.append(f'<p><a href="{_escape(url)}">{_escape(url)}</a></p>')
    return "".join(parts)


def _video_url(playlist: Any) -> str | None:
    """The playable URL from a pin playlist; the second entry is preferred."""
    if not isinstance(playlist, list) or not playlist:
        return None
    candidates = [playlist[1] if len(playlist) > 1 else None, playlist[0]]
    for candidate in candidates:
        if isinstance(candidate, dict) and candidate.get("url"):
            return str(candidate["url"])
    return None


def _pin_text(text: Any) -> str:
    """Pin text arrives either as rich HTML or as plain lines; pass or wrap."""
    if not text:
        return ""
    trimmed = str(text).strip()
    if not trimmed:
        return ""
    if "<" in trimmed and ">" in trimmed:
        return trimmed
    escaped = (
        html_module.escape(trimmed).replace("\r\n", "<br/>").replace("\n", "<br/>")
    )
    return f"<p>{escaped}</p>"


def _pin_title(target: dict[str, Any], content_html: str) -> str:
    """A pin's whole body is not a title; prefer the excerpt, capped at 60."""
    excerpt = _strip_tags(_text(target.get("excerpt_title")))
    if excerpt:
        return _truncate(html_module.unescape(excerpt), PIN_TITLE_MAX)
    body = _strip_tags(
        content_html.replace("<br>", " ").replace("<br/>", " ").replace("<br />", " ")
    )
    body = re.sub(r"\s+", " ", body).strip()
    if body:
        return _truncate(html_module.unescape(body), PIN_TITLE_MAX)
    return "想法"


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}…"


# --------------------------------------------------------------------------
# Content processing
# --------------------------------------------------------------------------


def _process_html(content: Any) -> str | None:
    """Light cleanup before the store-time sanitiser (ADR 0007) runs.

    What the reference adapter's HTML pass does and why each survives here:

    - Zhihu wraps outbound links in `link.zhihu.com/?target=` redirects;
      unwrapping restores the real target while the sanitiser would only
      keep the redirect.
    - `data-actualsrc`/`data-original` carry the full-size image the `src`
      attribute downgrades; the sanitiser drops data-* attributes, so the
      choice must happen here or the reader gets the thumbnail.
    - Empty paragraphs (`<p><br></p>`, Zhihu's editor markers) are dropped;
      paragraphs carrying an image are kept.

    Loose-text folding from the reference is not ported: it is cosmetic, and
    the sanitiser plus readers handle unwrapped text without it.
    """
    if not content:
        return None
    try:
        root = lxml_html.fragment_fromstring(str(content), create_parent="div")
    except etree.LxmlError:
        return str(content)

    for element in list(root.iter("noscript")):
        element.drop_tree()

    for anchor in list(root.iter("a")):
        href = anchor.get("href") or ""
        if "link.zhihu.com/?target=" in href:
            real = _unwrap_redirect(href)
            if real:
                anchor.set("href", real)

    for image in list(root.iter("img")):
        src = (
            image.get("data-actualsrc")
            or image.get("data-original")
            or image.get("src")
        )
        for attribute in ("data-actualsrc", "data-original", "width", "height"):
            image.attrib.pop(attribute, None)
        if not src:
            image.drop_tree()
        else:
            image.set("src", _fix_image_url(src))

    for paragraph in list(root.iter("p")):
        if paragraph.text_content().strip():
            continue
        if paragraph.find(".//img") is not None:
            continue
        paragraph.drop_tree()

    return "".join(etree.tostring(child, encoding="unicode") for child in root).strip()


def _unwrap_redirect(href: str) -> str | None:
    try:
        query = parse_qs(urlsplit(href).query)
        targets = query.get("target")
        return targets[0] if targets and targets[0] else None
    except ValueError:
        return None


def _fix_image_url(src: str) -> str:
    """Drop the size-suffix and resize params Zhihu appends to image URLs."""
    base = src.split("?", 1)[0]
    for suffix in ("_b.jpg", "_r.jpg", "_720w.jpg"):
        base = base.replace(suffix, ".jpg")
    return base


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _feed_title(kind: str, item: dict[str, Any], *, fallback: Any = None) -> str:
    """The name the feed carries, from the entry's own author when present."""
    named: dict[str, str] = {
        "activities": "的知乎动态",
        "answers": "的知乎回答",
        "posts": " 的知乎文章",
    }
    if kind == "activities":
        name = _name_of(item.get("actor"))
    else:
        name = _name_of(item.get("author"))
    if not name:
        name = _text(fallback)
    return f"{name}{named[kind]}"


def _id_of(item: dict[str, Any]) -> int | str | None:
    value = item.get("id")
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _name_of(actor: Any) -> str | None:
    if not isinstance(actor, dict):
        return None
    name = actor.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    token = actor.get("url_token")
    if isinstance(token, str) and token.strip():
        return token.strip()
    return None


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _strip_tags(value: str) -> str:
    return re.sub(r"<[^>]*>", "", value).strip()


def _escape(value: Any) -> str:
    return html_module.escape(str(value if value is not None else ""), quote=True)


def _unix_to_dt(value: Any) -> datetime | None:
    """Unix seconds → an aware UTC datetime; anything else is not a date."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return datetime.fromtimestamp(int(value), tz=UTC)


@HOOKIMPL
def source_config() -> Manifest:
    """The zhihu collector's manifest."""
    return Manifest(
        id="zhihu",
        version="0.1.0",
        name="知乎用户内容",
        description="知乎用户动态/回答/文章（登录型 + x-zse-96 签名，ADR 0014）。",
        example_url="https://www.zhihu.com/people/example",
        features=frozenset({"fetch", "signed", "cookie"}),
        required_config=(
            ConfigRequirement(
                env_name="PEARSS_ZHIHU_COOKIES",
                description="知乎 Cookie（须含 d_c0；或经 CookieCloud）",
            ),
        ),
    )


@HOOKIMPL
def collector_id() -> str:
    """The stable registration key (ADR 0017)."""
    return "zhihu"


@HOOKIMPL
def backfill() -> BackfillHooks | None:
    """This origin has no backward walk."""
    return None
