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

import base64
import json
import pathlib
import struct
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
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
    """AC-59 / AC-93: the body match and the title match arrive in one answer."""
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


# --------------------------------------------------------------------------
# AC-105..107: the fused order, from the same entry point
#
# The stand-in model is enough for these: what is under test is the *wiring* --
# that the route encodes the query, hands it to both paths and returns the fused
# order -- not the model's semantics, which `test_vector_search.py` checks against
# the real weights.
# --------------------------------------------------------------------------


def _chained_source(session: Session) -> str:
    source = create_source(
        session,
        source_type="rss",
        url="https://example.test/hybrid-surface",
        canonical_url="https://example.test/hybrid-surface",
        schedule_policy="{}",
        pipeline_chain=json.dumps([{"plugin_id": "embed", "kind": "embedding"}]),
        tenant_id="default",
        at="2026-01-01T00:00:00.000000Z",
    )
    session.commit()
    return source.id


def _with_vector(
    session: Session,
    *,
    source_id: str,
    external_id: str,
    title: str,
    content: str,
    vector: list[float],
) -> None:
    from adapters.db_models import ItemEnrichment
    from adapters.filestore import store_body
    from adapters.repository.enrichment import chain_for_source
    from app.enrichment_worker import _open_gate_if_complete

    item = normalise_and_store(
        session,
        [
            ItemDraft(
                source_id=source_id,
                external_id=external_id,
                title=title,
                canonical_url=f"https://example.test/{external_id}",
                published_at=None,
                content=content,
            )
        ],
        chain=chain_for_source(session, source_id=source_id),
    )[0].result.item
    session.commit()
    session.execute(
        sa.update(ItemEnrichment)
        .where(ItemEnrichment.item_id == item.id)
        .values(
            status="succeeded",
            payload_ref=store_body(
                base64.b64encode(struct.pack(f"<{len(vector)}f", *vector)).decode(
                    "ascii"
                )
            ),
        )
    )
    session.commit()
    _open_gate_if_complete(session, item_id=item.id, content_hash=item.content_hash)
    session.commit()


def _basis(axis: int, *, width: int = 384) -> list[float]:
    vector = [0.0] * width
    vector[axis] = 1.0
    return vector


@pytest.mark.asyncio
async def test_hybrid_without_a_query_is_refused(
    client: httpx.AsyncClient,
) -> None:
    """The same rule as relevance, and for one more reason: nothing to encode."""
    response = await client.get("/v1/items", params={"sort": "hybrid"})

    assert response.status_code == 422, response.text
    assert "hybrid_without_query" in response.text


@pytest.mark.asyncio
async def test_hybrid_returns_the_fused_order(
    client: httpx.AsyncClient,
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-105 at the entry point: both paths contribute to one answer."""
    monkeypatch.setenv("PEARSS_EMBEDDING_MODEL_DIR", str(_EMBED_FIXTURES))
    # The vectors come from the same encoder the route will use, rather than from
    # a guessed direction: the stand-in's weights are random, so "this text points
    # along axis 0" is not a fact about it. The semantic item is handed the
    # query's own vector, which makes it the nearest by construction.
    from plugins.processors.embed.encoder import Encoder

    encoder = Encoder(_EMBED_FIXTURES)
    query_vector = encoder.encode("内核调度器", as_query=True)
    with factory() as session:
        source_id = _chained_source(session)
        _with_vector(
            session,
            source_id=source_id,
            external_id="keyword",
            title="内核调度器如何工作",
            content="内核调度器如何工作",
            vector=encoder.encode("something else entirely"),
        )
        _with_vector(
            session,
            source_id=source_id,
            external_id="semantic-only",
            title="Linux scheduler internals",
            content="Linux scheduler internals",
            vector=query_vector,
        )

    titles = await _titles(client, full_text="内核调度器", sort="hybrid")

    assert titles == {"内核调度器如何工作", "Linux scheduler internals"}, titles


@pytest.mark.asyncio
async def test_hybrid_without_a_usable_model_says_so(
    client: httpx.AsyncClient, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-61: a missing model is a 503 with a command, not a silent lexical answer.

    A caller who asked for hybrid and got keyword-only results would read them as
    fused, and the recall they were asking for is exactly the difference.
    """
    monkeypatch.setenv("PEARSS_EMBEDDING_MODEL_DIR", str(tmp_path / "absent"))

    response = await client.get(
        "/v1/items", params={"full_text": "kernel", "sort": "hybrid"}
    )

    assert response.status_code == 503, response.text
    assert "embedding_model_unavailable" in response.text
    assert "just fetch-model" in response.text, "the suggestion has to be the fix"


_EMBED_FIXTURES = (
    pathlib.Path(__file__).resolve().parents[2]
    / "src"
    / "plugins"
    / "processors"
    / "embed"
    / "fixtures"
)
