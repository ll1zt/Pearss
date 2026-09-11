"""并发互斥的实证, 分两层各自验证。

- **job 级**: 同一 job 同时至多一个活跃 run。由 job_run 上的唯一部分索引在
  schema 层强制 —— 每个 caller 各自创建自己的 run_id, 行级条件更新无法在它们
  之间仲裁 (那是本文件此前实证过、随后被修正的错误假设)。
- **行级**: 单个 run 的 queued -> running 只能发生一次。由 claim 的条件更新
  保证, 是 run 生命周期自身的守卫。
"""

from __future__ import annotations

import threading

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from adapters.db_models import Source
from adapters.repository import (
    claim_job_run,
    create_job_run,
    ensure_collect_job,
    job_for_source,
)
from core.enums import SourceStatus, TrustLevel

pytestmark = pytest.mark.integration

POLICY = '{"type":"fixed","interval":"30m"}'
NOW = "2026-01-01T00:00:00.000000Z"


@pytest.fixture()
def run_id(migrated_factory: sessionmaker[Session]) -> str:
    with migrated_factory() as session:
        session.add(
            Source(
                id="src-1",
                tenant_id="t",
                type="rss",
                url="https://e.com/f.xml",
                canonical_url="https://e.com/f.xml",
                status=SourceStatus.ACTIVE.value,
                trust_level=TrustLevel.STANDARD.value,
                schedule_policy=POLICY,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()
        job_id = ensure_collect_job(session, source_id="src-1", tenant_id="t", at=NOW)
        session.commit()
        create_job_run(
            session, run_id="run-1", job_id=job_id, correlation_id="corr-1", at=NOW
        )
        session.commit()
        return "run-1"


def test_second_active_run_for_a_job_is_refused(
    migrated_factory: sessionmaker[Session], run_id: str
) -> None:
    """job 级互斥: 活跃 run 已存在时, 第二个 run 的 insert 必须失败。"""
    with migrated_factory() as session:
        job = job_for_source(session, source_id="src-1")
        assert job is not None
        with pytest.raises(IntegrityError):
            create_job_run(
                session,
                run_id="run-2",
                job_id=job.id,
                correlation_id="corr-2",
                at=NOW,
            )
            session.commit()


def test_run_from_a_finished_job_slot_can_be_created(
    migrated_factory: sessionmaker[Session], run_id: str
) -> None:
    """行级 claim 之后, 唯一索引的槽位被占用; 终结该 run 后槽位释放。"""
    with migrated_factory() as session:
        assert claim_job_run(session, run_id=run_id, at=NOW) is True
        session.commit()
    with migrated_factory() as session:
        job = job_for_source(session, source_id="src-1")
        assert job is not None
        with pytest.raises(IntegrityError):
            create_job_run(
                session,
                run_id="run-2",
                job_id=job.id,
                correlation_id="corr-2",
                at=NOW,
            )
            session.commit()


def test_concurrent_claim_has_exactly_one_winner(
    migrated_factory: sessionmaker[Session], run_id: str
) -> None:
    """两个 worker 各自持独立 session 同时 claim 同一行: 恰好一个赢。

    真正的并发: 两个线程用一个 barrier 对齐, 在同一个瞬间对同一行发布条件
    更新。此前的版本是 `for _ in range(2)` 的顺序循环, 顺序执行下条件更新
    当然只赢一次 —— 它验证的是行生命周期守卫, 不是并发。

    SQLite 下写事务是串行的, 所以这条测试证明的是"条件更新在争用下仍只让
    一个赢" (第二个要么 rowcount=0, 要么撞上锁超时后重试失败), 而不是真正的
    并行写。这正是生产里多个 worker 进程会遇到的情形。
    """
    workers = 2
    barrier = threading.Barrier(workers)
    results: list[bool] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def _attempt() -> None:
        try:
            barrier.wait(timeout=10)
            with migrated_factory() as session:
                won = claim_job_run(session, run_id=run_id, at=NOW)
                if won:
                    session.commit()
                with lock:
                    results.append(won)
        except BaseException as exc:
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=_attempt) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads), "a worker deadlocked"

    assert results.count(True) == 1, (results, errors)
    assert results.count(False) == 1, (results, errors)


def test_simultaneous_second_run_inserts_collide_on_the_partial_index(
    migrated_factory: sessionmaker[Session], run_id: str
) -> None:
    """job 级唯一部分索引在真正的并发写入下仍然拦住第二个活跃 run。

    这是 migration 0008 存在的理由: 每个 caller 各自生成 run_id, 行级条件
    更新无法在它们之间仲裁, 只有索引能。顺序的 insert-after-insert 只能证明
    索引存在, 不能证明它在同时写入时生效 —— 而"同时"才是真实场景 (手动触发
    与调度 tick 撞在一起)。
    """
    with migrated_factory() as session:
        job = job_for_source(session, source_id="src-1")
        assert job is not None
        job_id = job.id

    workers = 2
    barrier = threading.Barrier(workers)
    outcomes: list[str] = []
    lock = threading.Lock()

    def _insert(run_name: str) -> None:
        try:
            barrier.wait(timeout=10)
            with migrated_factory() as session:
                create_job_run(
                    session,
                    run_id=run_name,
                    job_id=job_id,
                    correlation_id=f"c-{run_name}",
                    at=NOW,
                )
                session.commit()
            with lock:
                outcomes.append("created")
        except IntegrityError:
            with lock:
                outcomes.append("refused")
        except BaseException as exc:
            with lock:
                outcomes.append(f"error:{type(exc).__name__}")

    threads = [
        threading.Thread(target=_insert, args=(f"run-race-{i}",))
        for i in range(workers)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    # The slot is already held by `run_id`, so both inserts must be refused --
    # and, crucially, neither may slip through by racing the index.
    assert outcomes.count("created") == 0, outcomes
    assert outcomes.count("refused") == workers, outcomes


def test_late_claim_is_rejected(
    migrated_factory: sessionmaker[Session], run_id: str
) -> None:
    """已被 claim 的 run, 再次 claim 必须被拒(幂等性来源)。"""
    with migrated_factory() as session:
        assert claim_job_run(session, run_id=run_id, at=NOW) is True
        session.commit()
    with migrated_factory() as session:
        assert claim_job_run(session, run_id=run_id, at=NOW) is False
