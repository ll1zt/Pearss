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

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
async def test_a_kind_nobody_implemented_waits_rather_than_failing(
    session_factory: sessionmaker[Session],
) -> None:
    """The claim filters on the executor map, so an unknown kind waits.

    Every kind the API accepts now has an executor, so this is about the next
    one: a work whose kind this host cannot run is neither claimed nor failed,
    and it stays `queued` with its targets untouched. Written through the
    repository, because the API refuses a kind outside its own list -- which is
    exactly why the worker needs its own guard.
    """
    work_id = _submit(session_factory, kind="teleport", targets=["somewhere"])

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


# --------------------------------------------------------------------------
# AC-207: a subscription batch, URL by URL
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac207_a_subscribe_work_judges_each_url_alone(
    session_factory: sessionmaker[Session],
) -> None:
    """AC-207: usable URLs become sources, a bad one fails only itself.

    The shared options are validated once for the work (a policy or chain this
    deployment cannot use is not a per-URL accident), while each URL is judged
    alone -- refusing a whole list over one typo is the loop the batch endpoint
    exists to break. The already-subscribed URL is `skipped`, and the assertion
    that it was not *touched* is the policy it still carries: `create_source`
    returns the existing row for the same identity rather than rewriting it, and
    a work that quietly re-subscribed it would overwrite that policy.
    """
    with session_factory() as session:
        existing = create_source(
            session,
            source_type="rss",
            url=f"{SITE}/already",
            canonical_url=f"{SITE}/already",
            schedule_policy='{"type": "fixed", "interval": "30m"}',
            tenant_id="default",
            at=timestamp(datetime(2026, 10, 4, 1, 0, tzinfo=UTC)),
        )
        session.commit()
        existing_id = str(existing.id)

    work_id = _submit(
        session_factory,
        kind="subscribe",
        targets=[f"{SITE}/already", f"{SITE}/fresh", "mailto:someone@example.com"],
        params={
            "schedule_policy": {"type": "fixed", "interval": "1h"},
            "type": "rss",
        },
    )
    report = await _sweep(session_factory)

    assert report == {"works": 1, "units": 3}
    units = _units(session_factory, work_id)
    assert units[f"{SITE}/already"][:3] == (
        UnitOutcome.SKIPPED,
        None,
        "already subscribed",
    )
    assert units[f"{SITE}/already"][3] == existing_id
    assert units[f"{SITE}/fresh"][:3] == (UnitOutcome.OK, None, "subscribed as rss")
    assert units["mailto:someone@example.com"][0] is UnitOutcome.FAILED
    assert units["mailto:someone@example.com"][1] is ErrorClass.PARSE

    with session_factory() as session:
        rows = session.execute(
            sa.text("select url, schedule_policy from source order by url")
        ).all()
    # Compared by interval rather than by string: the row keeps whatever form it
    # was stored in, and what this asserts is that the *existing* source was not
    # rewritten by a work that re-subscribed it.
    assert [(row[0], json.loads(row[1])["interval"]) for row in rows] == [
        (f"{SITE}/already", "30m"),
        (f"{SITE}/fresh", "1h"),
    ]


@pytest.mark.asyncio
async def test_a_subscribe_work_with_unusable_options_fails_every_target(
    session_factory: sessionmaker[Session],
) -> None:
    """Options are the work's problem, so they are refused once, with the reason.

    A missing or unusable policy would otherwise fail every target with the same
    sentence, one row at a time -- and the caller would have to read 200 rows to
    learn one thing.
    """
    work_id = _submit(
        session_factory,
        kind="subscribe",
        targets=[f"{SITE}/a", f"{SITE}/b"],
        params={"schedule_policy": {"type": "fixed", "interval": "not-a-duration"}},
    )

    await _sweep(session_factory)

    units = _units(session_factory, work_id)
    assert len(units) == 2
    assert {unit[:2] for unit in units.values()} == {
        (UnitOutcome.FAILED, ErrorClass.STRUCTURE_CHANGED)
    }
    assert all("unusable schedule_policy" in str(unit[2]) for unit in units.values())
    with session_factory() as session:
        assert session.execute(sa.text("select count(*) from source")).scalar_one() == 0


# --------------------------------------------------------------------------
# AC-208: pushed content, through the one write path
# --------------------------------------------------------------------------


