"""The fulltext chain end to end: storage, queue, sweep, gate, artifact.

Scenario register: src/plugins/processors/fulltext/SCENARIOS.md (AC-7, AC-8).
This suite lives under tests/integration because it exercises the
app -> plugins boundary (pipeline, enrichment worker, publish gate); the
plugin directory keeps only the unit-level tests (extraction, fetch, error
taxonomy) so the import-linter's layering contract stays intact.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.integration


class FulltextProbe:
    """A processor hooking the SAME hooks as fulltext, serving a canned page.

    It exercises the worker/gate wiring with a deterministic product. The
    network fetch and the extractor are pinned by the plugin's unit tests;
    here the row lifecycle and the publish gate are what is under test.
    """

    @staticmethod
    def process(
        item_ctx: dict[str, str], *, target_lang: str, store: Callable[[str], str]
    ) -> str:
        # Derived from the item_ctx the worker passes through the child
        # boundary: the assertion pins that the boundary carried the item in.
        return store("PROBE:" + item_ctx.get("content", ""))


def _seed_fulltext_world(
    factory: sessionmaker[Session],
    *,
    chain: str,
    content: str,
) -> str:
    """A source with the fulltext chain and one stored item, via the pipeline."""
    from adapters.repository import create_source, parse_chain, timestamp
    from app.pipeline import normalise_and_store
    from core.models import ItemDraft

    with factory() as session:
        source = create_source(
            session,
            source_type="rss",
            url="https://fulltext.example/feed",
            canonical_url="https://fulltext.example/feed",
            schedule_policy="{}",
            tenant_id="default",
            pipeline_chain=chain,
            at=timestamp(datetime.now(UTC)),
        )
        normalise_and_store(
            session,
            [
                ItemDraft(
                    origin_id=source.id,
                    external_id="post-1",
                    title="A summary post",
                    canonical_url="https://fulltext.example/post-1",
                    published_at=None,
                    content=content,
                )
            ],
            chain=parse_chain(chain, target_lang=None),
        )
        session.commit()
        item_id = session.scalar(
            sa.text("select id from item where source_id = :sid"), {"sid": source.id}
        )
    assert item_id is not None
    return str(item_id)


def test_user_chain_gated_item_publishes_after_fulltext_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-8: collection -> queued row -> sweep -> gate opens, artifact stored,
    item body untouched (products never rewrite items)."""
    from adapters import filestore
    from adapters.db_models import Item
    from app.enrichment_worker import sweep_queue
    from tests.migrated_schema import migrated_database

    monkeypatch.setenv("PEARSS_STORAGE_DIR", str(tmp_path / "storage"))
    # The sandboxed child resolves plugin modules through PYTHONPATH; without
    # pinning it to THIS checkout the shared venv's .pth wins and the child
    # imports the main checkout's (fulltext-less) tree.
    repo_src = Path(__file__).resolve().parents[2] / "src"
    monkeypatch.setenv("PYTHONPATH", str(repo_src))
    engine = sa.create_engine(migrated_database(tmp_path, "fulltext-e2e.db"))
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        chain = '[{"plugin_id":"fulltext","kind":"fulltext","required":true}]'
        summary = "<p>Short summary of the post.</p>"
        item_id = _seed_fulltext_world(factory, chain=chain, content=summary)

        # Stored behind the gate: pending, one queued fulltext row.
        with factory() as session:
            row = session.execute(
                sa.text(
                    "select status, plugin_id, kind from item_enrichment"
                    " where item_id = :i"
                ),
                {"i": item_id},
            ).first()
            assert row is not None
            assert (row[0], row[1], row[2]) == ("queued", "fulltext", "fulltext")
            assert (
                session.scalar(
                    sa.text("select publish_status from item where id = :i"),
                    {"i": item_id},
                )
                == "pending"
            )

        with factory() as session:
            report = sweep_queue(
                session,
                processors={"fulltext": FulltextProbe},
                row_session_factory=factory,
            )
            assert report["succeeded"] == 1

        probe_product = "PROBE:" + summary
        with factory() as session:
            gate = session.scalar(
                sa.text("select publish_status from item where id = :i"),
                {"i": item_id},
            )
            assert gate == "ready", "the gate must open when the chain completes"
            payload_ref = session.scalar(
                sa.text("select payload_ref from item_enrichment where item_id = :i"),
                {"i": item_id},
            )
            assert filestore.read_body(payload_ref) == probe_product
            stored_item = session.get(Item, item_id)
            assert stored_item is not None
            assert filestore.read_body(stored_item.content_ref) == summary
    finally:
        engine.dispose()


def test_user_existing_items_get_fulltext_queued_by_backfill(tmp_path: Path) -> None:
    """AC-7: a source that joins the chain late gets its stored items queued.

    The register promises this for fulltext, and it is the only way an item
    collected *before* the chain existed ever gets a body — the gate only
    sees items stored after the chain was configured. Verified as
    already-correct behavior rather than a repair: the commit that lands this
    test does not change `backfill_chain`. It is pinned because the plugin's
    own promise was resting on a translate-only test, so a regression that
    dropped fulltext's spec kind would have passed the whole suite while
    every pre-chain item silently stayed a summary.

    Idempotency is part of the promise ("re-running costs nothing"), so the
    second call is asserted to queue zero rather than assumed to.
    """
    from adapters.db_models import Item as ItemRow
    from adapters.db_models import Source as SourceRow
    from adapters.repository import backfill_chain
    from tests.migrated_schema import migrated_database

    engine = sa.create_engine(migrated_database(tmp_path, "fulltext-backfill.db"))
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        with factory() as session:
            session.add(
                SourceRow(
                    id="src-late",
                    tenant_id="default",
                    type="rss",
                    url="https://late.example/feed",
                    canonical_url="https://late.example/feed",
                    status="active",
                    trust_level="standard",
                    schedule_policy="{}",
                    pipeline_chain='[{"plugin_id":"fulltext","kind":"fulltext"}]',
                    created_at="2026-01-01T00:00:00.000000Z",
                    updated_at="2026-01-01T00:00:00.000000Z",
                )
            )
            # Stored and published before the chain existed.
            session.add(
                ItemRow(
                    id="late-item",
                    source_id="src-late",
                    guid="g-late",
                    content_hash="hash-late",
                    title="Published as a summary",
                    url="https://late.example/1",
                    publish_status="published",
                    tenant_id="default",
                    created_at="2026-01-01T00:00:00.000000Z",
                    updated_at="2026-01-01T00:00:00.000000Z",
                )
            )
            session.commit()

            assert backfill_chain(session, source_id="src-late") == 1
            session.commit()

            row = session.execute(
                sa.text(
                    "select status, plugin_id, kind, input_hash from item_enrichment"
                    " where item_id = :i"
                ),
                {"i": "late-item"},
            ).one()
            # The fulltext product, keyed to the stored revision so a content
            # change later is a new row rather than a silent overwrite.
            assert (row[0], row[1], row[2], row[3]) == (
                "queued",
                "fulltext",
                "fulltext",
                "hash-late",
            )
            # Already-published stays published: backfill adds products, it
            # does not reopen the gate on something clients already cached.
            assert (
                session.scalar(
                    sa.text("select publish_status from item where id = :i"),
                    {"i": "late-item"},
                )
                == "published"
            )

            # Idempotent: the same items are not queued twice.
            assert backfill_chain(session, source_id="src-late") == 0
    finally:
        engine.dispose()
