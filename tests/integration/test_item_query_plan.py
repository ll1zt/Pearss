"""The feed's reads must be served by an index, not by a scan and a sort.

Scenario register (normative source: ADR 0035 §9; issue #77).

  AC-78 列表页不再全表扫 + 临时排序
    Given 一个有量的条目集且已 ANALYZE
    When  取最新一页(全量可见集, 按时间倒序)
    Then  查询计划里没有全表扫、没有 temp B-tree 排序
    And   它走的是索引

  AC-79 按源取页同样走索引
    Given 同一批数据
    When  取某一个源的最新一页
    Then  计划同样没有全表扫与临时排序

  AC-80 排序键换了来源, 分页语义不变(边界)
    Given 包含「没有发布日期」(published_at 为 NULL) 的条目
    When  逐页翻完
    Then  没有行被跳过或重复
    And   这些行落在排序键的底值上, 与游标值一致

Why the plan is the assertion and the wall clock is not: at the scale a test can
seed, the two plans are both fast and the *difference* is not observable as
time -- a full scan plus a sort over 300 rows is a fraction of a millisecond.
Measured at 3,000 rows (where it does separate): 1.02ms with the scan and the
temp B-tree, 0.13ms with the index. So a timing bound here would be a test that
cannot fail, which is the thing this project keeps deleting; the plan is
scale-independent and is the mechanism itself.

The SQL is captured from the real query rather than rebuilt here: a test that
spells the query out again passes while the real one drifts, and the shape of
this query -- the coalesce, the gate, the keyset comparison -- is exactly what
makes the index usable or not.
"""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from adapters.repository import create_source, timestamp
from adapters.repository.items_query import ItemFilter, count_matching, query_items
from app.main import create_engine_for
from app.pipeline import normalise_and_store
from core.models import ItemDraft
from tests.migrated_schema import migrated_database

pytestmark = pytest.mark.integration

SOURCES = 4
PER_SOURCE = 75
BASE = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def seeded(tmp_path: pathlib.Path) -> sessionmaker[Session]:
    """A migrated database with enough rows for the planner to have statistics.

    ANALYZE is part of the fixture because the plan depends on it: without
    statistics SQLite has no basis to prefer the index over a scan, which is
    half of what issue #77 is about.
    """
    url = migrated_database(tmp_path, "plan.db")
    engine = create_engine_for(url)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        for index in range(SOURCES):
            source = create_source(
                session,
                source_type="rss",
                url=f"https://plan{index}.example/feed",
                canonical_url=f"https://plan{index}.example/feed",
                schedule_policy="{}",
                tenant_id="default",
                at=timestamp(BASE),
            )
            drafts = [
                ItemDraft(
                    source_id=source.id,
                    external_id=f"i{index}-{offset}",
                    title=f"Item {index}-{offset}",
                    canonical_url=f"https://plan{index}.example/{offset}",
                    # Every fifth row has no date at all: NULL is meaningful
                    # here, and those rows are the ones the sort floor exists
                    # for.
                    published_at=(
                        None
                        if offset % 5 == 0
                        else BASE + timedelta(minutes=index * PER_SOURCE + offset)
                    ),
                    content="<p>body</p>",
                )
                for offset in range(PER_SOURCE)
            ]
            normalise_and_store(session, drafts, tenant_id="default")
        session.commit()
        session.execute(sa.text("ANALYZE"))
        session.commit()
    return factory


def _capture(session: Session, run: Any) -> list[tuple[str, Any]]:
    """Run a repository call and return the (sql, params) it actually emitted.

    Captured through an event rather than rebuilt in the test, so the assertion
    is about the query the application runs. The SQL keeps its placeholders --
    `EXPLAIN QUERY PLAN` needs the parameters too, which is why they come back
    with it.
    """
    emitted: list[tuple[str, Any]] = []
    engine = session.get_bind()

    def record(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: Any
    ) -> None:
        emitted.append((statement, parameters))

    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        run()
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)
    return emitted


def _plan(engine: Any, sql: str, params: Any) -> list[str]:
    """The planner's plan for one captured statement."""
    with engine.connect() as conn:
        return [
            str(row[3])
            for row in conn.exec_driver_sql("EXPLAIN QUERY PLAN " + sql, params)
        ]