def _ingest_target(**fields: Any) -> str:
    """One item as canonical JSON: the shape an ingest target carries."""
    return json.dumps(fields, separators=(",", ":"), sort_keys=True)


@pytest.mark.asyncio
async def test_ac208_an_ingest_work_stores_through_the_same_path(
    session_factory: sessionmaker[Session],
) -> None:
    """AC-208: pushed content lands through `normalise_and_store`, once.

    The reconciliation is the interesting half: the same content pushed again --
    by a second work, i.e. through the same call the `:ingest` endpoint makes --
    must find the row the first push stored rather than minting a second one. An
    identity rule with two implementations is exactly how that breaks.
    """
    with session_factory() as session:
        agent = create_source(
            session,
            source_type="agent",
            url="agent://inbox",
            canonical_url="agent://inbox",
            schedule_policy='{"type": "fixed", "interval": "30m"}',
            tenant_id="default",
            at=timestamp(datetime(2026, 10, 4, 2, 0, tzinfo=UTC)),
        )
        session.commit()
        agent_id = str(agent.id)

    # A target is the item as canonical JSON, which is also the ledger's key.
    with_url = _ingest_target(
        external_id="pushed-1",
        title="Pushed one",
        canonical_url=f"{SITE}/pushed/1",
        content="<p>First body.</p>",
    )
    # No usable URL: the identity is minted under agent.invalid, which is the
    # rule both entry points share.
    minted_target = _ingest_target(
        external_id="pushed-2", title="Pushed two", content="<p>Two.</p>"
    )
    broken = "not json at all"

    first = _submit(
        session_factory,
        kind="ingest",
        targets=[with_url, minted_target, broken],
        params={"source_id": agent_id},
    )
    report = await _sweep(session_factory)

    assert report == {"works": 1, "units": 3}
    units = _units(session_factory, first)
    assert units[with_url][0] is UnitOutcome.OK
    assert units[broken][0] is UnitOutcome.FAILED
    assert units[broken][1] is ErrorClass.PARSE

    with session_factory() as session:
        rows = session.execute(
            sa.text("select id, url, guid, content_hash from item order by url")
        ).all()
    # Ordered by url, which is why agent.invalid sorts first.
    assert [row[1] for row in rows] == [
        "https://agent.invalid/inbox/pushed-2",
        f"{SITE}/pushed/1",
    ]
    stored = {row[1]: row[0] for row in rows}
    assert units[with_url][3] == stored[f"{SITE}/pushed/1"]
    # The detail is the dedupe's own reason (the fallback only fires when it has
    # nothing to say), so what is pinned here is the outcome and the reference.
    assert units[minted_target][0] is UnitOutcome.OK
    assert units[minted_target][2]
    assert units[minted_target][3] == stored["https://agent.invalid/inbox/pushed-2"]

    # The same content again: the dedupe finds it, and the ledger says so.
    again = _submit(
        session_factory,
        kind="ingest",
        targets=[with_url],
        params={"source_id": agent_id},
    )
    await _sweep(session_factory)

    repeated = _units(session_factory, again)[with_url]
    assert repeated[0] is UnitOutcome.SKIPPED
    assert repeated[3] == stored[f"{SITE}/pushed/1"]
    with session_factory() as session:
        assert session.execute(sa.text("select count(*) from item")).scalar_one() == 2


@pytest.mark.asyncio
async def test_an_ingest_work_about_a_non_agent_source_fails_every_target(
    session_factory: sessionmaker[Session], source_id: str
) -> None:
    """Pushing into an rss source would mix one origin's content into another's.

    The refusal is the source's, so it is one sentence for the whole work rather
    than a per-target accident -- the same 409 the endpoint answers, in the
    ledger's vocabulary.
    """
    work_id = _submit(
        session_factory,
        kind="ingest",
        targets=[_ingest_target(external_id="x", title="X", content="<p>x</p>")],
        params={"source_id": source_id},
    )

    await _sweep(session_factory)

    units = _units(session_factory, work_id)
    [unit] = units.values()
    assert unit[0] is UnitOutcome.FAILED and unit[1] is ErrorClass.STRUCTURE_CHANGED
    assert "only agent sources accept pushed items" in str(unit[2])
    with session_factory() as session:
        assert session.execute(sa.text("select count(*) from item")).scalar_one() == 0
