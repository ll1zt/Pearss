"""The other kinds: a work is a mechanism, not a fetch endpoint.

Scenario register (normative source: SCENARIOS.md, AC-205..206; issue #197).

`kind=fetch` was the first executor (issue #190) and the shape of the mechanism:
claim, resolve once, run one target at a time, record each outcome, close. These
scenarios are about the kinds that need no outbound request -- `delete`,
`enrich`, `retry` -- which is the point: the same ledger, the same vocabulary,
and no second implementation of "what happened to this target".

They drive one sweep directly, as `test_work_fetch.py` does, so what is asserted
is the execution rather than the timing of a background loop.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from adapters.db_models import Item as ItemRow
from adapters.db_models import ItemEnrichment
from adapters.filestore import path_for, store_body
from adapters.repository import (
    append_unit,
    create_source,
    create_work,
    units_for_work,
    work_by_id,
)
from adapters.timestamps import fixed_width_utc as timestamp
from app.main import create_engine_for
from app.work_worker import WorkWorker
from core.enums import ErrorClass, UnitOutcome, WorkStatus
from tests.migrated_schema import migrated_database

SITE = "https://kinds.example"
FEED_URL = f"{SITE}/blog/feed"

pytestmark = pytest.mark.integration


@pytest.fixture
def session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    url = migrated_database(tmp_path, "work_kinds.db")
    engine = create_engine_for(url)
    try:
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        engine.dispose()


@pytest.fixture
def source_id(session_factory: sessionmaker[Session]) -> str:
    """A source with a one-spec chain: what `enrich` has to queue."""
    with session_factory() as session:
        row = create_source(
            session,
            source_type="rss",
            url=FEED_URL,
            canonical_url=FEED_URL,
            schedule_policy='{"type": "fixed", "interval": "30m"}',
            pipeline_chain=(
                '[{"plugin_id": "translate", "kind": "translation", "lang": "en"}]'
            ),
            target_lang="en",
            tenant_id="default",
            at=timestamp(datetime(2026, 10, 2, 3, 0, tzinfo=UTC)),
        )
        session.commit()
        return str(row.id)


def _seed_item(
    session_factory: sessionmaker[Session],
    *,
    item_id: str,
    source_id: str,
    content_ref: str | None = None,
) -> None:
    """An item through the ORM: there is no public "create one item" call."""
    now = timestamp(datetime(2026, 10, 2, 4, 0, tzinfo=UTC))
    with session_factory() as session:
        session.add(
            ItemRow(
                id=item_id,
                source_id=source_id,
                guid=f"guid-{item_id}",
                content_hash=f"hash-{item_id}",
                title=f"Item {item_id}",
                url=f"{SITE}/blog/{item_id}",
                publish_status="ready",
                tenant_id="default",
                created_at=now,
                updated_at=now,
                content_ref=content_ref,
            )
        )
        session.commit()


def _seed_enrichment(
    session_factory: sessionmaker[Session],
    *,
    enrichment_id: str,
    item_id: str,
    status: str,
) -> None:
    now = timestamp(datetime(2026, 10, 2, 4, 0, tzinfo=UTC))
    with session_factory() as session:
        session.add(
            ItemEnrichment(
                id=enrichment_id,
                item_id=item_id,
                plugin_id="translate",
                plugin_version="0.1.0",
                kind="translation",
                lang="en",
                input_hash=f"hash-{item_id}",
                status=status,
                error_class=None if status != "failed" else "auth",
                detail=None if status != "failed" else "provider said no",
                attempt_count=0 if status != "failed" else 3,
                tenant_id="default",
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()


def _submit(
    session_factory: sessionmaker[Session],
    *,
    kind: str,
    targets: list[str],
    params: dict[str, object] | None = None,
) -> str:
    with session_factory() as session:
        work = create_work(
            session,
            kind=kind,
            targets=targets,
            params=params or {},
            fingerprint=f"fp-{kind}-{len(targets)}-{targets[0] if targets else ''}",
            tenant_id="default",
        )
        session.commit()
        return work.id


async def _sweep(session_factory: sessionmaker[Session]) -> dict[str, int]:
    """One pass of the real worker, with a client it will not need."""
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(599, text="no request belongs to this kind")
        )
    )
    worker = WorkWorker(session_factory, client=client)
    try:
        return await worker.sweep_once()
    finally:
        client.close()


def _units(
    session_factory: sessionmaker[Session], work_id: str
) -> dict[str, tuple[UnitOutcome, ErrorClass | None, str, str | None]]:
    """The work's ledger rows by target: outcome, class, detail, reference."""
    with session_factory() as session:
        return {
            row.target: (row.outcome, row.error_class, row.detail, row.ref)
            for row in units_for_work(session, work_id)
        }