def _assert_indexed(plan: list[str], what: str) -> None:
    """No sort, and no *table* scan.

    `SCAN item USING INDEX ix_...` is a full scan of the *index*, which is the
    right plan for "the newest rows, no filter on the sort key": it walks the
    index in order and stops at the limit. What must not appear is a scan of
    the table itself (no `USING`), or a sort into a temporary b-tree.
    """
    joined = " | ".join(plan)
    assert "TEMP B-TREE" not in joined, (
        f"{what} still sorts into a temporary b-tree: {plan}"
    )
    table_scans = [
        statement
        for statement in plan
        if statement.startswith("SCAN item") and "USING INDEX" not in statement
    ]
    assert not table_scans, f"{what} still scans the table: {plan}"
    assert any("USING INDEX" in statement for statement in plan), (
        f"{what} does not use an index: {plan}"
    )


# --------------------------------------------------------------------------
# AC-78 / AC-79: the plans
# --------------------------------------------------------------------------


def test_the_newest_page_is_served_by_an_index(
    seeded: sessionmaker[Session],
) -> None:
    """AC-78: the most common read in the system, and it was a full scan."""
    with seeded() as session:
        emitted = _capture(
            session,
            lambda: query_items(
                session,
                tenant_id="default",
                filter=ItemFilter(),
                descending=True,
                limit=50,
            ),
        )
        sql, params = next(
            (sql, params) for sql, params in emitted if "order by" in sql.lower()
        )
        plan = _plan(session.get_bind(), sql, params)
    _assert_indexed(plan, "the newest-first page")


def test_a_sources_page_is_served_by_an_index(
    seeded: sessionmaker[Session],
) -> None:
    """AC-79: scoping by source is the other shape every reader asks for."""
    with seeded() as session:
        source_id = session.execute(
            sa.text("select source_id from item limit 1")
        ).scalar_one()
        emitted = _capture(
            session,
            lambda: query_items(
                session,
                tenant_id="default",
                filter=ItemFilter(source_id=source_id),
                descending=True,
                limit=50,
            ),
        )
        sql, params = next(
            (sql, params) for sql, params in emitted if "order by" in sql.lower()
        )
        plan = _plan(session.get_bind(), sql, params)
    _assert_indexed(plan, "one source's page")


def test_the_count_does_not_sort(
    seeded: sessionmaker[Session],
) -> None:
    """The count shares the page's filters, so it must not pay for a sort."""
    with seeded() as session:
        counted: list[int] = []
        emitted = _capture(
            session,
            lambda: counted.append(
                count_matching(session, tenant_id="default", filter=ItemFilter())
            ),
        )
        sql, params = next(
            (sql, params) for sql, params in emitted if sql.startswith("SELECT count")
        )
        plan = _plan(session.get_bind(), sql, params)
    assert counted == [SOURCES * PER_SOURCE], counted
    assert "TEMP B-TREE" not in " | ".join(plan), plan


# --------------------------------------------------------------------------
# AC-80: the sort key changed, so the edge case it exists for must still page
# --------------------------------------------------------------------------


def test_items_without_a_date_page_without_being_skipped(
    seeded: sessionmaker[Session],
) -> None:
    """AC-80: NULL is a real state, and the keyset must agree with the sort.

    The ordering moved from `coalesce(published_at, ...)` in the query to a
    generated column, while the cursor value is still computed in Python from
    the row (`published_at or EPOCH_FLOOR`). If the two ever disagreed, the
    boundary would land beside the rows it names and a page would silently
    repeat or skip -- which is what this walks the whole set to rule out.
    """
    seen: list[str] = []
    with seeded() as session:
        cursor: tuple[str, str] | None = None
        for _ in range(SOURCES * PER_SOURCE):
            page = query_items(
                session,
                tenant_id="default",
                filter=ItemFilter(),
                descending=True,
                limit=7,
                cursor=cursor,
            )
            seen.extend(item.id for item in page.items)
            if page.next_cursor is None:
                break
            value, last_id = page.next_cursor.split("|", 1)
            cursor = (value, last_id)

    assert len(seen) == SOURCES * PER_SOURCE, (
        f"paged {len(seen)} of {SOURCES * PER_SOURCE}"
    )
    assert len(set(seen)) == len(seen), "a row was returned twice across pages"

    with seeded() as session:
        undated = [
            row_id
            for (row_id,) in session.execute(
                sa.text("select id from item where published_at is null")
            ).fetchall()
        ]
    assert undated, "the fixture must contain items with no date"
    assert set(undated) <= set(seen), (
        "items with no published date fell out of the pagination: the sort key "
        "and the cursor value disagree"
    )
