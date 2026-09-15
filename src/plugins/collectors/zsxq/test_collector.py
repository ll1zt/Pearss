"""zsxq collector tests: URL routing, fixture-driven parsing, fetch merge.

The fixtures are synthetic API payloads shaped like the real endpoints; the
shaping expectations (topic types, q&a blockquotes, title picking) come from
the reference `earss_source_zsxq` adapter.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from adapters.raw_payload import RawPayload
from core.enums import ErrorClass
from core.plugins.hooks import CollectorError
from plugins.collectors.zsxq import client as zsxq_client
from plugins.collectors.zsxq import collector
from plugins.collectors.zsxq.collector import match_url, resolve_source

FIXTURES = Path(__file__).parent / "fixtures"
SOURCE_ID = "src-zsxq-1"
GROUP_ID = "88855458825252"


def _fixture(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    return data


def _payload(name: str, scope: str = "all") -> RawPayload:
    """A merged payload, exactly what this collector's own fetch produces."""
    topics_path = collector.topics_api_path(
        collector.SourceRoute(group_id=GROUP_ID, scope=scope)
    )
    group_resp = _fixture("group.json")["resp_data"]
    topics_resp = _fixture(name)["resp_data"]
    merged = {
        "group": group_resp.get("group", {}),
        "topics": topics_resp.get("topics", []),
    }
    return RawPayload(
        url=f"{zsxq_client.BASE}{topics_path}",
        status_code=200,
        body=json.dumps(merged, ensure_ascii=False).encode(),
        source_id=SOURCE_ID,
    )


# --------------------------------------------------------------------------
# URL routing
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "url",
    [
        "https://wx.zsxq.com/dweb2/index/group/88855458825252",
        "https://wx.zsxq.com/dweb2/index/group/88855458825252?scope=digests",
        "https://wx.zsxq.com/dweb2/index/group/88855458825252/",
    ],
)
def test_match_url_accepts_group_pages(url: str) -> None:
    assert collector.match_url(url) is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/dweb2/index/group/88855458825252",
        "https://api.zsxq.com/v2/groups/88855458825252/topics",
        "https://wx.zsxq.com/dweb2/index/user/5a4b3c",
        "https://wx.zsxq.com/dweb2/index/group/abc123",
        "https://wx.zsxq.com/dweb2/index/group/12",
        "https://wx.zsxq.com/dweb2/index/group/88855458825252?scope=everything",
    ],
)
def test_match_url_rejects_everything_else(url: str) -> None:
    assert collector.match_url(url) is False


@pytest.mark.unit
def test_a_bare_group_url_means_the_all_scope() -> None:
    route = collector.resolve_source(
        "https://wx.zsxq.com/dweb2/index/group/88855458825252/"
    )
    assert (route.group_id, route.scope) == (GROUP_ID, "all")


@pytest.mark.unit
def test_the_topics_query_carries_scope_and_count() -> None:
    path = collector.topics_api_path(
        collector.SourceRoute(group_id=GROUP_ID, scope="digests")
    )
    assert path == f"/groups/{GROUP_ID}/topics?scope=digests&count=20"


# --------------------------------------------------------------------------
# The fetch hook merge
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_fetch_merges_group_and_topics_into_one_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_get_json(path: str, **kwargs: object) -> RawPayload:
        calls.append(path)
        name = (
            "group.json"
            if path.startswith("/groups/") and "/topics" not in path
            else "topics_valid.json"
        )
        # The real client.get_json returns the unwrapped resp_data.
        resp_data = json.loads((FIXTURES / name).read_text())["resp_data"]
        return RawPayload(
            url=f"{zsxq_client.BASE}{path}",
            status_code=200,
            body=json.dumps(resp_data).encode(),
            source_id=str(kwargs["source_id"]),
        )

    monkeypatch.setattr(zsxq_client, "get_json", fake_get_json)

    raw = collector.fetch(
        f"https://wx.zsxq.com/dweb2/index/group/{GROUP_ID}",
        client=httpx.Client(),
        source_id=SOURCE_ID,
    )

    assert [call.split("?")[0] for call in calls] == [
        f"/groups/{GROUP_ID}",
        f"/groups/{GROUP_ID}/topics",
    ]
    merged = json.loads(raw.text())
    assert merged["group"]["name"] == "示例星球"
    assert len(merged["topics"]) == 7
    assert raw.source_id == SOURCE_ID


