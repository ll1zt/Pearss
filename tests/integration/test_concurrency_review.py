"""Code review 实证: 并发 claim 是否真只有一个赢家。

`claim_job_run` 的注释断言「两个调度器同时 tick, 恰好一个看到 rowcount 1」。
这个断言此前从未被并发实测过 —— 本文件把它变成可执行事实。
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session, sessionmaker

from adapters.db_models import Source
from adapters.repository import claim_job_run, create_job_run, ensure_collect_job
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


def test_concurrent_claim_has_exactly_one_winner(
    migrated_factory: sessionmaker[Session], run_id: str
) -> None:
    """两个 worker 各自持独立 session 同时 claim: 恰好一个赢。"""
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
