"""Ownership races exercised through real migrated SQLite transactions."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from adapters import filestore
from adapters.db_models import Item, ItemEnrichment
from adapters.repository import create_source, parse_chain, timestamp
from app.enrichment_worker import sweep_queue
from app.pipeline import normalise_and_store
from core.models import ItemDraft
from tests.integration.test_enrichment_recovery import TitleSummary

pytestmark = pytest.mark.integration


def test_user_fresh_claim_is_not_stolen_by_a_stale_sweeper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Given a candidate, when another owner claims it, then no work is repeated.

    The SQL event is an interleaving gate, not a substitute repository: a second
    connection commits a real claim between enumeration and the first UPDATE.
    """
    from tests.migrated_schema import migrated_database

    monkeypatch.setenv("PEARSS_STORAGE_DIR", str(tmp_path / "storage"))
    engine = sa.create_engine(migrated_database(tmp_path, "ownership.db"))
    factory = sessionmaker(engine, expire_on_commit=False)
    chain = '[{"plugin_id":"title-summary","kind":"summary","required":true}]'
    now = datetime(2026, 9, 17, tzinfo=UTC)
    try:
        with factory() as session:
            source = create_source(
                session,
                source_type="rss",
                url="https://ownership.example/feed",
                canonical_url="https://ownership.example/feed",
                schedule_policy='{"type":"fixed","interval":"30m"}',
                tenant_id="default",
                pipeline_chain=chain,
                at=timestamp(now),
            )
            normalise_and_store(
                session,
                [
                    ItemDraft(
                        origin_id=source.id,
                        external_id="article",
                        title="Owned article",
                        canonical_url="https://ownership.example/article",
                        published_at=None,
                        content="<p>Original article.</p>",
                    )
                ],
                chain=parse_chain(chain, target_lang=None),
            )
            session.commit()
            assert session.scalar(sa.select(ItemEnrichment.status)) == "queued"
            row_id = session.scalar(sa.select(ItemEnrichment.id))

        armed = True

        @sa.event.listens_for(engine, "before_cursor_execute")
        def competing_claim(
            connection: Any,
            cursor: Any,
            statement: str,
            parameters: Any,
            context: Any,
            executemany: bool,
        ) -> None:
            nonlocal armed
            if armed and statement.startswith("UPDATE item_enrichment"):
                armed = False
                with factory.begin() as competitor:
                    competitor.execute(
                        sa.update(ItemEnrichment)
                        .where(ItemEnrichment.id == row_id)
                        .values(status="running", updated_at=timestamp(now))
                    )

        with factory() as session:
            report = sweep_queue(
                session,
                processors={"title-summary": TitleSummary},
                now=now,
                row_session_factory=factory,
            )
        with factory() as session:
            assert session.scalar(sa.select(ItemEnrichment.status)) == "running"
            assert session.scalar(sa.select(ItemEnrichment.updated_at)) == timestamp(
                now
            )
            assert session.scalar(sa.select(ItemEnrichment.payload_ref)) is None
            assert session.scalar(sa.select(Item.publish_status)) == "pending"
        assert report == {
            "ran": 0,
            "succeeded": 0,
            "failed": 0,
            "retried": 0,
            "skipped": 0,
        }
    finally:
        engine.dispose()