# --------------------------------------------------------------------------
# Fixture parsing
# --------------------------------------------------------------------------


@pytest.mark.fixture
def test_topics_fixture_covers_every_rendered_type() -> None:
    drafts = collector.parse(_payload("topics_valid.json"))

    # 7 topics minus the one without a topic_id.
    assert len(drafts) == 6
    assert [d.external_id for d in drafts] == [
        f"topic:{tid}" for tid in (5100001, 5100002, 5100003, 5100004, 5100005, 5100006)
    ]
    assert all(d.source_id == SOURCE_ID for d in drafts)
    assert all(d.canonical_url.startswith("https://wx.zsxq.com/topic/") for d in drafts)


@pytest.mark.fixture
def test_a_talk_renders_pseudo_tags_and_images() -> None:
    first = collector.parse(_payload("topics_valid.json"))[0]

    # The first line is the title; the web pseudo-tag becomes a real anchor
    # with its percent-encoded href/title decoded.
    assert first.title == "第一行分享"
    content = first.content or ""
    assert "第二行带链接 <br>" in content or "第二行带链接" in content
    assert '<a href="https://example.com/post">示例链接</a>' in content
    assert "<span>测试话题</span>" in content
    # Original wins when offered; the second image only has a thumbnail and
    # falls back to it.
    assert 'src="https://images.zsxq.com/f_example-original"' in content
    assert 'src="https://images.zsxq.com/f_example-thumb"' in content
    assert first.published_at == datetime(2026, 1, 2, 2, 0, tzinfo=UTC)
    assert first.metadata["feed_title"] == "知识星球 - 示例星球"


@pytest.mark.fixture
def test_answered_qa_carries_the_answerer_as_author() -> None:
    drafts = collector.parse(_payload("topics_valid.json"))

    answered = drafts[1]
    assert "<blockquote>提问者甲 提问：" in (answered.content or "")
    assert "星主乙 回答：" in (answered.content or "")
    assert answered.metadata["author"] == "星主乙"

    unanswered = drafts[2]
    assert "星主乙" not in (unanswered.content or "")
    assert unanswered.metadata["author"] == "提问者丙"


@pytest.mark.fixture
def test_an_unknown_topic_type_is_title_only() -> None:
    ebook = collector.parse(_payload("topics_valid.json"))[5]
    assert ebook.title == "一本示例电子书"
    assert ebook.content == "", "unknown types get no invented content"


@pytest.mark.fixture
def test_a_digested_scope_names_itself() -> None:
    drafts = collector.parse(_payload("topics_valid.json", scope="digests"))
    assert drafts[0].metadata["feed_title"] == "知识星球 - 示例星球 (digests)"


@pytest.mark.fixture
def test_an_empty_group_is_legitimate() -> None:
    assert collector.parse(_payload("topics_empty.json")) == []


@pytest.mark.fixture
def test_only_unusable_topics_is_not_a_quiet_empty() -> None:
    payload = _payload("topics_valid.json")
    merged = json.loads(payload.text())
    merged["topics"] = [{"type": "talk", "talk": {"text": "no id here"}}]
    broken = RawPayload(
        url=payload.url,
        status_code=200,
        body=json.dumps(merged).encode(),
        source_id=SOURCE_ID,
    )
    with pytest.raises(CollectorError) as exc_info:
        collector.parse(broken)
    assert exc_info.value.error_class is ErrorClass.STRUCTURE_CHANGED


@pytest.mark.unit
def test_resolve_accepts_the_bare_group_form() -> None:
    """The address-bar form /group/<id> resolves like the dweb2 one.

    Users paste the URL the web app shows; both spellings address the same
    page, and refusing the short form made the type inference fall back to
    rss and the rss collector try to parse the login page as a feed.
    """
    route = resolve_source("https://wx.zsxq.com/group/28888555855451")
    assert route.group_id == "28888555855451"
    assert route.scope == "all"
    assert match_url("https://wx.zsxq.com/group/28888555855451")
    # The dweb2 form and its scope parameter keep working.
    scoped = resolve_source(
        "https://wx.zsxq.com/dweb2/index/group/28888555855451?scope=digests"
    )
    assert scoped.scope == "digests"
