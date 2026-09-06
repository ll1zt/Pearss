"""并发互斥的实证, 分两层各自验证。

- **job 级**: 同一 job 同时至多一个活跃 run。由 job_run 上的唯一部分索引在
  schema 层强制 —— 每个 caller 各自创建自己的 run_id, 行级条件更新无法在它们
  之间仲裁 (那是本文件此前实证过、随后被修正的错误假设)。
- **行级**: 单个 run 的 queued -> running 只能发生一次。由 claim 的条件更新
  保证, 是 run 生命周期自身的守卫。
"""

from __future__ import annotations

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
    """两个 worker 各自持独立 session 同时 claim 同一行: 恰好一个赢。"""
    wins = 0
    for _ in range(2):
        with migrated_factory() as session:
            if claim_job_run(session, run_id=run_id, at=NOW):
                wins += 1
                session.commit()
    assert wins == 1, f"concurrent claim winners={wins}, expected exactly 1"


def test_late_claim_is_rejected(
    migrated_factory: sessionmaker[Session], run_id: str
) -> None:
    """已被 claim 的 run, 再次 claim 必须被拒(幂等性来源)。"""
    with migrated_factory() as session:
        assert claim_job_run(session, run_id=run_id, at=NOW) is True
        session.commit()
    with migrated_factory() as session:
        assert claim_job_run(session, run_id=run_id, at=NOW) is False
