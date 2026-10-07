"""The full-text index: written where rows change, and never a ghost.

Scenario register (normative source: ADR 0035 decisions 3, 4; issue #79).

  AC-88 用户搜索正文里的中文词能命中
    Given 一个入库的条目, 正文含 "eBPF 在内核中运行沙箱程序", 标题不含 "内核"
    When  用户搜索 "内核"
    Then  该条目被召回(证明搜的是正文, 不只标题)

  AC-89 pending 条目的正文不可被搜到(闸门不变量)
    Given 一个 publish_status=pending 的条目, 正文含 "机密草稿"
    When  用户搜索 "机密草稿"
    Then  不召回; 改成 ready 之后同样的搜索能召回

  AC-90 条目内容变更后, 旧内容不再被搜到
    Given 条目正文从 "旧版本内容" 改为 "新版本内容"
    When  搜索 "旧版本" / "新版本"
    Then  旧的不召回, 新的召回

  AC-91 删除条目后不留鬼影(三条删除路径)
    Given 一个已索引的条目
    When  走 delete_items / delete_source_items / delete_source 之一删除
    Then  它的正文关键词再也搜不到

  AC-92 回填后既有条目可被检索
    Given 一个有存量条目而 FTS 表为空的库
    When  运行回填
    Then  存量条目的正文可被搜索到

**断言的是召回与行状态**(issue 的断言强度要求), 不是「调用了 store/delete 函数」。
删除场景断言的是**查询不再返回**, 那才是业务可观测的东西。

**真实资源**: 真实迁移 SQLite + 真实 filestore(`PEARSS_STORAGE_DIR` 指向临时目录),
条目一律走 `normalise_and_store` 入库, 不手写 INSERT。
"""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from adapters.repository import create_source, timestamp
from adapters.repository.deletion import (
    delete_items,
    delete_source_items,
    purge_origin,
)
from adapters.repository.search_index import (
    deindex_items,
    index_missing,
    matching_item_ids,
)
from app.main import create_engine_for
from app.pipeline import normalise_and_store
from core.enrichment import EnrichmentKind, EnrichmentSpec
from core.models import ItemDraft
from tests.migrated_schema import migrated_database

pytestmark = pytest.mark.integration

