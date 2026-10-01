"""The fetch executor: a work's targets, one ledger row each.

Scenario register (normative source: SCENARIOS.md, AC-189..192; issue #190).

A work is executed by `WorkWorker`, which claims it, runs one unit per target and
writes the outcome as it goes. These tests drive one sweep directly rather than
waiting for the loop, so what is asserted is the execution, not the timing of a
background task.

The origin is a `MockTransport` that also records every request it sees: two of
the scenarios are about requests that must *not* happen, and "the origin was
never asked" is only observable at the origin. No mocks of anything inside this
repository: the ledger, the dedupe and the gate are the real ones.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

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

SITE = "https://bear.example.com"
FEED_URL = f"{SITE}/blog/feed"
ARTICLE_URL = f"{SITE}/blog/archives/2379"
OFF_SITE_URL = "https://elsewhere.example/blog/archives/1"
MISSING_URL = f"{SITE}/blog/archives/999999"
REST_PATH = "/blog/wp-json/wp/v2/posts"
REST_CONTENT = (
    "<p>UAE で免許を更新するには政府指定の眼科で視力検査を受ける必要がある。"
    "日本だと両眼で 0.7 以上、UAE だと 0.5 見えれば良いらしい。</p>"
)
GUID = f"{SITE}/blog/?p=2379"

pytestmark = pytest.mark.integration


class Origin:
    """A recorded origin: it answers what this path fetches, or refuses."""

    def __init__(self, *, forbidden: bool = False) -> None:
        self.requests: list[str] = []
        self.forbidden = forbidden

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        if self.forbidden:
            return httpx.Response(403, text="stop")
        if request.url.path == REST_PATH:
            include = request.url.params.get("include")
            body = (
                [
                    {
                        "id": 2379,
                        "link": ARTICLE_URL,
                        "guid": {"rendered": GUID},
                        "title": {"rendered": "視力検査と昼食"},
                        "content": {"rendered": REST_CONTENT},
                        "date_gmt": "2026-07-27T15:00:00",
                    }
                ]
                if include in (None, "2379")
                else []
            )
            return httpx.Response(
                200, json=body, headers={"content-type": "application/json"}
            )
        return httpx.Response(404, text="no such address")


@pytest.fixture
def session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    url = migrated_database(tmp_path, "work_fetch.db")
    engine = create_engine_for(url)
    try:
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        engine.dispose()


@pytest.fixture
def source_id(session_factory: sessionmaker[Session]) -> str:
    with session_factory() as session:
        row = create_source(
            session,
            source_type="rss",
            url=FEED_URL,
            canonical_url=FEED_URL,
            schedule_policy='{"type": "fixed", "interval": "30m"}',
            tenant_id="default",
            at=timestamp(datetime(2026, 10, 1, 3, 0, tzinfo=UTC)),
        )
        session.commit()
        return str(row.id)


def _worker(
    session_factory: sessionmaker[Session], origin: Origin
) -> tuple[WorkWorker, httpx.Client]:
    client = httpx.Client(transport=httpx.MockTransport(origin.handler))
    return WorkWorker(session_factory, client=client), client


def _submit(
    session_factory: sessionmaker[Session],
    *,
    source_id: str,
    targets: list[str],
    fingerprint: str = "fp-fetch",
    params: dict[str, object] | None = None,
) -> str:
    with session_factory() as session:
        work = create_work(
            session,
            kind="fetch",
            targets=targets,
            params={"source_id": source_id, **(params or {})},
            fingerprint=fingerprint,
            tenant_id="default",
        )
        session.commit()
        return work.id


def _items(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    with session_factory() as session:
        rows = session.execute(
            sa.text("select id, guid, url, title from item order by url")
        ).mappings()
        return [dict(row) for row in rows]


# --------------------------------------------------------------------------
# AC-189: a same-host address is fetched, stored, and recorded
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac189_a_same_host_address_is_stored_and_recorded(
    session_factory: sessionmaker[Session], source_id: str
) -> None:
    """AC-189: one unit per target, `ok`, pointing at the item it produced."""
    origin = Origin()
    worker, client = _worker(session_factory, origin)
    work_id = _submit(session_factory, source_id=source_id, targets=[ARTICLE_URL])
    try:
        report = await worker.sweep_once()
    finally:
        client.close()

    assert report == {"works": 1, "units": 1}
    with session_factory() as session:
        work = work_by_id(session, work_id)
        units = units_for_work(session, work_id)
    assert work is not None and work.status is WorkStatus.SUCCEEDED
    assert [(u.target, u.outcome) for u in units] == [(ARTICLE_URL, UnitOutcome.OK)]
    assert units[0].ref

    items = _items(session_factory)
    assert len(items) == 1
    assert items[0]["url"] == ARTICLE_URL
    assert units[0].ref == items[0]["id"]

    # The identity question, asked behaviourally: fetching the same address
    # again must land on that row instead of growing a second one. (`item.guid`
    # is a derived digest of (tenant, source, external_id), so comparing it
    # with the feed's <guid> string would compare two different things.)
    again = _submit(
        session_factory,
        source_id=source_id,
        targets=[ARTICLE_URL],
        fingerprint="fp-fetch-again",
    )
    worker2, client2 = _worker(session_factory, origin)
    try:
        await worker2.sweep_once()
    finally:
        client2.close()
    with session_factory() as session:
        second = units_for_work(session, again)
    assert [u.outcome for u in second] == [UnitOutcome.OK]
    assert second[0].ref == items[0]["id"], "the same article, not a second row"
    assert len(_items(session_factory)) == 1


# --------------------------------------------------------------------------
# AC-190: an address off the source's host is refused, untouched
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac190_an_address_on_another_host_is_never_fetched(
    session_factory: sessionmaker[Session], source_id: str
) -> None:
    """AC-190: rejected, zero requests, zero rows.

    `source_id` is the provenance boundary; a list of addresses is not a licence
    to fetch from servers this source never agreed to talk to.
    """
    origin = Origin()
    worker, client = _worker(session_factory, origin)
    work_id = _submit(session_factory, source_id=source_id, targets=[OFF_SITE_URL])
    try:
        await worker.sweep_once()
    finally:
        client.close()

    with session_factory() as session:
        units = units_for_work(session, work_id)
    assert [(u.target, u.outcome, u.error_class) for u in units] == [
        (OFF_SITE_URL, UnitOutcome.REJECTED, ErrorClass.STRUCTURE_CHANGED)
    ]
    assert origin.requests == []
    assert _items(session_factory) == []


# --------------------------------------------------------------------------
# AC-191: one bad target does not take the batch down
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac191_one_failing_address_leaves_the_others_intact(
    session_factory: sessionmaker[Session], source_id: str
) -> None:
    """AC-191: the work is `partial`, each outcome carries its own class."""
    origin = Origin()
    worker, client = _worker(session_factory, origin)
    work_id = _submit(
        session_factory, source_id=source_id, targets=[MISSING_URL, ARTICLE_URL]
    )
    try:
        await worker.sweep_once()
    finally:
        client.close()

    with session_factory() as session:
        work = work_by_id(session, work_id)
        by_target = {u.target: u for u in units_for_work(session, work_id)}
    assert work is not None and work.status is WorkStatus.PARTIAL
    assert by_target[ARTICLE_URL].outcome is UnitOutcome.OK
    assert by_target[MISSING_URL].outcome is UnitOutcome.FAILED
    assert by_target[MISSING_URL].error_class is not None
    assert [item["url"] for item in _items(session_factory)] == [ARTICLE_URL]


# --------------------------------------------------------------------------
# AC-192: a blocked origin stops the pass
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac192_a_blocked_origin_ends_the_pass(
    session_factory: sessionmaker[Session], source_id: str
) -> None:
    """AC-192: the refusal is recorded, the rest are not attempted.

    The origin answered 403: it is asking this deployment to stop, and trying
    the next target is the burst the pacing exists to prevent. Exactly one
    request proves the remaining targets were never fetched.
    """
    origin = Origin(forbidden=True)
    worker, client = _worker(session_factory, origin)
    targets = [ARTICLE_URL, f"{SITE}/blog/archives/2389", f"{SITE}/blog/archives/2385"]
    work_id = _submit(session_factory, source_id=source_id, targets=targets)
    try:
        await worker.sweep_once()
    finally:
        client.close()

    with session_factory() as session:
        work = work_by_id(session, work_id)
        by_target = {u.target: u for u in units_for_work(session, work_id)}
    assert work is not None and work.status is WorkStatus.FAILED
    assert by_target[targets[0]].outcome is UnitOutcome.FAILED
    assert by_target[targets[0]].error_class is ErrorClass.BLOCKED
    for rest in targets[1:]:
        assert by_target[rest].outcome is UnitOutcome.REJECTED
        assert by_target[rest].error_class is ErrorClass.BLOCKED
    # Which *targets* were attempted is the question, not how many probes the
    # collector spent on the first one: nothing may name a later address.
    later = targets[1:]
    assert origin.requests, "the first target was attempted"
    assert not [url for url in origin.requests if any(t in url for t in later)]
    assert _items(session_factory) == []


# --------------------------------------------------------------------------
# Resumability and the missing-source case
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_resumed_pass_only_runs_targets_without_a_row(
    session_factory: sessionmaker[Session], source_id: str
) -> None:
    """The ledger is the progress record: a row means "already done".

    A pass that dies mid-way leaves the work `running` with the rows it wrote,
    so continuing must fetch only what has no row. That is the difference
    between resuming and restarting -- and re-fetching what already ran is work
    the caller did not ask for twice.
    """
    origin = Origin()
    worker, client = _worker(session_factory, origin)
    targets = [ARTICLE_URL, f"{SITE}/blog/archives/2389"]
    work_id = _submit(session_factory, source_id=source_id, targets=targets)
    # What a crashed pass leaves behind: a row for the second target, none for
    # the first.
    with session_factory() as session:
        append_unit(
            session,
            work_id=work_id,
            target=targets[1],
            outcome=UnitOutcome.OK,
            detail="stored by the pass that died",
        )
        session.commit()
    try:
        await worker.sweep_once()
        second = await worker.sweep_once()
    finally:
        client.close()

    assert second == {"works": 0, "units": 0}, "a terminal work is not re-claimed"
    assert not [url for url in origin.requests if "2389" in url], origin.requests
    with session_factory() as session:
        work = work_by_id(session, work_id)
        units = units_for_work(session, work_id)
    assert {u.target for u in units} == set(targets), "the earlier row survived"
    assert work is not None and work.status is WorkStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_a_work_naming_an_unknown_source_accounts_for_every_target(
    session_factory: sessionmaker[Session],
) -> None:
    """A work whose source is gone must not sit queued forever.

    Every target gets a row saying why, which is what keeps the ledger's promise
    -- "somewhere to look up what happened" -- true for this failure too.
    """
    origin = Origin()
    worker, client = _worker(session_factory, origin)
    work_id = _submit(
        session_factory, source_id="no-such-source", targets=[ARTICLE_URL]
    )
    try:
        await worker.sweep_once()
    finally:
        client.close()

    with session_factory() as session:
        work = work_by_id(session, work_id)
        units = units_for_work(session, work_id)
    assert work is not None and work.status is WorkStatus.FAILED
    assert [(u.outcome, u.error_class) for u in units] == [
        (UnitOutcome.FAILED, ErrorClass.STRUCTURE_CHANGED)
    ]
    assert origin.requests == []
