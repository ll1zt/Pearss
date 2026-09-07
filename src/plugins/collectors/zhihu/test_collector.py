"""Zhihu collector tests: URL routing and fixture-driven parsing.

Fixtures are synthetic API payloads shaped like the real endpoints; the
parsing expectations come from the reference `earss_source_zhihu` adapter's
shaping rules (action tags, dropped noise activities, pin title capping).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from adapters.raw_payload import RawPayload
from core.enums import ErrorClass
from core.plugins.hooks import CollectorError
from plugins.collectors.zhihu import collector

FIXTURES = Path(__file__).parent / "fixtures"
SOURCE_ID = "src-zhihu-1"


def _payload(name: str) -> RawPayload:
    """A payload whose URL is what this collector's own fetch would produce."""
    answers_url = (
        collector.BASE
        + collector.api_path_for(
            collector.SourceRoute(kind="answers", url_token="example")
        )[0]
    )
    api_url = {
        "answers_valid.json": answers_url,
        "answers_empty.json": answers_url,
        "answers_structure_changed.json": answers_url,
        "articles_valid.json": collector.BASE + "/api/v4/members/example/articles",
        "activities_valid.json": collector.BASE + "/api/v3/moments/example/activities",
    }[name]
    return RawPayload(
        url=api_url,
        status_code=200,
        body=(FIXTURES / name).read_bytes(),
        source_id=SOURCE_ID,
    )


# --------------------------------------------------------------------------
# URL routing
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "url",
    [
        "https://www.zhihu.com/people/example",
        "https://www.zhihu.com/people/example/",
        "https://www.zhihu.com/people/example/activities",
        "https://www.zhihu.com/people/marisa.moe/answers",
        "https://www.zhihu.com/people/example/posts",
        "https://www.zhihu.com/org/example-org/posts",
        "https://zhihu.com/people/example/answers",
    ],
)
def test_match_url_accepts_user_pages(url: str) -> None:
    assert collector.match_url(url) is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/people/x",
        "https://www.zhihu.com/question/5501001",
        "https://www.zhihu.com/people/",
        "https://www.zhihu.com/people/ex..ample",
        "https://www.zhihu.com/people/exa$mple",
        "https://www.zhihu.com/org/example",
        "https://www.zhihu.com/people/example/topics",
    ],
)
def test_match_url_rejects_everything_else(url: str) -> None:
    assert collector.match_url(url) is False


@pytest.mark.unit
def test_a_bare_people_url_means_activities() -> None:
    route = collector.resolve_source("https://www.zhihu.com/people/example")
    assert route.kind == "activities"
    assert route.url_token == "example"


@pytest.mark.unit
def test_org_posts_carry_their_usertype() -> None:
    route = collector.resolve_source("https://www.zhihu.com/org/example-org/posts")
    assert (route.kind, route.usertype, route.url_token) == (
        "posts",
        "org",
        "example-org",
    )


@pytest.mark.unit
def test_the_api_path_is_signed_and_sent_identically() -> None:
    path, _ = collector.api_path_for(
        collector.SourceRoute(kind="activities", url_token="example")
    )
    assert (
        path
        == "/api/v3/moments/example/activities?limit=10&desktop=true&ws_qiangzhisafe=0"
    )

    path, _ = collector.api_path_for(
        collector.SourceRoute(kind="answers", url_token="example")
    )
    # The include's [*] is percent-encoded, and this exact string is what the
    # signature binds to — changing either side alone breaks every request.
    assert "include=data%5B%2A%5D.is_normal%2Ccontent" in path


# --------------------------------------------------------------------------
# Fixture parsing
# --------------------------------------------------------------------------


