"""The single-tenant assumption, checked against the data (issue #89).

Scenario register (normative source: ADR 0038; issue #89).

  AC-110 数据库里出现第二个租户时会被说出来
    Given 一张已迁移的库
    When  写入一行 tenant_id 不是 SINGLE_TENANT 的条目
    Then  启动校验报出那个租户
    And   没有别的租户时不报任何东西

The assumption can only be wrong in the data, and the column exists on every
fact table -- so this is the one place the decision can be shown false. It is
asserted at the level an operator sees it: what the check reports, not whether
some function was called.
"""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from adapters.repository import create_source, timestamp
from app.main import create_engine_for
from app.pipeline import normalise_and_store
from app.tenancy_check import other_tenants
from core.models import ItemDraft
from core.tenancy import SINGLE_TENANT
from tests.migrated_schema import migrated_database

pytestmark = pytest.mark.integration

BASE = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def factory(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> sessionmaker[Session]:
    url = migrated_database(tmp_path, "tenancy.db")
    monkeypatch.setenv("PEARSS_DATABASE_URL", url)
    monkeypatch.setenv("PEARSS_STORAGE_DIR", str(tmp_path / "storage"))
    return sessionmaker(create_engine_for(url), expire_on_commit=False)


def _store(session: Session, *, tenant_id: str) -> str:
    source = create_source(
        session,
        source_type="rss",
        url=f"https://{tenant_id}.example/feed",
        canonical_url=f"https://{tenant_id}.example/feed",
        schedule_policy="{}",
        tenant_id=tenant_id,
        at=timestamp(BASE),
    )
    normalise_and_store(
        session,
        [
            ItemDraft(
                origin_id=source.id,
                external_id="one",
                title="An item",
                canonical_url="https://x.example/one",
                published_at=BASE,
                content="<p>body</p>",
            )
        ],
        tenant_id=tenant_id,
    )
    return source.id


def test_nothing_is_reported_when_only_one_tenant_is_present(
    factory: sessionmaker[Session],
) -> None:
    """The normal case must be silent, or the check becomes noise."""
    with factory() as session:
        _store(session, tenant_id=SINGLE_TENANT)
        session.commit()
    assert other_tenants(factory) == []


def test_a_second_tenant_in_the_data_is_reported(
    factory: sessionmaker[Session],
) -> None:
    """AC-110: the assumption is falsifiable, which is why it is worth naming.

    A row for another tenant is representable -- the column is on every table --
    and every read filters on the constant, so those rows would be invisible
    with nothing saying so. That silence is what the check removes.
    """
    with factory() as session:
        _store(session, tenant_id=SINGLE_TENANT)
        _store(session, tenant_id="someone-else")
        session.commit()

    assert other_tenants(factory) == ["someone-else"]

    # And the reads really do hide it: the assumption is load-bearing, not
    # decorative. This is the failure the report exists to make visible.
    with factory() as session:
        visible = session.execute(
            sa.text("select count(*) from item where tenant_id = :t"),
            {"t": SINGLE_TENANT},
        ).scalar_one()
        total = session.execute(sa.text("select count(*) from item")).scalar_one()
    assert visible == 1
    assert total == 2, "the other tenant's row is invisible to every read"
