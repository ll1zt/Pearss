"""Search from the user's entry point: `q` finds titles, `full_text` finds text.

Scenario register (normative source: docs/api/mcp-tool-guide.md, ADR 0035;
issues #80 and #70).

  AC-93 用户用正文里的词能搜到
    Given 条目 A 标题为 "Weekly notes", 正文含 "kernel tracing"
    And   条目 B 标题含 "kernel", 正文无关
    When  用户请求 GET /v1/items?full_text=kernel
    Then  响应同时含 A 与 B(A 靠正文, B 靠标题)

  AC-94 相关性排序把最相关的排在前面
    Given 条目 A 正文多次出现某词, 条目 B 仅一次
    When  请求 full_text=<该词>&sort=relevance
    Then  A 排在 B 之前

  AC-95 无查询时不允许相关性排序(不静默降级)
    Given 无 full_text 参数
    When  请求 sort=relevance
    Then  422 并说明原因

  AC-96 既有 q 参数行为不变(向后兼容)
    Given 条目标题含 "kernel"
    When  请求 GET /v1/items?q=kernel
    Then  仍是标题子串匹配(正文里的词不会命中)

  AC-97 分页游标与排序组合仍正确
    Given 100 条匹配 full_text 的条目
    When  按 limit=10 翻页到底
    Then  并集恰好是全集, 无重复无遗漏

  AC-98 pending 条目不可见(闸门在检索路径同样成立)
    Given 一个 pending 条目正文含独有词
    When  请求 full_text=<该词>
    Then  不返回该条目

**断言的是响应内容与顺序**(issue 的断言强度要求), 不是「SQL 里出现了 MATCH」。
游标场景断言**并集恰好等于全集**——用户可见的结果, 不是 SQL 形态。

**真实资源**: 真实迁移 SQLite + 真实 filestore, 走真实 HTTP 路由; 条目一律经
`normalise_and_store` 入库(那也正是写索引的路径), 不手写 INSERT。
"""

from __future__ import annotations

import pathlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.orm import Session, sessionmaker

from adapters.repository import create_source, timestamp
from app.main import create_app, create_engine_for
from app.pipeline import normalise_and_store
from core.enrichment import EnrichmentKind, EnrichmentSpec
from core.models import ItemDraft
from tests.migrated_schema import migrated_database

pytestmark = pytest.mark.integration