BASE = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def factory(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> sessionmaker[Session]:
    """A migrated database and a real file store to put bodies in."""
    url = migrated_database(tmp_path, "search.db")
    monkeypatch.setenv("PEARSS_DATABASE_URL", url)
    monkeypatch.setenv("PEARSS_STORAGE_DIR", str(tmp_path / "storage"))
    return sessionmaker(create_engine_for(url), expire_on_commit=False)


def _source(session: Session, source_id: str = "s1") -> str:
    source = create_source(
        session,
        source_type="rss",
        url=f"https://{source_id}.example/feed",
        canonical_url=f"https://{source_id}.example/feed",
        schedule_policy="{}",
        tenant_id="default",
        at=timestamp(BASE),
    )
    return source.id


def _store(
    session: Session,
    *,
    source_id: str,
    guid: str,
    title: str,
    body: str,
    chain: tuple[Any, ...] = (),
) -> str:
    """One item through the production write path, returning its id."""
    drafts = [
        ItemDraft(
            origin_id=source_id,
            external_id=guid,
            title=title,
            canonical_url=f"https://x.example/{guid}",
            published_at=BASE,
            content=body,
        )
    ]
    ingested = normalise_and_store(session, drafts, tenant_id="default", chain=chain)
    return ingested[0].result.item.id


def _promote(session: Session, *, item_id: str, source_id: str) -> None:
    """Open the gate the way a completed chain does: status *and* readiness.

    ADR 0061: visibility is the ready row, so flipping `publish_status` alone
    would leave a test asserting about a column nothing reads any more.
    """
    from adapters.repository import mark_subscription_ready

    session.execute(
        sa.text("update item set publish_status = 'ready' where id = :id"),
        {"id": item_id},
    )
    mark_subscription_ready(
        session,
        item_id=item_id,
        subscription_id=source_id,
        at="2026-01-01T00:00:00.000000Z",
    )
    session.commit()


def _found(session: Session, query: str) -> set[str]:
    return set(matching_item_ids(session, query=query, limit=20))


# --------------------------------------------------------------------------
# AC-88: the body is searchable, which is the whole point
# --------------------------------------------------------------------------


def test_a_word_only_in_the_body_is_found(factory: sessionmaker[Session]) -> None:
    """AC-88: `q` matched the title only, so the body was unfindable."""
    with factory() as session:
        source_id = _source(session)
        item_id = _store(
            session,
            source_id=source_id,
            guid="body-only",
            title="无关标题",
            body="<p>eBPF 在内核中运行沙箱程序</p>",
        )
        assert item_id in _found(session, "内核"), "the body is not searchable"


# --------------------------------------------------------------------------
# AC-89: the gate, enforced by the join rather than by the write path
# --------------------------------------------------------------------------


def test_a_pending_item_is_not_searchable_until_it_is_published(
    factory: sessionmaker[Session],
) -> None:
    """AC-89: the index holds pending items; visibility is a query-time join.

    Indexing only published items would need a row written the moment an item
    is promoted -- a second copy of the gate, which is what ADR 0035 decision 4
    refuses.
    """
    # A non-translation spec: `_specs_needing_work` only drops translations
    # that are already in their target language, so this one always leaves the
    # gate closed -- which is what these two scenarios need.
    chain = (EnrichmentSpec(plugin_id="summary", kind=EnrichmentKind.SUMMARY),)
    with factory() as session:
        source_id = _source(session)
        item_id = _store(
            session,
            source_id=source_id,
            guid="draft",
            title="草稿",
            body="<p>机密草稿</p>",
            chain=chain,  # a non-empty chain holds the gate closed
        )
        status = session.execute(
            sa.text("select publish_status from item where id = :id"), {"id": item_id}
        ).scalar_one()
        assert status == "pending", status
        assert item_id not in _found(session, "机密草稿")

        _promote(session, item_id=item_id, source_id=source_id)
        assert item_id in _found(session, "机密草稿"), (
            "promoting the item did not make its body searchable, so the gate "
            "is being applied when the index is written rather than when it is read"
        )


# --------------------------------------------------------------------------
# AC-90: a changed body replaces its text instead of accumulating
# --------------------------------------------------------------------------


def test_changing_an_items_body_replaces_its_indexed_text(
    factory: sessionmaker[Session],
) -> None:
    """AC-90: FTS5 has no upsert, so the write path deletes before inserting."""
    # A non-translation spec: `_specs_needing_work` only drops translations
    # that are already in their target language, so this one always leaves the
    # gate closed -- which is what these two scenarios need.
    chain = (EnrichmentSpec(plugin_id="summary", kind=EnrichmentKind.SUMMARY),)
    with factory() as session:
        source_id = _source(session)
        item_id = _store(
            session,
            source_id=source_id,
            guid="edited",
            title="草稿",
            body="<p>旧版本内容</p>",
            chain=chain,
        )
        # The same guid again: the item is still pending, so the store advances
        # it on the new content (the UPDATE_PENDING outcome) rather than
        # creating a second row. A content change can only happen while the item
        # is unpublished -- once it is published the content is untouchable --
        # so the gate has to come down before any of this is searchable.
        again = _store(
            session,
            source_id=source_id,
            guid="edited",
            title="草稿",
            body="<p>新版本内容</p>",
            chain=chain,
        )
        assert again == item_id, "the second store did not update the same item"

        _promote(session, item_id=item_id, source_id=source_id)

        assert item_id in _found(session, "新版本"), "the new body was not indexed"
        assert item_id not in _found(session, "旧版本"), (
            "the old body is still indexed, so a content change appends to the "
            "index instead of replacing it"
        )


# --------------------------------------------------------------------------
# AC-91: no ghost rows, on every path that deletes an item
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["delete_items", "delete_source_items", "delete_source"]
)
def test_deleting_an_item_leaves_no_searchable_ghost(
    factory: sessionmaker[Session], path: str
) -> None:
    """AC-91: `item_fts` is a virtual table, so no cascade reaches it.

    All three deletion paths are exercised, because all three are real: a
    cascade would cover the first two by accident and never the third, and a
    row left behind is a result that cannot be opened (ADR 0021).
    """
    with factory() as session:
        source_id = _source(session)
        item_id = _store(
            session,
            source_id=source_id,
            guid="doomed",
            title="要被删掉的",
            body="<p>鬼影关键词</p>",
        )
        assert item_id in _found(session, "鬼影关键词")

        if path == "delete_items":
            delete_items(session, [item_id])
        elif path == "delete_source_items":
            delete_source_items(session, source_id)
        else:
            purge_origin(session, source_id, reason="the test says so")
        session.commit()

        assert item_id not in _found(session, "鬼影关键词"), (
            f"{path} left the item searchable"
        )


# --------------------------------------------------------------------------
# AC-92: the corpus that predates the index
# --------------------------------------------------------------------------