# --------------------------------------------------------------------------
# AC-205: a delete work, target by target
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac205_a_delete_work_records_every_target_and_skips_the_missing(
    session_factory: sessionmaker[Session], source_id: str
) -> None:
    """AC-205: present items are `ok`, missing ones `skipped` -- not failed.

    A missing id is the end state the caller asked for, and a client retrying a
    lost response has to see the same success the first attempt produced. It is
    also how the two outcomes stay readable: `failed` means "this needs a human",
    and an id that was already deleted does not.
    """
    for item_id in ("gone-1", "stays-2"):
        _seed_item(session_factory, item_id=item_id, source_id=source_id)

    work_id = _submit(
        session_factory, kind="delete", targets=["gone-1", "never-existed"]
    )
    report = await _sweep(session_factory)

    assert report == {"works": 1, "units": 2}
    units = _units(session_factory, work_id)
    assert units["gone-1"][0] is UnitOutcome.OK
    assert units["never-existed"][0] is UnitOutcome.SKIPPED
    assert units["never-existed"][2] == "already gone"
    with session_factory() as session:
        remaining = session.execute(
            sa.text("select id from item order by id")
        ).scalars()
        assert list(remaining) == ["stays-2"]
        work = work_by_id(session, work_id)
    assert work is not None and work.status is WorkStatus.SUCCEEDED
    # The untouched item was never touched, by value.
    assert units["gone-1"][1] is None


@pytest.mark.asyncio
async def test_ac205_a_delete_work_reclaims_the_files_it_orphaned(
    session_factory: sessionmaker[Session], source_id: str
) -> None:
    """The files go, and only after the commit.

    `ReclaimPlan` exists because the two halves have different atomicity: rows
    come back on a rollback, unlinked files do not. The work loop therefore runs
    the plan once the row is committed -- and this is the only test that watches
    a *work* do it.
    """
    ref = store_body("<p>Delete me.</p>")
    _seed_item(
        session_factory, item_id="with-body", source_id=source_id, content_ref=ref
    )
    path = path_for(ref)
    assert path is not None and path.exists()

    work_id = _submit(session_factory, kind="delete", targets=["with-body"])
    await _sweep(session_factory)

    units = _units(session_factory, work_id)
    assert units["with-body"][0] is UnitOutcome.OK
    assert not path.exists(), "the body file outlived the row that referenced it"


# --------------------------------------------------------------------------
# AC-206: enrich and retry reuse the existing rules
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac206_an_enrich_work_queues_what_a_backfill_would(
    session_factory: sessionmaker[Session], source_id: str
) -> None:
    """AC-206: the work path and `backfill_chain` queue the same rows.

    Reconciled rather than described: the same source is enriched through a work
    for one item and through `backfill_chain` for the other, and the two
    resulting rows are compared column by column. "The rules are the same" is
    exactly the claim that rots silently when two paths each own a copy of them.
    """
    from adapters.repository import backfill_chain

    _seed_item(session_factory, item_id="through-work", source_id=source_id)
    _seed_item(session_factory, item_id="through-backfill", source_id=source_id)

    work_id = _submit(
        session_factory,
        kind="enrich",
        targets=["through-work"],
        params={"source_id": source_id},
    )
    await _sweep(session_factory)
    with session_factory() as session:
        queued = backfill_chain(session, source_id=source_id)
        session.commit()
    assert queued == 1, "the other item's row"

    with session_factory() as session:
        rows = session.execute(
            sa.text(
                "select item_id, plugin_id, plugin_version, kind, lang, status,"
                " attempt_count, next_attempt_at from item_enrichment"
                " order by item_id"
            )
        ).all()
    assert [tuple(row) for row in rows] == [
        (
            "through-backfill",
            "translate",
            "0.1.0",
            "translation",
            "en",
            "queued",
            0,
            None,
        ),
        (
            "through-work",
            "translate",
            "0.1.0",
            "translation",
            "en",
            "queued",
            0,
            None,
        ),
    ]
    assert _units(session_factory, work_id)["through-work"][0] is UnitOutcome.OK


@pytest.mark.asyncio
async def test_ac206_enriching_twice_is_skipped_not_failed(
    session_factory: sessionmaker[Session], source_id: str
) -> None:
    """The identity is the idempotency key, so a second run has nothing to do.

    Storing the same item twice queues nothing new (the unique constraint), and
    a work that asked for it again did get what it asked for: the products are
    queued. Reporting `failed` would invite a retry that can never do anything.
    """
    _seed_item(session_factory, item_id="once", source_id=source_id)

    first = _submit(
        session_factory,
        kind="enrich",
        targets=["once"],
        params={"source_id": source_id},
    )
    await _sweep(session_factory)
    second = _submit(
        session_factory,
        kind="enrich",
        targets=["once"],
        params={"source_id": source_id},
    )
    await _sweep(session_factory)

    assert _units(session_factory, first)["once"][0] is UnitOutcome.OK
    assert _units(session_factory, second)["once"][:3] == (
        UnitOutcome.SKIPPED,
        None,
        "its products are already queued",
    )


