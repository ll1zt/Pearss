"""Storage recovery through the enrichment poll and real persistence path."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from adapters import filestore
from adapters.db_models import Item, ItemEnrichment
from adapters.repository import create_source, parse_chain
from app.enrichment_worker import EnrichmentWorker
from app.pipeline import normalise_and_store
from core.models import ItemDraft

pytestmark = pytest.mark.integration


class TitleSummary:
    """Local test plugin; exercises the processor contract without a provider."""

    @staticmethod
    def process(
        item_ctx: dict[str, str], *, target_lang: str, store: Callable[[str], str]
    ) -> str:
        return store(f"<p>{item_ctx['title']}</p>")


@pytest.mark.asyncio
async def test_user_pending_item_is_processed_after_storage_unlocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Given a locked queue, when storage recovers, the item becomes ready.

    A controlled poll and rollback-journal lock isolate queue enumeration from
    other lifespan resources. The processor, artifact store and DB are real;
    this is not a claim about production WAL read locking or worker readiness.
    """
    # Child resolution of TitleSummary must not initialize conftest's API app.
    from tests.migrated_schema import migrated_database

    monkeypatch.setenv("PEARSS_STORAGE_DIR", str(tmp_path / "storage"))
    engine = sa.create_engine(
        migrated_database(tmp_path, "enrichment-recovery.db"),
        connect_args={"timeout": 0},
    )
    factory = sessionmaker(engine, expire_on_commit=False)
    chain = '[{"plugin_id":"title-summary","kind":"summary","required":true}]'
    lock = sqlite3.connect(tmp_path / "enrichment-recovery.db", timeout=0)
    try:
        with factory() as session:
            source = create_source(
                session,
                source_type="rss",
                url="https://recovery.example/feed",
                canonical_url="https://recovery.example/feed",
                schedule_policy='{"type":"fixed","interval":"30m"}',
                tenant_id="default",
                pipeline_chain=chain,
                at="2026-01-01T00:00:00.000000Z",
            )
            normalise_and_store(
                session,
                [
                    ItemDraft(
                        source_id=source.id,
                        external_id="recovery-item",
                        title="Recovery article",
                        canonical_url="https://recovery.example/article",
                        published_at=None,
                        content="<p>Original article body.</p>",
                    )
                ],
                chain=parse_chain(chain, target_lang=None),
            )
            session.commit()
            assert session.scalar(sa.select(Item.publish_status)) == "pending"
            assert session.scalar(sa.select(ItemEnrichment.status)) == "queued"

        phases = iter(("lock", "unlock", "finish"))

        async def poll(seconds: float) -> None:
            assert seconds == 60
            phase = next(phases)
            if phase == "lock":
                lock.execute("BEGIN EXCLUSIVE")
            elif phase == "unlock":
                lock.rollback()
                with factory() as session:
                    assert session.scalar(sa.select(Item.publish_status)) == "pending"
                    assert session.scalar(sa.select(ItemEnrichment.status)) == "queued"
            else:
                raise asyncio.CancelledError

        monkeypatch.setattr(asyncio, "sleep", poll)
        worker = EnrichmentWorker(factory, processors={"title-summary": TitleSummary})
        with pytest.raises(asyncio.CancelledError):
            await worker._loop()
        with factory() as session:
            assert session.scalar(sa.select(Item.publish_status)) == "ready"
            assert session.scalar(sa.select(ItemEnrichment.status)) == "succeeded"
            assert (
                filestore.read_body(
                    session.scalar(sa.select(ItemEnrichment.payload_ref))
                )
                == "<p>Recovery article</p>"
            )
            assert (
                filestore.read_body(session.scalar(sa.select(Item.content_ref)))
                == "<p>Original article body.</p>"
            )
    finally:
        lock.rollback()
        lock.close()
        engine.dispose()
