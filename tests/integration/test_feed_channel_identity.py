"""The RSS channel is a subscription, and its metadata names the origin.

Scenario register (normative source: the AC-340 row of SCENARIOS.md)

  AC-340 the RSS channel is the subscription
    Given a subscription and its published items
    When  its channel is rendered
    Then  the channel is addressed by the subscription id while its name and
          link describe the origin behind it -- the same display label the
          Reader's sidebar uses
    And   the item set is that subscription's visible items, pending excluded

The 1:1 stage makes the subscription and the origin share an id, so what these
pin is the *resolution*: the window is scoped by the origin the subscription
consumes, and the gate applies before anything is rendered.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from adapters.repository import channel_for_subscription, items_ready_for_feed
from adapters.rss_out import render_feed
from app.pipeline import normalise_and_store
from core.models import ItemDraft
from core.normalize import origin_display_label
from core.tenancy import SINGLE_TENANT
from tests.source_seed import seed_source

pytestmark = pytest.mark.integration

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _store(
    session: Session, *, origin_id: str, external_id: str, pending: bool = False
) -> str:
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
    item_id = str(
        session.execute(
            sa.text("select id from item where origin_id = :o"), {"o": origin_id}
        ).scalar_one()
    )
    if pending:
        # Behind the gate. Set directly rather than through a chain: the gate
        # is a status, and this test is about the window's scoping, not about
        # how an item got there.
        session.execute(
            sa.text("update item set publish_status = 'pending' where id = :i"),
            {"i": item_id},
        )
    session.commit()
    return item_id


@pytest.mark.integration
def test_the_channel_names_the_subscriptions_origin(
    session_factory: sessionmaker[Session],
) -> None:
    """AC-340: the metadata is the origin's, and an unknown channel has none."""
    with session_factory() as session:
        seed_source(
            session,
            source_id="sub-a",
            url="https://a.example/feed",
            title="A Feed",
            folder="Tech",
        )
        seed_source(session, source_id="sub-b", url="https://b.example/feed")
        session.commit()

    with session_factory() as session:
        meta = channel_for_subscription(
            session, subscription_id="sub-a", tenant_id=SINGLE_TENANT
        )
        assert meta is not None
        # The name comes from the origin, by the same label rule the Reader
        # sidebar uses -- so the two outputs cannot disagree.
        assert meta.title == origin_display_label(
            title="A Feed",
            url="https://a.example/feed",
            canonical_url="https://a.example/feed",
            origin_id="sub-a",
        )
        assert meta.link == "https://a.example/feed"

        assert (
            channel_for_subscription(
                session, subscription_id="no-such-channel", tenant_id=SINGLE_TENANT
            )
            is None
        )


@pytest.mark.integration
def test_the_window_is_scoped_to_the_channel_and_the_gate_still_holds(
    session_factory: sessionmaker[Session],
) -> None:
    """AC-340: one channel's items only, and pending rows never appear."""
    with session_factory() as session:
        seed_source(session, source_id="sub-a", url="https://a.example/feed")
        seed_source(session, source_id="sub-b", url="https://b.example/feed")
        seed_source(session, source_id="sub-pending", url="https://p.example/feed")
        session.flush()
        a_item = _store(session, origin_id="sub-a", external_id="a-1")
        b_item = _store(session, origin_id="sub-b", external_id="b-1")
        _store(session, origin_id="sub-pending", external_id="p-1", pending=True)

    with session_factory() as session:
        scoped = items_ready_for_feed(session, origin_id="sub-a")
        assert [item.id for item in scoped] == [a_item]
        assert b_item not in {item.id for item in scoped}

        everything = items_ready_for_feed(session)
        assert {item.id for item in everything} == {a_item, b_item}, (
            "the unscoped window is every origin's visible items; the pending "
            "channel's item must not be among them"
        )

        meta = channel_for_subscription(
            session, subscription_id="sub-a", tenant_id=SINGLE_TENANT
        )
        assert meta is not None
        document = render_feed(meta, scoped)
        assert meta.title.encode() in document
        assert a_item.encode() not in document, (
            "the item id is not rendered; the guid is"
        )
        assert document.count(b"<item>") == 1