@pytest.fixture
def queued_product(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[sessionmaker[Session]]:
    from tests.migrated_schema import migrated_database

    monkeypatch.setenv("PEARSS_STORAGE_DIR", str(tmp_path / "storage"))
    engine = sa.create_engine(migrated_database(tmp_path, "product.db"))
    factory = sessionmaker(engine, expire_on_commit=False)
    chain = '[{"plugin_id":"title-summary","kind":"summary","required":true}]'
    try:
        with factory.begin() as session:
            source = create_source(
                session,
                source_type="rss",
                url="https://ownership.example/feed",
                canonical_url="https://ownership.example/feed",
                schedule_policy='{"type":"fixed","interval":"30m"}',
                tenant_id="default",
                pipeline_chain=chain,
                at="2026-09-17T00:00:00.000000Z",
            )
            normalise_and_store(
                session,
                [
                    ItemDraft(
                        origin_id=source.id,
                        external_id="article",
                        title="Owned article",
                        canonical_url="https://ownership.example/article",
                        published_at=None,
                        content="<p>Original article.</p>",
                    )
                ],
                chain=parse_chain(chain, target_lang=None),
            )
        yield factory
    finally:
        engine.dispose()


def test_user_recovers_expired_work_with_an_existing_owner(
    queued_product: sessionmaker[Session],
) -> None:
    """Given a crashed owner, when its lease expires, then processing resumes."""
    factory = queued_product
    now = datetime(2026, 9, 17, tzinfo=UTC)
    with factory.begin() as session:
        session.execute(
            sa.update(ItemEnrichment).values(
                status="running",
                owner_token="crashed-owner",
                updated_at=timestamp(now - timedelta(hours=1)),
            )
        )
    with factory() as session:
        report = sweep_queue(
            session,
            processors={"title-summary": TitleSummary},
            now=now,
            row_session_factory=factory,
        )
    with factory() as session:
        assert session.scalar(sa.select(ItemEnrichment.status)) == "succeeded"
        assert session.scalar(sa.select(Item.publish_status)) == "ready"
        assert (
            filestore.read_body(session.scalar(sa.select(ItemEnrichment.payload_ref)))
            == "<p>Owned article</p>"
        )
    assert report == {"ran": 1, "succeeded": 1, "failed": 0, "retried": 0, "skipped": 0}


@pytest.mark.parametrize("fails", [False, True], ids=["late-success", "late-error"])
def test_user_new_owner_result_survives_a_late_completion(
    queued_product: sessionmaker[Session],
    fails: bool,
) -> None:
    """Given ownership changed during processing, then the old result is ignored."""
    factory = queued_product
    authoritative_ref = filestore.store_body("<p>New owner result.</p>")

    armed = True
    engine = factory.kw["bind"]

    @sa.event.listens_for(engine, "before_cursor_execute")
    def replace_before_completion(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        nonlocal armed
        if (
            armed
            and statement.startswith("UPDATE item_enrichment")
            and "owner_token=" not in statement
        ):
            armed = False
            with factory.begin() as competitor:
                competitor.execute(
                    sa.update(ItemEnrichment).values(
                        status="succeeded",
                        owner_token="new-owner",
                        payload_ref=authoritative_ref,
                        updated_at="2026-09-17T01:00:00.000000Z",
                    )
                )

    with factory() as session:
        report = sweep_queue(
            session,
            processors={"title-summary": FailedSummary if fails else TitleSummary},
            row_session_factory=factory,
        )
    with factory() as session:
        assert session.scalar(sa.select(ItemEnrichment.status)) == "succeeded"
        assert session.scalar(sa.select(ItemEnrichment.owner_token)) == "new-owner"
        assert (
            session.scalar(sa.select(ItemEnrichment.payload_ref)) == authoritative_ref
        )
        assert (
            session.scalar(sa.select(ItemEnrichment.updated_at))
            == "2026-09-17T01:00:00.000000Z"
        )
        assert session.scalar(sa.select(ItemEnrichment.error_class)) is None
        assert session.scalar(sa.select(Item.publish_status)) == "pending"
    assert report == {"ran": 1, "succeeded": 0, "failed": 0, "retried": 0, "skipped": 1}


@pytest.mark.parametrize("offset", [-1, 0, 1], ids=["expired", "at-expiry", "live"])
def test_user_stored_lease_controls_recovery_not_current_timeout(
    queued_product: sessionmaker[Session],
    offset: int,
) -> None:
    """Given an old timestamp, only the stored expiry permits reclaiming work."""
    factory = queued_product
    now = datetime(2026, 9, 17, tzinfo=UTC)
    with factory.begin() as session:
        session.execute(
            sa.update(ItemEnrichment).values(
                status="running",
                owner_token="lease-owner",
                updated_at=timestamp(now - timedelta(hours=1)),
                lease_expires_at=timestamp(now + timedelta(seconds=offset)),
            )
        )
    with factory() as session:
        report = sweep_queue(
            session,
            processors={"title-summary": TitleSummary},
            now=now,
            call_timeout=10,
            row_session_factory=factory,
        )
    with factory() as session:
        assert session.scalar(sa.select(ItemEnrichment.status)) == (
            "running" if offset > 0 else "succeeded"
        )
        assert session.scalar(sa.select(Item.publish_status)) == (
            "pending" if offset > 0 else "ready"
        )
        assert session.scalar(sa.select(ItemEnrichment.lease_expires_at)) == timestamp(
            now + timedelta(seconds=offset if offset > 0 else 15)
        )
    assert report["ran"] == (0 if offset > 0 else 1)


class FailedSummary:
    @staticmethod
    def process(
        item_ctx: dict[str, str], *, target_lang: str, store: Callable[[str], str]
    ) -> str:
        raise RuntimeError("late processor failure")
