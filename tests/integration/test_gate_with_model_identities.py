"""A chain whose product is keyed by a model opens the gate -- and only for it.

Scenario register (normative source: ADR 0035 §9; issue #82).

These pin the defect the retrieval tests found: the gate is handed the *item's*
content hash and derives each spec's identity from it. Handing it a row's own
`input_hash` derives a second time, matches nothing, and the item stays pending
forever -- invisible to every read, with nothing logged. The existing gate tests
could not see it because for a translation the derived identity *is* the content
hash, so the two arguments are the same string.
"""

from __future__ import annotations

import base64
import json
import struct
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from adapters.db_models import Item, ItemEnrichment
from adapters.filestore import store_body
from adapters.repository import create_source
from adapters.repository.enrichment import chain_for_source
from app.enrichment_worker import _open_gate_if_complete
from app.pipeline import normalise_and_store
from core.enrichment import (
    EnrichmentKind,
    EnrichmentSpec,
    ModelIdentity,
    input_identity,
)
from core.models import ItemDraft

pytestmark = pytest.mark.integration

FIXTURES = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "plugins"
    / "processors"
    / "embed"
    / "fixtures"
)
CHAIN = json.dumps([{"plugin_id": "embed", "kind": "embedding"}])


@pytest.fixture(autouse=True)
def _model_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PEARSS_EMBEDDING_MODEL_DIR", str(FIXTURES))


def _status(session: Session, item_id: str) -> str:
    """`publish_status`, with the lookup narrowed rather than asserted at each use."""
    item = session.get(Item, item_id)
    assert item is not None
    return item.publish_status


def _pending_item(session: Session) -> str:
    source = create_source(
        session,
        source_type="rss",
        url="https://example.test/gate",
        canonical_url="https://example.test/gate",
        schedule_policy="{}",
        pipeline_chain=CHAIN,
        tenant_id="default",
        at="2026-01-01T00:00:00.000000Z",
    )
    session.commit()
    item = normalise_and_store(
        session,
        [
            ItemDraft(
                source_id=source.id,
                external_id="gated",
                title="gated",
                canonical_url="https://example.test/gate/1",
                published_at=None,
                content="an item waiting on its vector",
            )
        ],
        chain=chain_for_source(session, source_id=source.id),
    )[0].result.item
    session.commit()
    assert _status(session, item.id) == "pending"
    return item.id


def _succeed_the_product(
    session: Session, item_id: str, *, model_revision: str
) -> None:
    """Write the success the worker would write, under a chosen model identity.

    The identity is `input_hash`, not the `model` column: the column records what
    produced the row, while the digest is what the row is *keyed* by. A test that
    changed only the column would be relabelling the jar.
    """
    item = session.get(Item, item_id)
    assert item is not None
    spec = EnrichmentSpec(
        plugin_id="embed",
        kind=EnrichmentKind.EMBEDDING,
        model=ModelIdentity(
            name="pearss-test-fixture", dimensions=384, revision=model_revision
        ),
    )
    session.execute(
        sa.update(ItemEnrichment)
        .where(ItemEnrichment.item_id == item_id)
        .values(
            status="succeeded",
            input_hash=input_identity(item.content_hash, spec),
            model=str(spec.model),
            payload_ref=store_body(
                base64.b64encode(struct.pack("<4f", 0.0, 0.0, 0.0, 1.0)).decode("ascii")
            ),
        )
    )
    session.commit()


def test_a_succeeded_vector_opens_the_gate(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        item_id = _pending_item(session)
        _succeed_the_product(session, item_id, model_revision="tiny-1")
        item = session.get(Item, item_id)
        assert item is not None

        opened = _open_gate_if_complete(
            session, item_id=item_id, content_hash=item.content_hash
        )
        session.commit()

    assert opened is True
    assert item.publish_status == "ready", item.publish_status


def test_a_product_from_another_model_does_not_open_the_gate(
    session_factory: sessionmaker[Session],
) -> None:
    """Why the identity is per spec: a vector from a different model is not a product.

    The row exists and is `succeeded`, so anything that keyed products by
    (item, plugin, kind) alone would open the gate on it -- and serve vectors
    from a model the item's key does not name (ADR 0035 decision 6).
    """
    with session_factory() as session:
        item_id = _pending_item(session)
        _succeed_the_product(session, item_id, model_revision="some-other-model")
        item = session.get(Item, item_id)
        assert item is not None

        opened = _open_gate_if_complete(
            session, item_id=item_id, content_hash=item.content_hash
        )
        session.commit()

    assert opened is False
    assert item.publish_status == "pending", item.publish_status


def test_a_row_identity_is_not_what_the_gate_wants(
    session_factory: sessionmaker[Session],
) -> None:
    """AC-119: the defect itself, pinned: a row's `input_hash` is a different argument.

    For an embedding the two strings differ, and passing the row's own identity
    derives a second time, matches nothing, and leaves the item pending with
    nothing logged. This test exists so the next caller that reaches for
    `row.input_hash` fails here rather than in production.
    """
    with session_factory() as session:
        item_id = _pending_item(session)
        _succeed_the_product(session, item_id, model_revision="tiny-1")
        item = session.get(Item, item_id)
        assert item is not None
        row = session.scalars(
            sa.select(ItemEnrichment).where(ItemEnrichment.item_id == item_id)
        ).one()
        assert row.input_hash != item.content_hash, (
            "the whole point: a vector's identity is a digest of the content hash"
        )

        opened = _open_gate_if_complete(
            session, item_id=item_id, content_hash=row.input_hash
        )

    assert opened is False


def test_a_revised_item_needs_a_vector_for_its_new_text(
    session_factory: sessionmaker[Session],
) -> None:
    """AC-102: the vector is pinned to the content version, like every product.

    A revised body is a different text, so the old vector describes something the
    item no longer says. It is not deleted -- products are history -- but it stops
    counting, and a new one is queued under a new identity.
    """
    with session_factory() as session:
        item_id = _pending_item(session)
        _succeed_the_product(session, item_id, model_revision="tiny-1")
        item = session.get(Item, item_id)
        assert item is not None
        _open_gate_if_complete(session, item_id=item_id, content_hash=item.content_hash)
        session.commit()
        assert _status(session, item_id) == "ready"

        source_id = item.source_id
        normalise_and_store(
            session,
            [
                ItemDraft(
                    source_id=source_id,
                    external_id="gated",
                    title="gated",
                    canonical_url="https://example.test/gate/1",
                    published_at=None,
                    content="the same item, revised",
                )
            ],
            chain=chain_for_source(session, source_id=source_id),
        )
        session.commit()
        rows = session.scalars(
            sa.select(ItemEnrichment).where(ItemEnrichment.item_id == item_id)
        ).all()
        status = _status(session, item_id)

    assert len(rows) == 2, "a revised text needs its own vector"
    assert len({row.input_hash for row in rows}) == 2
    assert status == "pending", "the revision has to earn the gate again"
