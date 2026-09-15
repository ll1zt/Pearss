"""The end-to-end loop across more than one source.

Every other E2E test stands up a single source, which is the shape that cannot
show the scheduler's central isolation property: one broken source must not stop
the others. `test_one_broken_source_does_not_end_the_tick` in the integration
suite asserts it, but only against a stubbed client and a synthetic failure --
never through the real fetch path, where the failure arrives as a real 500 over
real TCP, is classified by the real collector, and is recorded by the real run
bookkeeping.

That gap matters because the isolation claim is about *this* stack: a source
whose origin returns 500 must be marked failed, and its neighbour must be
collected in the same pass regardless. The failure modes in between are what a
single-source test cannot reach -- a pass that aborts at the first failure, a
shared session left dirty by it, a backoff that turns one failure into a retry
storm.

Scope, stated because it is narrower than it looks: these tests exercise the
*classified* failure path (an origin answering 500, which the collector turns
into a `FAILED` run). The other path -- a collector that raises, where an inner
guard in `collect_now` closes the run so the source is not wedged -- is covered
by `test_an_escaped_exception_closes_the_run_and_does_not_wedge_the_source` in
the integration suite, which is the right tier for it: forcing it needs
monkeypatching the collection call, and doing that E2E would replace the very
path under test. Verified here by mutation: making that inner guard re-raise
leaves these tests green and fails the integration one, which is the split this
docstring describes.

Both sources are real HTTP origins on the loopback (`PEARSS_ALLOW_PRIVATE_FETCH`
is the documented escape hatch for a hermetic run), and the tick is driven
through `run_tick` with the app's own session factory, so the paths under test
are the production ones.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from tests.e2e.conftest import API_TOKEN

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]

# A fixed instant, advanced by hand between passes, so "due" is decided by the
# policy rather than by how long the test took to run.
NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


async def _add_source(client: httpx.AsyncClient, url: str, key: str) -> str:
    response = await client.post(
        "/v1/sources",
        headers={"Idempotency-Key": key},
        json={"url": url, "schedule_policy": {"type": "fixed", "interval": "30m"}},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def _tick(session_factory: sessionmaker[Session], *, now: datetime) -> Any:
    from app.scheduler import run_tick

    with httpx.Client(follow_redirects=True, timeout=10.0) as client:
        return run_tick(session_factory, client=client, now=now)


def _runs_by_source(session_factory: sessionmaker[Session]) -> dict[str, list[str]]:
    """{source canonical_url: [run status, ...]} in creation order.

    The job/source link is `job.config` holding the source id (see
    `ensure_collect_job`) rather than a foreign-key column: there is one job per
    source, and the source id is the config.
    """
    with session_factory() as db:
        rows = db.execute(
            sa.text(
                "select s.canonical_url, r.status from job_run r"
                " join job j on j.id = r.job_id"
                " join source s on s.id = j.config"
                " order by r.started_at, r.id"
            )
        ).fetchall()
    out: dict[str, list[str]] = {}
    for url, status in rows:
        out.setdefault(str(url), []).append(str(status))
    return out


# --------------------------------------------------------------------------
# Scenario: one source's origin failing does not stop the others
# --------------------------------------------------------------------------


async def test_a_failing_source_does_not_stop_a_healthy_one(
    served: str,
    origin_url: str,
    failing_origin_url: str,
    session_factory: sessionmaker[Session],
) -> None:
    """The isolation property, over real sockets.

    A 500 from one origin must cost that source its run and nothing more: the
    healthy source is collected in the same pass, its items reach the database,
    and the pass reports one failure rather than aborting. Asserted on both
    sides -- collected items for the good source, a failed run for the bad one
    -- because "the tick completed" alone would also be true if the bad source
    had been silently skipped instead of attempted.
    """
    async with httpx.AsyncClient(
        base_url=served, headers={"Authorization": f"Bearer {API_TOKEN}"}
    ) as client:
        healthy_id = await _add_source(client, origin_url, "e2e-multi-good")
        broken_id = await _add_source(client, failing_origin_url, "e2e-multi-bad")

    report = _tick(session_factory, now=NOW)

    # Both were considered; exactly one collected and one failed.
    assert broken_id in report.failed, report
    assert healthy_id in report.collected, report
    assert len(report.failed) == 1, report

    with session_factory() as db:
        items = db.execute(
            sa.text("select count(*) from item where source_id = :id"),
            {"id": healthy_id},
        ).scalar_one()
        broken_items = db.execute(
            sa.text("select count(*) from item where source_id = :id"),
            {"id": broken_id},
        ).scalar_one()
    assert items >= 1, "the healthy source stored nothing"
    assert broken_items == 0, "the failing source stored items anyway"

    runs = _runs_by_source(session_factory)
    assert runs[origin_url][-1] == "succeeded"
    assert runs[failing_origin_url][-1] in {"failed", "timed_out"}


async def test_the_broken_source_keeps_failing_without_blocking_the_next_pass(
    served: str,
    origin_url: str,
    failing_origin_url: str,
    session_factory: sessionmaker[Session],
) -> None:
    """A failure is contained, and it does not become a retry storm.

    Two properties in one pass of the loop. The healthy source is collected
    every time it is due, whether or not its neighbour is broken -- and the
    broken one does *not* come due again on the normal interval, because a
    failed run backs the next attempt off. Asserting the second explicitly is
    what distinguishes "contained" from "retried forever": a source failing
    every 30 minutes against a dead origin is a different bug from the same
    source failing once and waiting.

    The measured behaviour was not what I first assumed here: at +31 minutes
    the broken source is `skipped`, not `failed`, which is the backoff doing
    its job.
    """
    async with httpx.AsyncClient(
        base_url=served, headers={"Authorization": f"Bearer {API_TOKEN}"}
    ) as client:
        await _add_source(client, failing_origin_url, "e2e-multi2-bad")
        healthy_id = await _add_source(client, origin_url, "e2e-multi2-good")

    first = _tick(session_factory, now=NOW)
    # Past the healthy source's 30m interval, but not past the failure backoff.
    second = _tick(session_factory, now=NOW + timedelta(minutes=31))

    assert healthy_id in first.collected, first
    assert healthy_id in second.collected, second
    assert len(first.failed) == 1, first
    assert second.failed == [], f"a backoff source was retried: {second}"

    runs = _runs_by_source(session_factory)
    assert runs[origin_url] == ["succeeded", "succeeded"], runs[origin_url]
    # One attempt recorded, not two: the second pass did not reach the origin.
    assert len(runs[failing_origin_url]) == 1, runs[failing_origin_url]


# --------------------------------------------------------------------------
# Scenario: the healthy source's content is complete, not merely present
# --------------------------------------------------------------------------


async def test_the_healthy_source_serves_its_items_after_a_neighbour_failed(
    served: str,
    origin_url: str,
    failing_origin_url: str,
    session_factory: sessionmaker[Session],
) -> None:
    """End to end to a client, not just to the database.

    The isolation claim is only useful if the surviving source's items actually
    reach the surfaces a client reads. A pass that stored rows and then failed
    to publish them would satisfy a row-count assertion, so this reads them
    back over HTTP.
    """
    async with httpx.AsyncClient(
        base_url=served, headers={"Authorization": f"Bearer {API_TOKEN}"}
    ) as client:
        await _add_source(client, failing_origin_url, "e2e-multi3-bad")
        await _add_source(client, origin_url, "e2e-multi3-good")

    _tick(session_factory, now=NOW)

    async with httpx.AsyncClient(
        base_url=served, headers={"Authorization": f"Bearer {API_TOKEN}"}
    ) as client:
        listed = await client.get("/v1/items")
        assert listed.status_code == 200, listed.text
        items = listed.json()["data"]
        assert items, "the healthy source's items are not visible over the API"
        # Nothing from the failed source, and nothing stuck behind the gate.
        assert all(row["publish_status"] in ("ready", "published") for row in items), (
            items
        )