@pytest.mark.asyncio
async def test_ac206_a_retry_work_requeues_only_the_failed_row(
    session_factory: sessionmaker[Session], source_id: str
) -> None:
    """AC-206: one rule for retrying, and per-target outcomes for the rest.

    A failed row comes back; a succeeded one has its product already (so there
    is nothing to do, and re-producing it is a different command); an id that
    names nothing is a failure the caller has to hear about.
    """
    # One row per item: the chain identity (item, plugin, version, kind, lang,
    # input_hash) is unique, which is the idempotency key the retry relies on.
    for item_id, enrichment_id, status in (
        ("retry-item-a", "retry-failed", "failed"),
        ("retry-item-b", "retry-succeeded", "succeeded"),
    ):
        _seed_item(session_factory, item_id=item_id, source_id=source_id)
        _seed_enrichment(
            session_factory,
            enrichment_id=enrichment_id,
            item_id=item_id,
            status=status,
        )

    work_id = _submit(
        session_factory,
        kind="retry",
        targets=["retry-failed", "retry-succeeded", "retry-missing"],
    )
    await _sweep(session_factory)

    units = _units(session_factory, work_id)
    assert units["retry-failed"][0] is UnitOutcome.OK
    assert units["retry-failed"][2] == "requeued"
    assert units["retry-succeeded"][:3] == (
        UnitOutcome.SKIPPED,
        None,
        "already succeeded: its product exists",
    )
    assert units["retry-missing"][:3] == (
        UnitOutcome.FAILED,
        ErrorClass.STRUCTURE_CHANGED,
        "no enrichment row 'retry-missing'",
    )

    with session_factory() as session:
        rows = {
            row[0]: row[1]
            for row in session.execute(
                sa.text("select id, status from item_enrichment")
            ).all()
        }
    assert rows == {"retry-failed": "queued", "retry-succeeded": "succeeded"}


# --------------------------------------------------------------------------
# What the mechanism refuses, and what it does not implement yet
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_work_about_a_missing_source_fails_every_target_with_the_reason(
    session_factory: sessionmaker[Session],
) -> None:
    """A work naming something this deployment cannot use is not left queued.

    Every target gets a row saying why, which is what keeps the ledger's promise
    -- "somewhere to look up what happened" -- true for this failure too.
    """
    work_id = _submit(
        session_factory,
        kind="enrich",
        targets=["item-a", "item-b"],
        params={"source_id": "no-such-source"},
    )
    await _sweep(session_factory)

    units = _units(session_factory, work_id)
    assert {unit[:3] for unit in units.values()} == {
        (UnitOutcome.FAILED, ErrorClass.STRUCTURE_CHANGED, "no source 'no-such-source'")
    }
    with session_factory() as session:
        work = work_by_id(session, work_id)
    assert work is not None and work.status is WorkStatus.FAILED


@pytest.mark.asyncio
async def test_a_kind_without_an_executor_waits_rather_than_failing(
    session_factory: sessionmaker[Session],
) -> None:
    """The kinds that need domain validation belong to another change.

    `subscribe` and `ingest` are accepted by the API (the kind is known) but no
    executor claims them yet, so a sweep leaves them queued. That is the design
    (issue #190): a kind can be accepted before it is implemented, and the work
    is not claimed and failed in the meantime. Their scenarios live in the issue
    that implements them, which is why no scenario number is cited here.
    """
    work_id = _submit(session_factory, kind="subscribe", targets=[f"{SITE}/feed"])

    assert await _sweep(session_factory) == {"works": 0, "units": 0}
    with session_factory() as session:
        work = work_by_id(session, work_id)
        assert work is not None and work.status is WorkStatus.QUEUED
        assert units_for_work(session, work_id) == []


@pytest.mark.asyncio
async def test_a_resumed_pass_skips_targets_that_already_have_a_row(
    session_factory: sessionmaker[Session], source_id: str
) -> None:
    """Resumption is the ledger, not a cursor (this is #190's property, kept).

    Written as a witness that the *generic* loop still has it after the kinds
    moved out: the second target already has a row, so it is not executed again.
    """
    for item_id in ("resume-a", "resume-b"):
        _seed_item(session_factory, item_id=item_id, source_id=source_id)
    work_id = _submit(
        session_factory,
        kind="delete",
        targets=["resume-a", "resume-b"],
    )
    with session_factory() as session:
        append_unit(
            session,
            work_id=work_id,
            target="resume-b",
            outcome=UnitOutcome.OK,
            detail="deleted by the pass that died",
        )
        session.commit()

    report = await _sweep(session_factory)

    assert report == {"works": 1, "units": 1}
    with session_factory() as session:
        assert session.get(ItemRow, "resume-a") is None
        assert session.get(ItemRow, "resume-b") is not None, "not re-executed"