TOKEN = "search-surface-token"
BASE = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def factory(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> sessionmaker[Session]:
    url = migrated_database(tmp_path, "surface.db")
    monkeypatch.setenv("PEARSS_DATABASE_URL", url)
    monkeypatch.setenv("PEARSS_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("PEARSS_API_TOKEN", TOKEN)
    return sessionmaker(create_engine_for(url), expire_on_commit=False)


@pytest_asyncio.fixture
async def client(factory: sessionmaker[Session]) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(factory)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as opened:
        yield opened


def _store(
    session: Session,
    *,
    source_id: str,
    guid: str,
    title: str,
    body: str,
    chain: tuple[Any, ...] = (),
) -> str:
    ingested = normalise_and_store(
        session,
        [
            ItemDraft(
                source_id=source_id,
                external_id=guid,
                title=title,
                canonical_url=f"https://x.example/{guid}",
                published_at=BASE,
                content=body,
            )
        ],
        tenant_id="default",
        chain=chain,
    )
    return ingested[0].result.item.id


def _source(session: Session) -> str:
    return create_source(
        session,
        source_type="rss",
        url="https://search.example/feed",
        canonical_url="https://search.example/feed",
        schedule_policy="{}",
        tenant_id="default",
        at=timestamp(BASE),
    ).id


async def _titles(client: httpx.AsyncClient, **params: Any) -> set[str]:
    response = await client.get("/v1/items", params=params)
    assert response.status_code == 200, response.text
    return {row["title"] for row in response.json()["data"]}


# --------------------------------------------------------------------------
# AC-93: the body is searchable through the route
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_word_in_the_body_is_found(
    client: httpx.AsyncClient, factory: sessionmaker[Session]
) -> None:
    """AC-93: the body match and the title match arrive in one answer."""
    with factory() as session:
        source_id = _source(session)
        _store(
            session,
            source_id=source_id,
            guid="a",
            title="Weekly notes",
            body="<p>kernel tracing</p>",
        )
        _store(
            session,
            source_id=source_id,
            guid="b",
            title="kernel news",
            body="<p>unrelated</p>",
        )

    assert await _titles(client, full_text="kernel") == {"Weekly notes", "kernel news"}


# --------------------------------------------------------------------------
# AC-94: relevance, in the right direction
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_relevance_puts_the_better_match_first(
    client: httpx.AsyncClient, factory: sessionmaker[Session]
) -> None:
    """AC-94: FTS5's bm25 is *negative*, so best-first is ascending.

    Reading that backwards silently reverses the ranking, which is why this
    asserts the order rather than the presence of the two rows.
    """
    with factory() as session:
        source_id = _source(session)
        many = _store(
            session,
            source_id=source_id,
            guid="many",
            title="A",
            body="<p>可观测性 可观测性 可观测性</p>",
        )
        once = _store(
            session,
            source_id=source_id,
            guid="once",
            title="B",
            body="<p>可观测性</p>",
        )

    response = await client.get(
        "/v1/items", params={"full_text": "可观测性", "sort": "relevance"}
    )
    assert response.status_code == 200, response.text
    assert [row["id"] for row in response.json()["data"]] == [many, once]


# --------------------------------------------------------------------------
# AC-95: no silent downgrade
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_relevance_without_a_query_is_refused(client: httpx.AsyncClient) -> None:
    """AC-95: an order by relevance needs something to be relevant to.

    Falling back to the time order would be worse than an error: the caller
    asked for ranked results and would read whatever came back as ranked.
    """
    response = await client.get("/v1/items", params={"sort": "relevance"})
    assert response.status_code == 422, response.text
    assert "relevance_without_query" in response.text


# --------------------------------------------------------------------------
# AC-96: `q` keeps meaning what it meant
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_q_is_still_a_title_substring_match(
    client: httpx.AsyncClient, factory: sessionmaker[Session]
) -> None:
    """AC-96: existing callers keep the behaviour they were written against."""
    with factory() as session:
        source_id = _source(session)
        _store(
            session,
            source_id=source_id,
            guid="a",
            title="Weekly notes",
            body="<p>kernel tracing</p>",
        )
        _store(
            session,
            source_id=source_id,
            guid="b",
            title="kernel news",
            body="<p>unrelated</p>",
        )

    assert await _titles(client, q="kernel") == {"kernel news"}


# --------------------------------------------------------------------------
# AC-97: the keyset survives a ranked order
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paging_a_ranked_search_neither_repeats_nor_skips(
    client: httpx.AsyncClient, factory: sessionmaker[Session]
) -> None:
    """AC-97: 100 matches, ten at a time, and the union is exactly the set.

    Every item matches equally here, so every score ties -- which is the case a
    cursor built only from the score would loop on. The id breaks the tie.
    """
    with factory() as session:
        source_id = _source(session)
        for index in range(100):
            _store(
                session,
                source_id=source_id,
                guid=f"p{index:03d}",
                title=f"Item {index:03d}",
                body="<p>批量关键词</p>",
            )

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(15):
        params: dict[str, Any] = {
            "full_text": "批量关键词",
            "sort": "relevance",
            "limit": 10,
            "fields": "id",
        }
        if cursor is not None:
            params["cursor"] = cursor
        response = await client.get("/v1/items", params=params)
        assert response.status_code == 200, response.text
        body = response.json()
        seen.extend(row["id"] for row in body["data"])
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert len(seen) == 100, f"paged {len(seen)} of 100"
    assert len(set(seen)) == 100, "a row came back on two pages"


# --------------------------------------------------------------------------
# AC-98: the gate holds on the search path too
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_pending_item_is_not_found_by_searching_its_body(
    client: httpx.AsyncClient, factory: sessionmaker[Session]
) -> None:
    """AC-98: the index holds pending items, so the gate has to be in the read."""
    with factory() as session:
        source_id = _source(session)
        _store(
            session,
            source_id=source_id,
            guid="draft",
            title="草稿",
            body="<p>未发布的独有词</p>",
            chain=(EnrichmentSpec(plugin_id="summary", kind=EnrichmentKind.SUMMARY),),
        )

    assert await _titles(client, full_text="未发布的独有词") == set()
