"""Zhihu activity targets the recorded fixture does not carry.

`activities_valid.json` covers four target types -- answer, pin, article,
question -- which leaves the *rendering* of the rest unexercised. Those branches
are not exotic: 收藏夹 (collection), 专栏 (column), 话题 (topic), Live and
圆桌 (roundtable) all appear in a real member's activity stream, and each one
builds its own title fallback, link and body.

They are tested here through `parse` rather than by calling the shaping helpers
directly, because the behaviour a reader depends on is "this activity becomes
this draft". The payloads are synthetic but shaped like the API's: the point is
the mapping from target kind to draft, and a synthetic target states that
mapping without pretending to be a recording.

The `_id_of` guards are covered here too. Every branch of the dispatcher starts
with `if target_id is None: return None`, and without a test a mutation removing
any one of them would publish a draft whose link is
`https://www.zhihu.com/question/None`.

One detail the recorded fixture makes easy to forget: every activity body is
prefixed with the actor card, so a target that renders no body of its own still
produces `<p>…</p>`. The assertions below therefore look *inside* the content
rather than expecting an empty string.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from adapters.raw_payload import RawPayload
from core.enums import ErrorClass
from core.plugins.hooks import CollectorError
from plugins.collectors.zhihu import collector

pytestmark = pytest.mark.unit

SOURCE_ID = "src-zhihu-activities"
ACTIVITIES_URL = collector.BASE + "/api/v3/moments/example/activities"

# An action every classifier accepts as "create", so the only thing a test
# varies is the target.
_CREATE_ACTION = "发布了想法"

_ACTOR = {"name": "示例用户", "url_token": "example"}


def _payload(targets: list[Any], *, action: str = _CREATE_ACTION) -> bytes:
    body = {
        "paging": {"is_end": True},
        "data": [
            {
                "id": 9001,
                "action_text": action,
                "verb": "create",
                "actor": _ACTOR,
                "target": target,
            }
            for target in targets
        ],
    }
    return json.dumps(body).encode("utf-8")


def _parse(targets: list[Any], *, action: str = _CREATE_ACTION) -> list[Any]:
    return collector.parse(
        RawPayload(
            url=ACTIVITIES_URL,
            status_code=200,
            body=_payload(targets, action=action),
            source_id=SOURCE_ID,
        )
    )


def _dropped(targets: list[Any], *, action: str = _CREATE_ACTION) -> None:
    """Assert every target was skipped, which `parse` reports as structure_changed.

    A payload where nothing survives is not an empty result: `parse`'s contract
    is that "the API changed shape" and "the user has no content" are different
    situations, and only the latter is an empty list. So a test for a dropped
    target must either mix in a usable one or expect the raised error -- an
    assertion of `== []` would be asserting the very conflation the code
    refuses to make.
    """
    with pytest.raises(CollectorError) as excinfo:
        collector.parse(
            RawPayload(
                url=ACTIVITIES_URL,
                status_code=200,
                body=_payload(targets, action=action),
                source_id=SOURCE_ID,
            )
        )
    assert excinfo.value.error_class is ErrorClass.STRUCTURE_CHANGED
    assert excinfo.value.detail == f"all {len(targets)} entries lacked a usable shape"


def _only_draft(target: dict[str, Any], *, action: str = _CREATE_ACTION) -> Any:
    drafts = _parse([target], action=action)
    assert len(drafts) == 1, drafts
    return drafts[0]


#: A target every branch renders, so a test can pair an unusable one with a
#: usable one and observe that the unusable one alone was dropped.
_KEEPER: dict[str, Any] = {"type": "collection", "id": 9999, "title": "保留"}


# --------------------------------------------------------------------------
# The target kinds the recorded fixture lacks
# --------------------------------------------------------------------------


def test_a_collection_target_links_to_the_collection() -> None:
    """A 收藏夹 carries no body of its own, only what it is called."""
    draft = _only_draft({"type": "collection", "id": 7001, "title": "我的收藏夹"})

    assert draft.external_id == "activity:9001"
    assert draft.canonical_url == "https://www.zhihu.com/collection/7001"
    assert "我的收藏夹" in draft.title
    # The actor card is the whole body; no target content is invented for it.
    assert "<img" not in draft.content
    assert "示例用户" in draft.content


def test_a_column_target_renders_its_intro_and_image() -> None:
    draft = _only_draft(
        {
            "type": "column",
            "id": 7002,
            "title": "示例专栏",
            "intro": "一个专栏的简介",
            "image_url": "https://pic.example/column.jpg",
        }
    )

    assert draft.canonical_url == "https://zhuanlan.zhihu.com/7002"
    assert "一个专栏的简介" in draft.content
    assert "https://pic.example/column.jpg" in draft.content


def test_a_column_target_without_an_image_still_renders() -> None:
    """The image is optional; the intro alone must not produce a broken body."""
    draft = _only_draft(
        {"type": "column", "id": 7003, "title": "无图专栏", "intro": "只有简介"}
    )

    assert "只有简介" in draft.content
    assert "<img" not in draft.content


def test_a_topic_target_includes_its_follower_count() -> None:
    draft = _only_draft(
        {
            "type": "topic",
            "id": 7004,
            "name": "示例话题",
            "introduction": "话题简介",
            "followers_count": 12345,
        }
    )

    assert draft.canonical_url == "https://www.zhihu.com/topic/7004"
    assert "示例话题" in draft.title
    assert "12345" in draft.content


def test_a_live_target_escapes_its_description() -> None:
    """A description is raw text, not markup.

    `_text` strips tags, but the newline-to-`<br/>` conversion means the result
    is interpreted as HTML -- so a description containing markup must be
    escaped or a target could inject it into the body.
    """
    draft = _only_draft(
        {
            "type": "live",
            "id": 7005,
            "subject": "示例 Live",
            "description": "第一行\n第二行 <script>alert(1)</script>",
        }
    )

    assert draft.canonical_url == "https://www.zhihu.com/lives/7005"
    assert "第一行<br/>第二行" in draft.content
    assert "<script>" not in draft.content


def test_a_live_target_without_a_description_adds_no_body() -> None:
    draft = _only_draft({"type": "live", "id": 7006, "subject": "空 Live"})

    assert "空 Live" in draft.title
    # Only the actor card remains, so nothing from the target leaked in.
    assert "第一行" not in draft.content


def test_a_roundtable_target_links_to_the_roundtable() -> None:
    draft = _only_draft(
        {
            "type": "roundtable",
            "id": 7007,
            "name": "示例圆桌",
            "description": "<p>圆桌描述</p>",
        }
    )

    assert draft.canonical_url == "https://www.zhihu.com/roundtable/7007"
    assert "圆桌描述" in draft.content


def test_an_ebook_target_is_dropped() -> None:
    """试读电子书 is not in the dispatcher, so it renders nothing.

    Recorded behaviour rather than an endorsement: the recorded fixture has
    this exact target and the test beside it asserts it is dropped. Stating it
    here means a future addition of ebook support is a deliberate change with a
    failing test, not a silent one.
    """
    _dropped([{"type": "ebook", "id": 7008, "title": "示例电子书"}])

    # Paired with a renderable target, the drop is visible as an omission
    # rather than as an all-unusable payload.
    drafts = _parse([{"type": "ebook", "id": 7008, "title": "示例电子书"}, _KEEPER])
    assert len(drafts) == 1
    assert "保留" in drafts[0].title


# --------------------------------------------------------------------------
# Targets with no usable id
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind",
    [
        "answer",
        "article",
        "question",
        "pin",
        "collection",
        "column",
        "topic",
        "live",
        "roundtable",
    ],
)
def test_a_target_without_an_id_is_dropped(kind: str) -> None:
    """Every branch guards on the id, and each guard is load-bearing.

    Without it the draft's link and external_id would interpolate `None`, so a
    client would show "https://www.zhihu.com/question/None" -- and the item's
    identity would collide with every other id-less target of the same kind.
    """
    target: dict[str, Any] = {"type": kind, "title": "无 id", "name": "无 id"}
    if kind == "answer":
        target["question"] = {"id": 1, "title": "q"}

    drafts = _parse([target, _KEEPER])
    assert [draft.canonical_url for draft in drafts] == [
        "https://www.zhihu.com/collection/9999"
    ]


@pytest.mark.parametrize("bad_id", [None, 0, -1, True, False, "", "   ", [], {}])
def test_an_unusable_id_value_counts_as_missing(bad_id: Any) -> None:
    """`_id_of` rejects more than None, and each rejection is load-bearing.

    A boolean is an `int` in Python, so `true` would otherwise become id 1 and
    collide with the real id 1. Zero and negative ids are not real, and a blank
    string would produce a link ending in a space.
    """
    drafts = _parse([{"type": "collection", "id": bad_id, "title": "x"}, _KEEPER])
    assert len(drafts) == 1
    assert drafts[0].canonical_url.endswith("/9999")


def test_an_answer_without_its_question_is_dropped() -> None:
    """An answer's URL needs the question id, which is not the answer's id.

    The answer branch is the only one that depends on a second object, so it
    has a guard the others do not.
    """
    for target in (
        {"type": "answer", "id": 8001, "content": "<p>x</p>"},
        {"type": "answer", "id": 8001, "question": {}},
        {"type": "answer", "id": 8001, "question": "not an object"},
    ):
        drafts = _parse([target, _KEEPER])
        assert len(drafts) == 1, target
        assert "answer" not in drafts[0].external_id, target


def test_a_non_object_target_is_dropped() -> None:
    _dropped(
        [
            {"id": 1, "action_text": "赞同了回答", "target": "not an object"},
            {"id": 2, "action_text": "赞同了回答", "target": None},
            {"id": 3, "action_text": "赞同了回答"},
        ]
    )


def test_a_non_object_activity_is_dropped() -> None:
    _dropped(["not an object", 42, None])


# --------------------------------------------------------------------------
# Pin content parts
# --------------------------------------------------------------------------


def test_a_pin_link_part_becomes_an_anchor() -> None:
    draft = _only_draft(
        {
            "type": "pin",
            "id": 7100,
            "content": [
                {"type": "link", "url": "https://example.com/a", "title": "示例链接"}
            ],
        }
    )

    assert '<a href="https://example.com/a">示例链接</a>' in draft.content


def test_a_pin_link_card_drops_its_query_string() -> None:
    """A card URL is a tracking-carrying redirect; only the path identifies it."""
    draft = _only_draft(
        {
            "type": "pin",
            "id": 7101,
            "content": [
                {"type": "link_card", "url": "https://example.com/a?utm_source=x&id=1"}
            ],
        }
    )

    assert "https://example.com/a" in draft.content
    assert "utm_source" not in draft.content


def test_a_pin_image_is_upsized() -> None:
    draft = _only_draft(
        {
            "type": "pin",
            "id": 7102,
            "content": [{"type": "image", "url": "https://pic.example/xl.jpg"}],
        }
    )

    # `xl` -> `r` is the full-size variant the reference adapter uses.
    assert "https://pic.example/r.jpg" in draft.content


def test_a_pin_text_part_is_wrapped_as_a_paragraph() -> None:
    """Plain pin text arrives without markup and is escaped into HTML."""
    draft = _only_draft(
        {
            "type": "pin",
            "id": 7103,
            "content": [{"type": "text", "own_text": "第一行\n第二行"}],
        }
    )

    assert "第一行<br/>第二行" in draft.content


def test_a_pin_content_part_of_an_unknown_type_is_ignored() -> None:
    """An unmodelled part is skipped, not rendered as a stray object.

    The API adds content types over time; the collector must degrade to
    dropping the unknown part rather than emitting Python's repr of it.
    """
    draft = _only_draft(
        {
            "type": "pin",
            "id": 7104,
            "content": [
                {"type": "text", "own_text": "保留"},
                {"type": "future_part", "whatever": "丢弃"},
            ],
        }
    )

    assert "保留" in draft.content
    assert "future_part" not in draft.content
    assert "丢弃" not in draft.content


def test_a_pin_with_no_content_parts_renders_only_the_actor_card() -> None:
    draft = _only_draft({"type": "pin", "id": 7105, "content": []})

    assert "示例用户" in draft.content
    assert "<img" not in draft.content
    assert "<a href" not in draft.content


def test_a_pin_video_prefers_the_second_playlist_entry() -> None:
    """The playlist's second entry is the playable one; the first is a poster.

    Asserted as a link, not a `<video>` tag: the sanitiser has no video tag
    (ADR 0007), so a video element would be stripped before the reader saw it.
    """
    draft = _only_draft(
        {
            "type": "pin",
            "id": 7106,
            "content": [
                {
                    "type": "video",
                    "playlist": [
                        {"url": "https://v.example/poster.jpg"},
                        {"url": "https://v.example/playable.m3u8"},
                    ],
                }
            ],
        }
    )

    assert "https://v.example/playable.m3u8" in draft.content
    assert "<video" not in draft.content


def test_a_pin_video_falls_back_to_the_first_entry() -> None:
    draft = _only_draft(
        {
            "type": "pin",
            "id": 7107,
            "content": [
                {"type": "video", "playlist": [{"url": "https://v.example/only.mp4"}]}
            ],
        }
    )

    assert "https://v.example/only.mp4" in draft.content


def test_a_pin_video_without_a_usable_url_adds_no_link() -> None:
    draft = _only_draft(
        {
            "type": "pin",
            "id": 7108,
            "content": [
                {"type": "video", "playlist": []},
                {"type": "video", "playlist": "not a list"},
                {"type": "video", "playlist": [{"no_url": True}]},
            ],
        }
    )

    assert "<a href" not in draft.content
    assert "v.example" not in draft.content
