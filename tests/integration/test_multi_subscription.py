"""One origin, several subscriptions (ADR 0061).

Scenario register (normative source: the AC-345..347 row of SCENARIOS.md)

  AC-345 an origin can carry two policies
    Given an origin with a subscription
    When  a second subscription is added with a different folder and chain
    Then  both are effective, the origin's facts are shared (one origin row),
          and each channel's items are its own subscription's

  AC-346 dedupe is by origin, not by subscription
    Given one article collected for an origin with two subscriptions
    When  it is stored
    Then  there is one item, carrying the origin -- not one per subscription

  AC-347 visibility is (item, subscription)
    Given a subscription with a translation chain and one without
    When  the item is stored
    Then  the chainless channel shows it and the chained one does not
    And   completing the chain opens the second channel, not the first again
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from adapters import reader_api
from adapters.db_models import Item as ItemRow
from adapters.db_models import ItemSubscriptionReady as ReadyRow
from adapters.repository import (
    create_subscription_for_origin,
    mark_subscription_ready,
    subscriptions_for_origin,
    visible_ids_for_stream,
)
from adapters.repository.enrichment import chain_for_source, is_source_chain_complete
from adapters.repository.feed import items_ready_for_feed
from app.pipeline import normalise_and_store
from core.models import ItemDraft
from core.tenancy import SINGLE_TENANT
from tests.source_seed import seed_source

pytestmark = pytest.mark.integration

BASE = datetime(2026, 1, 1, tzinfo=UTC)
CHAIN = '[{"plugin_id": "translation", "kind": "translation", "lang": "en"}]'


def _store(session: Session, *, origin_id: str, external_id: str) -> str:
    normalise_and_store(
        session,
        [
            ItemDraft(
                origin_id=origin_id,
                external_id=external_id,
                title=f"An item {external_id}",
                canonical_url=f"https://example.test/{external_id}",
                published_at=BASE,
                content="<p>body</p>",
            )
        ],
    )
    session.commit()
    return str(
        session.execute(
            sa.text("select id from item where origin_id = :o"), {"o": origin_id}
        ).scalar_one()
    )


def _second_subscription(
    session: Session, *, origin_id: str, folder: str, pipeline_chain: str | None
) -> str:
    return create_subscription_for_origin(
        session,
        origin_id=origin_id,
        tenant_id=SINGLE_TENANT,
        schedule_policy='{"interval_minutes": 30}',
        at="2026-02-01T00:00:00.000000Z",
        folder=folder,
        pipeline_chain=pipeline_chain,
    ).id


@pytest.mark.integration
def test_an_origin_carries_two_subscriptions_with_their_own_policies(
    session_factory: sessionmaker[Session],
) -> None:
    """AC-345: two policies over one origin, and the facts stay one row."""
    with session_factory() as session:
        seed_source(session, source_id="sub-a", url="https://a.example/feed")
        second = _second_subscription(
            session, origin_id="sub-a", folder="Translations", pipeline_chain=CHAIN
        )
        session.commit()

    assert second != "sub-a", "the second subscription needs its own id"

    with session_factory() as session:
        subscriptions = subscriptions_for_origin(
            session, origin_id="sub-a", tenant_id=SINGLE_TENANT
        )
        assert [row.id for row in subscriptions] == ["sub-a", second]
        # The facts are shared: one origin, whatever the subscription count.
        assert session.execute(sa.text("select count(*) from origin")).scalar_one() == 1
        by_id = {row.id: row for row in subscriptions}
        assert by_id["sub-a"].folder != by_id[second].folder
        assert by_id["sub-a"].pipeline_chain is None
        assert by_id[second].pipeline_chain == CHAIN


@pytest.mark.integration
def test_dedupe_is_by_origin_not_by_subscription(
    session_factory: sessionmaker[Session],
) -> None:
    """AC-346: one article, one item, whichever subscriptions share the origin."""
    with session_factory() as session:
        seed_source(session, source_id="sub-a", url="https://a.example/feed")
        _second_subscription(
            session, origin_id="sub-a", folder="Translations", pipeline_chain=None
        )
        session.commit()
        item_id = _store(session, origin_id="sub-a", external_id="one")

    with session_factory() as session:
        assert session.execute(sa.text("select count(*) from item")).scalar_one() == 1
        assert (
            session.execute(sa.text("select origin_id from item")).scalar_one()
            == "sub-a"
        )
        # Both channels are about the same item: `origin_id` is the origin, so
        # a second subscription does not duplicate content.
        assert (
            session.execute(
                sa.text("select count(*) from item where origin_id = :o"),
                {"o": "sub-a"},
            ).scalar_one()
            == 1
        )
        assert item_id


@pytest.mark.integration
def test_visibility_is_per_subscription(
    session_factory: sessionmaker[Session],
) -> None:
    """AC-347: the chainless channel opens first, and the chained one follows."""
    with session_factory() as session:
        seed_source(session, source_id="sub-plain", url="https://a.example/feed")
        chained = _second_subscription(
            session, origin_id="sub-plain", folder="Translations", pipeline_chain=CHAIN
        )
        session.commit()
        item_id = _store(session, origin_id="sub-plain", external_id="one")

    with session_factory() as session:
        plain_stream = visible_ids_for_stream(
            session,
            stream_id=reader_api.feed_stream_id("sub-plain"),
            tenant_id=SINGLE_TENANT,
        )
        chained_stream = visible_ids_for_stream(
            session,
            stream_id=reader_api.feed_stream_id(chained),
            tenant_id=SINGLE_TENANT,
        )
        assert plain_stream == [item_id], (
            "the chainless subscription must see the item immediately"
        )
        assert chained_stream == [], (
            "the chained subscription must not see it before its chain completes"
        )
        assert items_ready_for_feed(session, subscription_id="sub-plain")
        assert items_ready_for_feed(session, subscription_id=chained) == []

        # Completing the chain opens the second channel, and only then.
        mark_subscription_ready(
            session,
            item_id=item_id,
            subscription_id=chained,
            at="2026-02-01T00:00:00.000000Z",
        )
        session.commit()
        assert visible_ids_for_stream(
            session,
            stream_id=reader_api.feed_stream_id(chained),
            tenant_id=SINGLE_TENANT,
        ) == [item_id]
        # The predicate is the pair: the chainless channel still sees it, and
        # a channel nothing is ready for sees nothing.
        assert (
            visible_ids_for_stream(
                session,
                stream_id=reader_api.feed_stream_id("no-such-subscription"),
                tenant_id=SINGLE_TENANT,
            )
            == []
        )


def _materialised(session: Session) -> set[tuple[str, str]]:
    """The gate as it is stored."""
    return {
        (str(item_id), str(subscription_id))
        for item_id, subscription_id in session.execute(
            sa.select(ReadyRow.item_id, ReadyRow.subscription_id)
        ).all()
    }


def _derived(session: Session) -> set[tuple[str, str]]:
    """The gate as the enrichments imply it."""
    derived: set[tuple[str, str]] = set()
    for item in session.execute(sa.select(ItemRow)).scalars():
        for subscription in subscriptions_for_origin(
            session, origin_id=item.origin_id, tenant_id=item.tenant_id
        ):
            if is_source_chain_complete(
                session,
                item_id=item.id,
                content_hash=item.content_hash,
                chain=chain_for_source(session, source_id=subscription.id),
            ):
                derived.add((item.id, subscription.id))
    return derived


@pytest.mark.integration
def test_the_materialised_gate_agrees_with_what_the_enrichments_imply(
    session_factory: sessionmaker[Session],
) -> None:
    """ADR 0061's consistency guard, injected to prove it can fail.

    The materialised answer's failure mode is a forgotten write: the item is
    ready in every sense except the row, so it is invisible and nothing says so.
    This derives the ready set from the enrichments and requires the table to
    agree -- then removes a row and requires the two to disagree, because a
    guard that cannot fail is a comment.
    """
    with session_factory() as session:
        seed_source(session, source_id="sub-plain", url="https://a.example/feed")
        chained = _second_subscription(
            session, origin_id="sub-plain", folder="Translations", pipeline_chain=CHAIN
        )
        session.commit()
        _store(session, origin_id="sub-plain", external_id="one")

    with session_factory() as session:
        assert _materialised(session) == _derived(session), (
            "the stored gate and the enrichments disagree"
        )
        # The chainless channel is open; the chained one is not.
        assert {subscription for _item, subscription in _materialised(session)} == {
            "sub-plain"
        }

        # Injection: forget one write and the guard has to notice.
        session.execute(
            sa.text("delete from item_subscription_ready where subscription_id = :s"),
            {"s": "sub-plain"},
        )
        session.commit()
        assert _materialised(session) != _derived(session), (
            "a missing ready row was not noticed"
        )
        assert chained