@pytest.mark.fixture
def test_answers_fixture_becomes_drafts() -> None:
    drafts = collector.parse(_payload("answers_valid.json"))

    assert len(drafts) == 2
    first = drafts[0]
    assert first.source_id == SOURCE_ID
    assert first.external_id == "answer:3100001"
    assert first.title == "在单人项目里，如何让约定不靠自觉？"
    assert (
        first.canonical_url == "https://www.zhihu.com/question/5501001/answer/3100001"
    )
    assert first.published_at == datetime.fromtimestamp(1767225600, tz=UTC)
    # Zhihu's empty-paragraph editor marker is gone; the redirect link points
    # at the real target.
    assert "ztext-empty-paragraph" not in (first.content or "")
    assert "link.zhihu.com" not in (first.content or "")
    assert "https://example.com/post" in (first.content or "")
    assert first.metadata["feed_title"] == "示例作者甲的知乎回答"
    assert first.metadata["author"] == "示例作者甲"


@pytest.mark.fixture
def test_activities_fixture_keeps_content_and_drops_noise() -> None:
    drafts = collector.parse(_payload("activities_valid.json"))

    # 赞同回答, 发布了想法, 收藏了文章 stay; 关注/提出了问题/未知 target are dropped.
    assert len(drafts) == 3

    like = drafts[0]
    assert like.external_id == "activity:4100001"
    assert like.title.startswith("[赞同] ")
    assert like.title.endswith("在单人项目里，如何让约定不靠自觉？")
    assert like.canonical_url == "https://www.zhihu.com/question/5501001/answer/3100001"
    # The content author names the item; the actor is credited in the body card.
    assert like.metadata["author"] == "原作者乙"
    assert "<strong>示例用户</strong> 赞同了回答" in (like.content or "")

    pin = drafts[1]
    assert pin.external_id == "activity:4100002"
    assert pin.canonical_url == "https://www.zhihu.com/pin/6100001"
    # The 60-char cap governs the pin-derived title; the action tag sits on top.
    pin_title = pin.title.removeprefix("[创作] ")
    assert len(pin_title) <= collector.PIN_TITLE_MAX + 1  # the ellipsis
    assert pin_title.endswith("…")
    assert "https://v.vzuu.com/video/example-hd" in (pin.content or "")
    assert "<video" not in (pin.content or ""), (
        "video must be a link, the sanitiser has no video tag"
    )

    collect = drafts[2]
    assert collect.title.startswith("[收藏] ")
    assert collect.metadata["author"] == "原作者丙"


@pytest.mark.fixture
def test_articles_fixture_resolves_full_size_images() -> None:
    drafts = collector.parse(_payload("articles_valid.json"))

    assert [d.external_id for d in drafts] == ["article:3200001", "article:3200002"]
    first = drafts[0]
    assert first.canonical_url == "https://zhuanlan.zhihu.com/p/3200001"
    assert 'src="https://picx.zhimg.com/v2-example_big.jpg"' in (first.content or "")
    assert "_small_b.jpg" not in (first.content or "")
    assert "data-actualsrc" not in (first.content or "")
    assert first.metadata["feed_title"] == "示例作者甲 的知乎文章"


@pytest.mark.fixture
def test_an_empty_feed_is_legitimate() -> None:
    assert collector.parse(_payload("answers_empty.json")) == []


@pytest.mark.fixture
def test_a_moved_shape_is_structure_changed() -> None:
    with pytest.raises(CollectorError) as exc_info:
        collector.parse(_payload("answers_structure_changed.json"))
    assert exc_info.value.error_class is ErrorClass.STRUCTURE_CHANGED


@pytest.mark.fixture
def test_only_unusable_entries_is_not_a_quiet_empty() -> None:
    payload = _payload("answers_valid.json")
    body = '{"data": [{"nope": true}, {"question": {"id": 1}}]}'
    broken = RawPayload(
        url=payload.url, status_code=200, body=body.encode(), source_id=SOURCE_ID
    )
    with pytest.raises(CollectorError) as exc_info:
        collector.parse(broken)
    assert exc_info.value.error_class is ErrorClass.STRUCTURE_CHANGED


@pytest.mark.fixture
def test_undecodable_json_is_a_parse_error() -> None:
    payload = _payload("answers_valid.json")
    broken = RawPayload(
        url=payload.url,
        status_code=200,
        body=b"<html>not json</html>",
        source_id=SOURCE_ID,
    )
    with pytest.raises(CollectorError) as exc_info:
        collector.parse(broken)
    assert exc_info.value.error_class is ErrorClass.PARSE
