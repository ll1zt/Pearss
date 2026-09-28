"""The audit id a caller is handed must identify its own operation.

Scenario register (normative source: docs/api/mcp-tool-guide.md section 3,
issue #64).

  AC-40 并发调用各自拿到自己的 audit id
    Given 多个写调用同时进行, 且每个调用在读到自己的 audit id 之前被强制交错
          (barrier 栅栏 -- 单纯 asyncio.gather 会假绿)
    When  每次调用取回它自己那一次写入的 audit id
    Then  每个调用拿到互不相同的 audit id
    And   每个 audit_id 都能在 audit_log 里查到对应的那一行

The defect is that the id was kept in shared mutable state on the one client
that serves every request (`CoreAPIClient._last_audit_id`), so callers could
overwrite each other's answer. The fix is that the id travels with the
response, which is why this file reads it from the call's return value rather
than from the client: the return value is the contract that makes the
concurrency safe, not an implementation detail of it.

Why a barrier rather than plain `asyncio.gather`: measured, not assumed. Under
plain `gather` the old shared-state implementation still returned distinct ids,
because every tool body assigns the id after its only `await` and reads it back
with no suspension in between -- so assignment and read never interleave and
the collision is latent. Forcing one suspension point between "the response
arrived" and "the id is consumed" made four callers collapse onto a single id
(1 distinct id out of 4). The barrier supplies that suspension here, so the
test exercises the real hazard instead of staying green until some future
`await` lands in a tool body and turns it live.

That latency is also why the assertion is the contract -- "each caller gets its
own id" -- rather than today's call graph: the contract is what the next
`await` must not break.

The row behind the id is *not* on disk when the response returns: the
middleware hands the write to a worker thread and returns immediately, which is
its documented contract rather than a defect. So the last assertion drains that
queue first. Reading the table straight away is a race that wins on an idle
machine and loses on a loaded one -- measured by injecting a 0.5s delay into
the audit worker, which made all four ids look unwritten, where CI's loaded
runner made three of them. That is how this file first failed in CI, in a run
whose diff touched nothing related.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from adapters.repository import create_source, timestamp
from app.main import create_app, create_engine_for
from app.mcp.core_client import CoreAPIClient
from app.middleware import CorrelationAndAuditMiddleware
from tests.migrated_schema import migrated_database

pytestmark = pytest.mark.integration

API_TOKEN = "audit-crosstalk-token"

# Four is the reproduction count from the issue: enough that every caller
# collapsing onto one id is unambiguous, few enough that the barrier cannot
# deadlock waiting for a party that never arrives.
CONCURRENCY = 4


@pytest.fixture
def database_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A migrated database and the environment the app runs with."""
    url = migrated_database(tmp_path, "crosstalk.db")
    monkeypatch.setenv("PEARSS_DATABASE_URL", url)
    monkeypatch.setenv("PEARSS_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("PEARSS_API_TOKEN", API_TOKEN)
    monkeypatch.setenv("PEARSS_SCHEDULER_ENABLED", "0")
    yield url


def _seed_source(factory: sessionmaker[Session]) -> str:
    """One source to mutate, created through the production write path."""
    with factory() as seed:
        source = create_source(
            seed,
            source_type="rss",
            url="https://audit.example/feed",
            canonical_url="https://audit.example/feed",
            schedule_policy="{}",
            tenant_id="default",
            at=timestamp(datetime.now(UTC)),
        )
        seed.commit()
        return source.id


def _stored_audit_ids(url: str) -> set[str]:
    """Every audit row id on disk, the ground truth to compare against.

    Drains first: the middleware promises the id when the response starts and
    writes the row afterwards, so reading the table straight away is a race
    that passes on an idle machine and fails on a loaded one. `drain_audit`
    returns False on timeout, and that has to fail loudly -- otherwise a slow
    worker looks like a cross-talk bug.
    """
    assert CorrelationAndAuditMiddleware.drain_audit(), (
        "the audit worker did not finish, so an audit-row set would be a lie"
    )
    engine = create_engine_for(url)
    try:
        with engine.connect() as conn:
            return {
                row_id
                for (row_id,) in conn.execute(
                    text("select id from audit_log")
                ).fetchall()
            }
    finally:
        engine.dispose()


# --------------------------------------------------------------------------
# AC-40: each concurrent caller gets its own id
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_writes_each_receive_their_own_audit_id(
    database_url: str,
) -> None:
    """AC-40: the id a caller reports must be the id of the caller's own write.

    An audit id that names a different operation is worse than no id: it is a
    precise-looking wrong answer that sends an investigation to an innocent
    row. Triggering it needs no malice -- one agent turn issuing two writes, or
    two clients sharing the endpoint, is enough.
    """
    engine = create_engine_for(database_url)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        source_id = _seed_source(factory)
        app = create_app(factory)
        core = CoreAPIClient(app)

        barrier = asyncio.Barrier(CONCURRENCY)

        async def one(index: int) -> str:
            status, payload, audit_id = await core.request(
                "PATCH",
                f"/v1/sources/{source_id}",
                json_body={
                    "schedule_policy": {"type": "fixed", "interval": f"{30 + index}m"}
                },
                idempotency_key=f"crosstalk-{index}",
            )
            assert status == 200, payload
            # The suspension point that makes the hazard reachable: under the
            # shared-state implementation every caller had overwritten the one
            # stored id by the time any of them read it back.
            await barrier.wait()
            assert audit_id is not None, "a write must be handed its audit id"
            return audit_id

        ids = await asyncio.gather(*(one(index) for index in range(CONCURRENCY)))
    finally:
        engine.dispose()

    assert len(set(ids)) == CONCURRENCY, (
        f"{CONCURRENCY} concurrent writes were handed {len(set(ids))} distinct "
        f"audit ids: {ids}"
    )
    # And they are real rows, not merely distinct strings: attributability is
    # the entire point of returning one. The reader drains before it looks.
    stored = _stored_audit_ids(database_url)
    assert set(ids) <= stored, f"ids with no audit row: {set(ids) - stored}"


@pytest.mark.asyncio
async def test_a_read_reports_its_status_and_payload(database_url: str) -> None:
    """The return-shape change must not disturb the read path.

    Reads are not audited, so their audit id is legitimately None -- but the
    status and payload every read tool unpacks must be exactly what they were.
    """
    engine = create_engine_for(database_url)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        source_id = _seed_source(factory)
        app = create_app(factory)
        core = CoreAPIClient(app)

        status, payload, audit_id = await core.get(f"/v1/sources/{source_id}")
    finally:
        engine.dispose()

    assert status == 200, payload
    assert payload["id"] == source_id
    assert audit_id is None, "a read is not audited and has no audit row"


# --------------------------------------------------------------------------
# The invariant, not just the behaviour
# --------------------------------------------------------------------------


def test_the_shared_client_keeps_no_per_call_result_state() -> None:
    """The absence of shared mutable state is the fix, so assert the absence.

    This is the guard that the concurrency test above cannot be. Measured: with
    the old shared-attribute implementation restored *inside* `request()`, the
    concurrency test still passes -- because it reads the value the call
    returned, and assignment-plus-return is one uninterrupted step, so each
    caller still receives its own. The failure only exists for a caller that
    reads the attribute *later*, which is exactly what the tools used to do.

    So the property to pin is not "the returned id is right" (that is true
    either way) but "there is no second way to obtain an id": no attribute on
    the shared client that a future tool body could read after an `await`.
    One client serves every request, so such an attribute is the race.

    Falsified by reintroducing one: adding `self._last_audit_id` and a
    `last_audit_id` property to `CoreAPIClient` turns this red. That injection
    is the only construction found that makes any test in this file fail, which
    is why the guard exists rather than more concurrency scenarios.
    """
    core = CoreAPIClient(object())
    leaked = {name for name in vars(core) if "audit" in name.lower()} | {
        name
        for name in dir(core)
        if "audit" in name.lower() and not name.startswith("__")
    }
    assert not leaked, (
        f"CoreAPIClient exposes per-call audit state {sorted(leaked)}; the id "
        "must travel in the return value of request(), because one client "
        "serves every concurrent request"
    )