def test_the_backfill_indexes_items_that_predate_the_index(
    factory: sessionmaker[Session],
) -> None:
    """AC-92: existing rows are invisible to search until something indexes them.

    Simulated by emptying the index after a normal store -- which is exactly the
    state a database that predates migration 0023 is in. The backfill is not a
    migration because filling it means reading a body per item from the
    filestore, and a migration has to stay fast and reversible.
    """
    with factory() as session:
        source_id = _source(session)
        item_id = _store(
            session,
            source_id=source_id,
            guid="legacy",
            title="存量条目",
            body="<p>存量正文关键词</p>",
        )
        deindex_items(session, [item_id])
        session.commit()
        assert item_id not in _found(session, "存量正文关键词")

        indexed = index_missing(session)
        session.commit()

        assert indexed == 1, indexed
        assert item_id in _found(session, "存量正文关键词"), (
            "the backfill did not make the stored body searchable"
        )


# --------------------------------------------------------------------------
# The entry points: the CLI, the scheduler pass, and the degenerate inputs
# --------------------------------------------------------------------------


def test_the_reindex_command_indexes_a_corpus_that_predates_the_index(
    factory: sessionmaker[Session], capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-92 through the operator's entry point, not the library call.

    `just reindex` is what someone runs after upgrading; the loop and the
    `optimize` at the end are part of what it promises, so the assertion is on
    what it printed *and* on what the index can now answer.
    """
    from app import reindex

    with factory() as session:
        source_id = _source(session)
        item_id = _store(
            session,
            source_id=source_id,
            guid="cli",
            title="存量条目",
            body="<p>命令行关键词</p>",
        )
        deindex_items(session, [item_id])
        session.commit()
        assert item_id not in _found(session, "命令行关键词")

    reindex.main()

    printed = capsys.readouterr().out
    assert "reindexed 1 item(s)" in printed, printed
    with factory() as session:
        assert item_id in _found(session, "命令行关键词")


@pytest.mark.parametrize(
    ("value", "expected"),
    [("lots", "batch must be a number"), ("0", "batch must be at least 1")],
)
def test_the_reindex_command_refuses_a_bad_batch(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    value: str,
    expected: str,
) -> None:
    """A bad batch must be a usage error, not a traceback or a silent default.

    Quietly falling back would run the whole corpus in one transaction, which is
    the thing the batch exists to avoid.
    """
    from app import reindex

    monkeypatch.setenv("PEARSS_REINDEX_BATCH", value)
    with pytest.raises(SystemExit) as exit_info:
        reindex.main()
    assert exit_info.value.code == 2
    assert expected in capsys.readouterr().err


def test_the_scheduler_catches_up_items_the_index_is_missing(
    factory: sessionmaker[Session],
) -> None:
    """The writer indexes a changed item; this is what finds an unchanged one.

    Without the pass, an item stored before the index existed -- or one whose
    index write failed after the row committed -- stays invisible to search
    until an operator runs the CLI.
    """
    import asyncio

    import httpx

    from app.scheduler import Scheduler

    with factory() as session:
        source_id = _source(session)
        item_id = _store(
            session,
            source_id=source_id,
            guid="scheduled",
            title="存量条目",
            body="<p>调度器关键词</p>",
        )
        deindex_items(session, [item_id])
        session.commit()
        assert item_id not in _found(session, "调度器关键词")

    scheduler = Scheduler(
        factory,
        client=httpx.Client(
            transport=httpx.MockTransport(lambda _r: httpx.Response(200))
        ),
        tick_seconds=3600,
    )
    asyncio.run(scheduler._index_if_due())

    with factory() as session:
        assert item_id in _found(session, "调度器关键词")


def test_a_query_with_nothing_searchable_matches_nothing(
    factory: sessionmaker[Session],
) -> None:
    """Punctuation alone is not a search.

    `phrase_query` returns an empty expression for it, and an empty MATCH
    expression is a syntax error rather than a wildcard -- so the early return
    is what keeps "type a full stop" from being either an error or every item.
    """
    with factory() as session:
        source_id = _source(session)
        _store(
            session,
            source_id=source_id,
            guid="anything",
            title="随便一条",
            body="<p>正文</p>",
        )
        assert matching_item_ids(session, query="", limit=10) == []
        assert matching_item_ids(session, query="   ...", limit=10) == []


def test_deindexing_an_empty_list_is_a_no_op(factory: sessionmaker[Session]) -> None:
    """The deletion wiring calls this with whatever the delete found."""
    with factory() as session:
        deindex_items(session, [])
        session.commit()
