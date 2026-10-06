"""origin/subscription split stage A: the dual-write façade (issue #363).

Scenario register (normative source: the AC-332..333 row of SCENARIOS.md)

  AC-333 one create writes the same row into all three tables
    Given a migrated database
    When  a source is created through create_source
    Then  source/origin/subscription each hold exactly one row with the same id,
          and the fact columns and the policy columns have landed where they belong
    And   calling it again with the same URL writes nothing the second time

Stage A adds tables only: the read paths still use `source` (stage B switches
them), so what this asserts is the *write* side. Nothing here counts calls --
it reads the three tables' real rows and compares them column for column. The
repository read functions added with the tables are driven by real data here,
so they cannot become helpers with tests and no production caller.

The statements are literals rather than interpolated strings: the table and
column names are fixed, and writing them out is what keeps a query in a test
readable as the query it is.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from adapters.repository import create_source, timestamp
from adapters.repository.origins import get_origin, list_origins
from adapters.repository.scheduling import collectable_sources
from adapters.repository.subscriptions import get_subscription, list_subscriptions
from app.pipeline import normalise_and_store
from core.enums import SourceStatus, TrustLevel
from core.models import ItemDraft
from core.tenancy import SINGLE_TENANT

pytestmark = pytest.mark.integration

BASE = datetime(2026, 1, 1, tzinfo=UTC)
AT = timestamp(BASE)
SCHEDULE = '{"interval_minutes": 60}'
CHAIN = '[{"plugin_id": "translate", "kind": "translate", "lang": "zh"}]'

SOURCE_FACTS = (
    "select id, tenant_id, type, url, canonical_url, title, parser_plugin_id,"
    " seen_frontier, floor_cursor, created_at, updated_at"
    " from source where id = :id"
)
ORIGIN_FACTS = (
    "select id, tenant_id, type, url, canonical_url, title, parser_plugin_id,"
    " seen_frontier, floor_cursor, created_at, updated_at"
    " from origin where id = :id"
)
SUBSCRIPTION_POLICY = (
    "select id, tenant_id, origin_id, schedule_policy, pipeline_chain, target_lang,"
    " folder, status, trust_level, created_at, updated_at"
    " from subscription where id = :id"
)
COUNTS = {
    "source": "select count(*) from source",
    "origin": "select count(*) from origin",
    "subscription": "select count(*) from subscription",
}


def _create(
    session: Session,
    *,
    url: str = "https://a.example/feed",
    folder: str = "Tech",
) -> str:
    row = create_source(
        session,
        source_type="rss",
        url=url,
        canonical_url=url,
        schedule_policy=SCHEDULE,
        tenant_id=SINGLE_TENANT,
        parser_plugin_id="rss",
        pipeline_chain=CHAIN,
        target_lang="zh",
        folder=folder,
        trust_level=TrustLevel.TRUSTED.value,
        at=AT,
    )
    return row.id


def _count(session: Session, table: str) -> int:
    return int(session.execute(sa.text(COUNTS[table])).scalar_one())


@pytest.mark.integration
def test_creating_a_source_writes_the_same_row_into_all_three_tables(
    session_factory: sessionmaker[Session],
) -> None:
    """AC-333: one create writes three rows, splitting facts from policy."""
    with session_factory() as session:
        source_id = _create(session)
        session.commit()

    with session_factory() as session:
        fact = session.execute(sa.text(SOURCE_FACTS), {"id": source_id}).one()
        origin = session.execute(sa.text(ORIGIN_FACTS), {"id": source_id}).one()
        policy = session.execute(sa.text(SUBSCRIPTION_POLICY), {"id": source_id}).one()

        # Column for column, not "roughly the same".
        assert tuple(origin) == tuple(fact)
        # The subscription shares the id (the 1:1 stage) and points at its own
        # origin rather than merely repeating the id.
        assert policy[0] == source_id
        assert policy[1] == SINGLE_TENANT
        assert policy[2] == source_id
        # Every policy column landed on the subscription, and none was dropped.
        assert policy[3:] == (
            SCHEDULE,
            CHAIN,
            "zh",
            "Tech",
            SourceStatus.ACTIVE.value,
            TrustLevel.TRUSTED.value,
            AT,
            AT,
        )

        assert _count(session, "source") == 1
        assert _count(session, "origin") == 1
        assert _count(session, "subscription") == 1


@pytest.mark.integration
def test_the_write_path_stores_the_origin_and_mirrors_the_legacy_column(
    session_factory: sessionmaker[Session],
) -> None:
    """AC-334: the item's fact is `origin_id`, and reads follow the subscription.

    Three statements, each one a way this could be wrong: the collected item
    must carry the origin (not only the column 0028 drops), the mirror must
    still be populated so a rollback to the previous release keeps working, and
    the scheduler must read policy from the subscription rather than from the
    frozen mirror row.
    """
    with session_factory() as session:
        source_id = _create(session)
        normalise_and_store(
            session,
            [
                ItemDraft(
                    origin_id=source_id,
                    external_id="entry-1",
                    title="An item",
                    canonical_url="https://a.example/1",
                    published_at=BASE,
                    content="<p>body</p>",
                )
            ],
        )
        just_created = collectable_sources(session, tenant_id=SINGLE_TENANT)
        session.commit()

    assert just_created == [(source_id, SCHEDULE)]

    with session_factory() as session:
        row = session.execute(sa.text("select origin_id, source_id from item")).one()
        assert row.origin_id == source_id
        assert row.source_id == source_id, "the mirror column must stay populated"

        # Change only the subscription, then ask the scheduler again: the policy
        # it reports has to follow the subscription, or it is reading the mirror.
        session.execute(
            sa.text("update subscription set schedule_policy = :policy where id = :id"),
            {"policy": '{"interval_minutes": 5}', "id": source_id},
        )
        session.commit()
        policy_after = collectable_sources(session, tenant_id=SINGLE_TENANT)
        mirror = session.execute(
            sa.text("select schedule_policy from source where id = :id"),
            {"id": source_id},
        ).scalar_one()

    assert policy_after == [(source_id, '{"interval_minutes": 5}')]
    assert mirror == SCHEDULE, (
        "the mirror is frozen at creation on purpose: nothing reads it, and a "
        "second writer would be a second owner of the same fact"
    )


@pytest.mark.integration
def test_creating_the_same_url_twice_writes_nothing_the_second_time(
    session_factory: sessionmaker[Session],
) -> None:
    """AC-333: a repeated create is idempotent in all three tables, not just one."""
    with session_factory() as session:
        first = _create(session)
        session.commit()

    with session_factory() as session:
        second = _create(session)
        session.commit()

    assert second == first
    with session_factory() as session:
        assert _count(session, "source") == 1
        assert _count(session, "origin") == 1
        assert _count(session, "subscription") == 1


@pytest.mark.integration
def test_the_new_read_paths_answer_for_what_the_create_path_wrote(
    session_factory: sessionmaker[Session],
) -> None:
    """Stage A's read functions return the rows its write path produced."""
    with session_factory() as session:
        first = _create(session, url="https://a.example/feed", folder="Tech")
        second = _create(session, url="https://b.example/feed", folder="News")
        session.commit()

    with session_factory() as session:
        origin = get_origin(session, first)
        assert origin is not None
        assert origin.id == first
        assert origin.canonical_url == "https://a.example/feed"
        assert origin.type == "rss"

        subscription = get_subscription(session, first)
        assert subscription is not None
        assert subscription.origin_id == first
        assert subscription.folder == "Tech"

        assert get_origin(session, "no-such-id") is None
        assert get_subscription(session, "no-such-id") is None

        origins = list_origins(session, tenant_id=SINGLE_TENANT, limit=10)
        assert [row.id for row in origins] == [first, second]
        subscriptions = list_subscriptions(session, tenant_id=SINGLE_TENANT, limit=10)
        assert [row.id for row in subscriptions] == [first, second]
        assert [row.folder for row in subscriptions] == ["Tech", "News"]
