"""Unpublished revisions must earn visibility for their own content version.

Scenario register (normative source: ADR 0035 §9; issue #82).
"""

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from adapters.db_models import Item as ItemRow
from adapters.db_models import ItemEnrichment
from adapters.repository import (
    create_source,
    enqueue_chain,
    items_ready_for_feed,
    mark_published,
    store_item,
)
from app.pipeline import normalise_and_store
from core.enrichment import EnrichmentKind, EnrichmentSpec, ModelIdentity
from core.models import ItemDraft
from core.normalize import normalize


def test_user_publication_survives_a_collectors_stale_snapshot(
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Given a collector's old view, publishing first makes its later edit harmless."""
    monkeypatch.setenv("PEARSS_STORAGE_DIR", str(tmp_path / "storage"))
    with session_factory() as seed:
        source = create_source(
            seed,
            source_type="rss",
            url="https://example.test/race",
            canonical_url="https://example.test/race",
            schedule_policy="{}",
            tenant_id="default",
            at="2026-01-01T00:00:00.000000Z",
        )
        draft = ItemDraft(
            source_id=source.id,
            external_id="race",
            title="Original",
            canonical_url="https://example.test/article",
            published_at=None,
            content="<p>Original body</p>",
        )
        item_id = normalise_and_store(seed, [draft])[0].result.item.id
    with session_factory() as collector:
        stale = collector.scalars(select(ItemRow).where(ItemRow.id == item_id)).one()
        assert stale.publish_status == "ready"
        collector.commit()
        with session_factory() as publisher:
            mark_published(
                publisher,
                items_ready_for_feed(publisher),
                at=datetime(2026, 1, 2, tzinfo=UTC),
            )
            publisher.commit()
            frozen = publisher.execute(select(ItemRow.__table__)).one()
        candidate = normalize(
            replace(draft, content="<p>New body</p>"), tenant_id="default"
        ).item
        outcome = store_item(collector, candidate)
        collector.commit()
        collector.expire_all()
        assert collector.execute(select(ItemRow.__table__)).one() == frozen
        assert outcome.action.value == "skip_published_content_changed"


pytestmark = pytest.mark.integration


@pytest.mark.parametrize("published", [False, True])
def test_user_only_sees_processed_or_already_frozen_content(
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    published: bool,
) -> None:
    """Given a visible item, a revision waits for processing unless already frozen."""
    monkeypatch.setenv("PEARSS_STORAGE_DIR", str(tmp_path / "storage"))
    with session_factory() as session:
        source = create_source(
            session,
            source_type="rss",
            url="https://example.test/feed",
            canonical_url="https://example.test/feed",
            schedule_policy="{}",
            tenant_id="default",
            at="2026-01-01T00:00:00.000000Z",
        )
        draft = ItemDraft(
            source_id=source.id,
            external_id="revision",
            title="Article",
            canonical_url="https://example.test/article",
            published_at=None,
            content="<p>Original article.</p>",
        )
        first = normalise_and_store(session, [draft])[0]
        visible = items_ready_for_feed(session)
        assert [item.id for item in visible] == [first.result.item.id]
        if published:
            mark_published(session, visible, at=datetime(2026, 1, 2, tzinfo=UTC))
            session.commit()
        original = session.execute(select(ItemRow.__table__)).one()
        changed = replace(draft, content="<p>Revised article requiring summary.</p>")
        revision = normalise_and_store(
            session,
            [changed],
            chain=(EnrichmentSpec(plugin_id="summary", kind=EnrichmentKind.SUMMARY),),
        )[0]
        session.expire_all()
        stored = session.execute(select(ItemRow.__table__)).one()
        products = session.execute(select(ItemEnrichment)).scalars().all()
        if published:
            assert stored == original
            assert products == []
            assert [item.id for item in items_ready_for_feed(session)] == [original.id]
        else:
            assert stored.id == original.id
            assert stored.guid == original.guid
            assert stored.content_hash == revision.result.item.content_hash
            assert stored.publish_status == "pending"
            assert items_ready_for_feed(session) == []
            assert [(row.input_hash, row.status) for row in products] == [
                (revision.result.item.content_hash, "queued")
            ]


def test_switching_the_embedding_model_does_not_reuse_the_old_vector(
    session_factory: sessionmaker[Session],
) -> None:
    """AC-101: a different model must produce a new product, not reuse M1's row.

    The identity was (item, plugin, version, kind, lang, input_hash) with
    `input_hash` = the item's content hash alone, and the `model` column was never
    written. So a switch from M1 to M2 found M1's row, called the product valid
    and never recomputed -- and nothing errored. For a vector that is worse than
    stale: a 384-wide row consumed as 768-wide, or a vector from a different
    semantic space ranked as if it belonged, with bad ranking as the only symptom.
    """
    with session_factory() as session:
        source = create_source(
            session,
            source_type="rss",
            url="https://example.test/vectors",
            canonical_url="https://example.test/vectors",
            schedule_policy="{}",
            tenant_id="default",
            at="2026-01-01T00:00:00.000000Z",
        )
        item = normalise_and_store(
            session,
            [
                ItemDraft(
                    source_id=source.id,
                    external_id="vectors",
                    title="Vectors",
                    canonical_url="https://example.test/vectors/1",
                    published_at=None,
                    content="<p>An article that needs a vector</p>",
                )
            ],
        )[0].result.item

        def spec(model: ModelIdentity) -> EnrichmentSpec:
            return EnrichmentSpec(
                plugin_id="embed", kind=EnrichmentKind.EMBEDDING, model=model
            )

        m1 = ModelIdentity(name="multilingual-e5-small", dimensions=384, revision="v1")
        queued_m1 = enqueue_chain(
            session,
            item_id=item.id,
            content_hash=item.content_hash,
            chain=(spec(m1),),
            tenant_id="default",
        )
        session.commit()
        assert queued_m1 == 1

        # Same item, same content, same plugin: only the model changed.
        m2 = ModelIdentity(name="multilingual-e5-small", dimensions=768, revision="v1")
        queued_m2 = enqueue_chain(
            session,
            item_id=item.id,
            content_hash=item.content_hash,
            chain=(spec(m2),),
            tenant_id="default",
        )
        session.commit()
        rows = session.scalars(
            select(ItemEnrichment).where(ItemEnrichment.item_id == item.id)
        ).all()

    assert queued_m2 == 1, (
        "a new model must queue a new product; reusing M1's row is the silent "
        f"path AC-95 closes (rows: {[(r.model, r.input_hash[:12]) for r in rows]})"
    )
    assert len(rows) == 2
    assert {row.model for row in rows} == {str(m1), str(m2)}
    assert len({row.input_hash for row in rows}) == 2
