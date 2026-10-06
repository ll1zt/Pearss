"""The Reader channel is a subscription, not the origin behind it.

Scenario register (normative source: the AC-338..339 row of SCENARIOS.md)

  AC-338 a channel is a subscription
    Given an origin, its subscription and an item under it
    When  a client asks for stream feed/<id>
    Then  the id is the subscription id and the stream is that subscription's
          origin's items
    And   an id no subscription carries is an empty channel, not the library

  AC-339 the folder belongs to the subscription
    Given two subscriptions in different folders, each with an item
    When  a client asks for user/-/label/<folder>
    Then  only that folder's subscription's items come back

In the 1:1 stage the subscription and the origin share an id, so none of this
changes what a client sees -- which is why the tests assert the resolution
rather than a difference. The two-subscriptions form of AC-338 is issue #371's.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from adapters import reader_api
from adapters.repository import mark_all_read, visible_ids_for_stream
from app.pipeline import normalise_and_store
from core.models import ItemDraft
from core.tenancy import SINGLE_TENANT
from tests.source_seed import seed_source

pytestmark = pytest.mark.integration

BASE = datetime(2026, 1, 1, tzinfo=UTC)


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


@pytest.mark.integration
def test_a_channel_id_resolves_through_the_subscription(
    session_factory: sessionmaker[Session],
) -> None:
    """AC-338: feed/<id> is a subscription id, and an unknown one is empty."""
    with session_factory() as session:
        seed_source(session, source_id="sub-1", url="https://a.example/feed")
        item_id = _store(session, origin_id="sub-1", external_id="one")

    with session_factory() as session:
        stream = reader_api.feed_stream_id("sub-1")
        assert visible_ids_for_stream(
            session, stream_id=stream, tenant_id=SINGLE_TENANT
        ) == [item_id]

        # An id no subscription carries is an empty channel. Falling through to
        # the library is the wrong answer that looks like data.
        assert (
            visible_ids_for_stream(
                session,
                stream_id=reader_api.feed_stream_id("no-such-subscription"),
                tenant_id=SINGLE_TENANT,
            )
            == []
        )

        # And marking an unknown channel read changes nothing, rather than
        # sweeping every item the way an unresolved scope would.
        assert (
            mark_all_read(
                session,
                tenant_id=SINGLE_TENANT,
                before=datetime(2030, 1, 1, tzinfo=UTC),
                subscription_id="no-such-subscription",
            )
            == 0
        )
        assert (
            mark_all_read(
                session,
                tenant_id=SINGLE_TENANT,
                before=datetime(2030, 1, 1, tzinfo=UTC),
                subscription_id="sub-1",
            )
            == 1
        )


@pytest.mark.integration
def test_the_folder_stream_follows_the_subscription_folder(
    session_factory: sessionmaker[Session],
) -> None:
    """AC-339: a label stream is the items of the subscriptions in that folder."""
    with session_factory() as session:
        seed_source(
            session, source_id="sub-tech", url="https://t.example/feed", folder="Tech"
        )
        seed_source(
            session, source_id="sub-news", url="https://n.example/feed", folder="News"
        )
        tech_item = _store(session, origin_id="sub-tech", external_id="tech-1")
        news_item = _store(session, origin_id="sub-news", external_id="news-1")

    with session_factory() as session:
        tech = visible_ids_for_stream(
            session, stream_id="user/-/label/Tech", tenant_id=SINGLE_TENANT
        )
        assert tech == [tech_item], "the folder stream served another folder's item"
        assert news_item not in tech

        empty = visible_ids_for_stream(
            session, stream_id="user/-/label/NoSuchFolder", tenant_id=SINGLE_TENANT
        )
        assert empty == [], "a folder no subscription carries is an empty stream"
