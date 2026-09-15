"""Paging a run history: a new run must not push one out of the walk.

The reason this is keyset and not offset is a real behaviour, not a
preference. Runs are appended at the front of a newest-first list, so an
offset-based page two re-reads the window that just shifted: it silently skips
a run for every one that arrived. The scenario below is exactly that race,
staged deterministically -- page one, insert a run, page two -- so the defect
it guards against is reproduced rather than described.

The runs are seeded through the repository, and the pages are read through the
MCP surface, so what is asserted is the contract an agent sees.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]

TENANT = "default"


def _payload(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    for block in getattr(result, "content", []):
        text = getattr(block, "text", None)
        if text:
            loaded = json.loads(text)
            if isinstance(loaded, dict):
                return loaded
    raise AssertionError(f"no structured payload: {result!r}")


def _seed_source_and_job(
    session_factory: sessionmaker[Session],
) -> tuple[str, str]:
    """Create one source and its collection job; return both ids.

    Uses the repository's own `create_source` + `ensure_collect_job` rather
    than hand-written rows: the first draft of this file inserted a Job
    directly and left `schedule`/`updated_at` unset, which the schema rejects.
    The repository is the thing that knows the required columns.
    """
    from adapters.repository import create_source, ensure_collect_job
    from adapters.timestamps import fixed_width_utc as timestamp

    at = timestamp(datetime(2026, 9, 15, 12, 0, tzinfo=UTC))
    with session_factory() as db:
        source = create_source(
            db,
            source_type="rss",
            url="https://paging.example/feed.xml",
            canonical_url="https://paging.example/feed.xml",
            schedule_policy='{"type": "fixed", "interval": "30m"}',
            tenant_id=TENANT,
            at=at,
        )
        job_id = ensure_collect_job(
            db, source_id=str(source.id), tenant_id=TENANT, at=at
        )
        db.commit()
        return str(source.id), str(job_id)


def _add_run(
    session_factory: sessionmaker[Session],
    job_id: str,
    run_id: str,
    started_at: datetime,
) -> None:
    """Store one finished run, dated `started_at`.

    `create_job_run` opens a run with `started_at=None` (a queued run has not
    started), and the one-active-run index forbids a second queued row, so this
    writes the finished state directly through the model for the history this
    test needs to page through.
    """
    from adapters.db_models import JobRun
    from adapters.timestamps import fixed_width_utc as timestamp

    with session_factory() as db:
        db.add(
            JobRun(
                id=run_id,
                job_id=job_id,
                status="succeeded",
                metrics="{}",
                correlation_id="c-pagination",
                started_at=timestamp(started_at),
                finished_at=timestamp(started_at + timedelta(seconds=5)),
            )
        )
        db.commit()


async def test_paging_a_run_history_skips_nothing_when_a_run_arrives_mid_walk(
    mcp_url: str,
    open_mcp_session: Callable[..., Any],
    session_factory: sessionmaker[Session],
) -> None:
    """Page one, insert a newer run, page two: nothing is skipped.

    With offset paging the inserted run shifts every row down by one, so page
    two starts after the run that page one already showed and the *oldest*
    run of page one is never seen. Keyset compares against the last row read,
    so it is unaffected.
    """
    _source_id, job_id = _seed_source_and_job(session_factory)
    base = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
    for index in range(4):
        _add_run(
            session_factory,
            job_id,
            f"run-{index}",
            base + timedelta(minutes=index),
        )

    async with open_mcp_session(mcp_url) as session:
        first = _payload(
            await session.call_tool("list_runs", {"job_id": job_id, "limit": 2})
        )
        assert len(first["data"]) == 2, first
        assert first.get("next_cursor"), first

        # A run lands between the two reads -- the exact case offset paging
        # gets wrong.
        _add_run(
            session_factory,
            job_id,
            "run-arrived-mid-walk",
            base + timedelta(minutes=10),
        )

        second = _payload(
            await session.call_tool(
                "list_runs",
                {"job_id": job_id, "limit": 2, "cursor": first["next_cursor"]},
            )
        )

    seen = [row["id"] for row in first["data"]] + [row["id"] for row in second["data"]]
    # The two oldest runs must both appear: page one showed run-3 and run-2,
    # so page two must show run-1 and run-0. An offset cursor would return
    # run-0 only, having skipped run-1.
    assert "run-1" in seen, f"a run was skipped by paging: {seen}"
    assert "run-0" in seen, f"the oldest run was skipped: {seen}"
    assert len(set(seen)) == len(seen), f"a run appeared twice: {seen}"


async def test_a_run_history_reports_the_end_of_the_history(
    mcp_url: str,
    open_mcp_session: Callable[..., Any],
    session_factory: sessionmaker[Session],
) -> None:
    """The last page carries no cursor, so a client knows to stop.

    Without this a client cannot tell "no more runs" from "the cursor was
    lost", and would either loop or silently under-read the history.
    """
    _source_id, job_id = _seed_source_and_job(session_factory)
    base = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
    _add_run(session_factory, job_id, "only-run", base)

    async with open_mcp_session(mcp_url) as session:
        payload = _payload(
            await session.call_tool("list_runs", {"job_id": job_id, "limit": 10})
        )

    assert [row["id"] for row in payload["data"]] == ["only-run"]
    assert payload.get("next_cursor") is None, payload


async def test_a_malformed_cursor_is_refused_not_silently_restarted(
    mcp_url: str,
    open_mcp_session: Callable[..., Any],
    session_factory: sessionmaker[Session],
) -> None:
    """A bad cursor is an error, not a quiet return to page one.

    Silently restarting would look like a successful page-two request while
    handing back rows the caller has already read -- a walk that never
    finishes and never reports why.
    """
    _source_id, job_id = _seed_source_and_job(session_factory)
    _add_run(session_factory, job_id, "r1", datetime(2026, 9, 15, 12, 0, tzinfo=UTC))

    async with open_mcp_session(mcp_url) as session:
        payload = _payload(
            await session.call_tool(
                "list_runs", {"job_id": job_id, "cursor": "not-a-real-cursor!!"}
            )
        )

    assert payload.get("error") == "invalid_cursor", payload
